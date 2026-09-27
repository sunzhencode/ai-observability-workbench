"""Candidate current-Incident and occurrence-history HTTP contracts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.application.sources import EndpointDraft, SourceDraft
from app.bootstrap import create_job_platform_app
from app.bootstrap.job_platform import JobPlatformResources
from app.domains.sources.models import EndpointObservation, merge_endpoint_observations
from app.main import app as current_default_app


UTC = timezone.utc


def _raw() -> dict[str, object]:
    return {
        "fingerprint": "target-one",
        "labels": {
            "alertname": "TargetDown",
            "severity": "warning",
            "cluster": "cluster-a",
        },
        "annotations": {"summary": "target unavailable"},
        "startsAt": "2026-08-13T00:00:00Z",
    }


def _candidate(tmp_path: Path) -> JobPlatformResources:
    key = tmp_path / "master.key"
    key.write_text("existing-test-key\n", encoding="utf-8")
    resources = create_job_platform_app(
        database_path=tmp_path / "incident-operations.db",
        master_key_path=key,
        cursor_secret=b"incident-api-cursor-key-at-least-32-bytes",
    )
    resources.sources.create_source(
        SourceDraft(
            id="src-a",
            name="Primary AM",
            endpoints=(EndpointDraft(0, "https://am.invalid"),),
            resolution_grace_seconds=0,
        ),
        now=datetime(2026, 8, 13, tzinfo=UTC),
    )
    return resources


def _apply(
    resources: JobPlatformResources,
    alerts: tuple[dict[str, object], ...],
    now: datetime,
) -> None:
    snapshot = resources.sources.load_snapshot("src-a", expected_version=1)
    outcome = merge_endpoint_observations(
        (EndpointObservation(snapshot.endpoints[0], "SUCCESS", alerts, 2),)
    )
    assert resources.sources.apply_collection(snapshot, outcome, observed_at=now).committed


def test_candidate_incident_detail_handling_conflict_and_history_cursor(
    tmp_path: Path,
) -> None:
    resources = _candidate(tmp_path)
    t0 = datetime(2026, 8, 13, 1, tzinfo=UTC)
    _apply(resources, (_raw(),), t0)

    with TestClient(resources.app) as client:
        listed = client.get("/api/v1/incidents")
        assert listed.status_code == 200
        incident = listed.json()[0]
        assert incident["handling_state"] == "NEW"
        assert incident["handling_version"] == 1
        assert incident["member_count"] == 1
        assert incident["occurrence_started_at"].endswith("Z")
        incident_id = incident["id"]

        detail = client.get(f"/api/v1/incidents/{incident_id}")
        assert detail.status_code == 200
        assert detail.json()["source_name"] == "Primary AM"
        assert detail.json()["handling_history"] == []

        queue = client.get("/api/v1/occurrences?view=UNMAPPED")
        assert queue.status_code == 200
        assert queue.json()["evaluated_at"].endswith("Z")
        operational = queue.json()["items"][0]
        assert operational["incident_id"] == incident_id
        assert operational["source_name"] == "Primary AM"
        assert operational["assignment_origin"] == "UNMAPPED"
        assert operational["service_id"] is None
        assert operational["member_count"] == 1
        assert operational["evidence_completeness"] == "COMPLETE"
        assert operational["ack_sla_seconds"] == 900
        assert operational["ack_sla_state"] in {
            "ON_TRACK",
            "AT_RISK",
            "BREACHED",
        }
        overview = client.get(f"/api/v1/occurrences/{operational['id']}")
        assert overview.status_code == 200
        assert overview.json()["id"] == operational["id"]
        assert overview.json()["group_key"] == operational["group_key"]
        assert overview.json()["ack_sla_due_at"] == operational["ack_sla_due_at"]
        assert "response_priority" not in overview.json()

        bad_queue_cursor = client.get(
            "/api/v1/occurrences", params={"cursor": "not-a-valid-cursor"}
        )
        assert bad_queue_cursor.status_code == 400
        assert bad_queue_cursor.json()["error"]["code"] == "CURSOR_INVALID"
        assert "priorities" not in client.get("/openapi.json").text

        retired_handling = client.patch(
            f"/api/v1/incidents/{incident_id}/handling",
            json={"state": "IN_PROGRESS", "reason": "old command", "actor": "local-user", "expected_version": 1},
        )
        assert retired_handling.status_code == 404

        _apply(resources, (), t0 + timedelta(minutes=1))
        _apply(resources, (), t0 + timedelta(minutes=2))
        history = client.get("/api/v1/incident-occurrences?limit=1")
        assert history.status_code == 200
        item = history.json()["items"][0]
        assert item["incident_id"] == incident_id
        assert item["recovered_at"].endswith("Z")
        assert item["handling_conclusion"] == "NEW"

        tampered = client.get(
            "/api/v1/incident-occurrences",
            params={"cursor": "not-a-valid-cursor"},
        )
        assert tampered.status_code == 400
        assert tampered.json()["error"]["code"] == "CURSOR_INVALID"

        missing = client.get("/api/v1/incidents/999999")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "INCIDENT_NOT_FOUND"

    resources.engine.dispose()


def test_current_default_application_is_not_switched_to_candidate_routes() -> None:
    current_paths = {
        route.path for route in current_default_app.routes if hasattr(route, "path")
    }
    assert "/api/v1/incident-occurrences" not in current_paths
    assert "/api/v1/occurrences" not in current_paths
    assert "/api/v1/incidents/{incident_id}/handling" not in current_paths


def test_response_commands_are_versioned_idempotent_and_audited(
    tmp_path: Path,
) -> None:
    resources = _candidate(tmp_path)
    t0 = datetime(2026, 8, 13, 1, tzinfo=UTC)
    _apply(resources, (_raw(),), t0)

    with TestClient(resources.app) as client:
        occurrence = client.get("/api/v1/occurrences").json()["items"][0]
        occurrence_id = occurrence["id"]
        start_body = {"expected_version": 1, "reason": "scope checked"}
        headers = {"Idempotency-Key": "operator-start-key-0001"}

        started = client.post(
            f"/api/v1/occurrences/{occurrence_id}/start-handling",
            json=start_body,
            headers=headers,
        )
        assert started.status_code == 200
        assert started.json()["current_state"] == "IN_PROGRESS"
        assert started.json()["version"] == 2
        assert started.json()["timeline"][0]["event_type"] == "RESPONSE_HANDLING_STARTED"
        assert started.json()["timeline"][0]["actor_type"] == "INTERACTIVE_OPERATOR"
        assert started.json()["timeline"][0]["request_id"]
        assert "response_priority" not in started.json()

        replay = client.post(
            f"/api/v1/occurrences/{occurrence_id}/start-handling",
            json=start_body,
            headers=headers,
        )
        assert replay.status_code == 200
        assert replay.json()["replayed"] is True
        assert replay.json()["timeline"] == started.json()["timeline"]

        reused = client.post(
            f"/api/v1/occurrences/{occurrence_id}/start-handling",
            json={"expected_version": 1, "reason": "different request"},
            headers=headers,
        )
        assert reused.status_code == 409
        assert reused.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"

        stale = client.post(
            f"/api/v1/occurrences/{occurrence_id}/resolve",
            json={"expected_version": 1, "resolution_code": "NO_ACTION", "reason": "not actionable"},
            headers={"Idempotency-Key": "operator-resolve-stale"},
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "CONCURRENT_MODIFICATION"

        incompatible_resolution = client.post(
            f"/api/v1/occurrences/{occurrence_id}/resolve",
            json={"expected_version": 2, "resolution_code": "FIXED", "reason": "asserted fixed"},
            headers={"Idempotency-Key": "operator-resolve-invalid"},
        )
        assert incompatible_resolution.status_code == 422
        assert incompatible_resolution.json()["error"]["code"] == "RESOLUTION_SIGNAL_STATE_INVALID"

        resolved = client.post(
            f"/api/v1/occurrences/{occurrence_id}/resolve",
            json={
                "expected_version": 2,
                "resolution_code": "NO_ACTION",
                "reason": "signal is not actionable in this environment",
            },
            headers={"Idempotency-Key": "operator-resolve-00001"},
        )
        assert resolved.status_code == 200
        assert resolved.json()["current_state"] == "RESOLVED"

        timeline = client.get(
            f"/api/v1/occurrences/{occurrence_id}/timeline"
        )
        assert timeline.status_code == 200
        assert [item["sequence"] for item in timeline.json()] == [1, 2]
        assert [item["event_type"] for item in timeline.json()] == [
            "RESPONSE_HANDLING_STARTED",
            "OCCURRENCE_RESOLVED",
        ]

        refreshed = client.get(f"/api/v1/occurrences/{occurrence_id}").json()
        assert refreshed["response_state"] == "RESOLVED"
        assert refreshed["resolution_code"] == "NO_ACTION"
        assert refreshed["ack_sla_state"] == "STOPPED"

    resources.engine.dispose()


def test_batch_start_handling_is_bounded_partial_success(tmp_path: Path) -> None:
    resources = _candidate(tmp_path)
    t0 = datetime(2026, 8, 13, 1, tzinfo=UTC)
    first = _raw()
    second = _raw()
    second["fingerprint"] = "target-two"
    second["labels"] = {
        "alertname": "DatabaseDown",
        "severity": "critical",
        "cluster": "cluster-a",
    }
    _apply(resources, (first, second), t0)

    with TestClient(resources.app) as client:
        items = client.get("/api/v1/occurrences").json()["items"]
        assert len(items) == 2
        response = client.post(
            "/api/v1/occurrences/batch-start-handling",
            json={
                "items": [
                    {"occurrence_id": items[0]["id"], "expected_version": 1},
                    {"occurrence_id": items[1]["id"], "expected_version": 99},
                ],
                "reason": "batch scope checked",
            },
            headers={"Idempotency-Key": "operator-batch-start-0001"},
        )
        assert response.status_code == 200
        assert response.json()["succeeded"] == 1
        assert response.json()["failed"] == 1
        assert [item["ok"] for item in response.json()["items"]] == [True, False]
        assert response.json()["items"][1]["error_code"] == "CONCURRENT_MODIFICATION"

    resources.engine.dispose()


def test_duplicate_target_rules_are_closed(tmp_path: Path) -> None:
    resources = _candidate(tmp_path)
    t0 = datetime(2026, 8, 13, 1, tzinfo=UTC)
    alerts = []
    for index, alertname in enumerate(("ApiDown", "WorkerDown", "QueueDown"), 1):
        item = _raw()
        item["fingerprint"] = f"target-{index}"
        item["labels"] = {
            "alertname": alertname,
            "severity": "warning",
            "cluster": "cluster-a",
        }
        alerts.append(item)
    _apply(resources, tuple(alerts), t0)

    with TestClient(resources.app) as client:
        occurrences = client.get("/api/v1/occurrences").json()["items"]
        first, second, third = [item["id"] for item in occurrences]
        for occurrence_id in (first, second, third):
            started = client.post(
                f"/api/v1/occurrences/{occurrence_id}/start-handling",
                json={"expected_version": 1},
                headers={"Idempotency-Key": f"operator-start-{occurrence_id:05d}"},
            )
            assert started.status_code == 200
        assert client.get(f"/api/v1/occurrences/{first}").json()["acknowledged_at"]

        duplicate = client.post(
            f"/api/v1/occurrences/{second}/resolve",
            json={
                "expected_version": 2,
                "resolution_code": "DUPLICATE",
                "duplicate_of_occurrence_id": first,
                "reason": "same impact is tracked by the canonical occurrence",
            },
            headers={"Idempotency-Key": "operator-duplicate-00001"},
        )
        assert duplicate.status_code == 200
        assert duplicate.json()["resolution_code"] == "DUPLICATE"

        chained = client.post(
            f"/api/v1/occurrences/{third}/resolve",
            json={
                "expected_version": 2,
                "resolution_code": "DUPLICATE",
                "duplicate_of_occurrence_id": second,
                "reason": "attempted duplicate chain",
            },
            headers={"Idempotency-Key": "operator-duplicate-chain1"},
        )
        assert chained.status_code == 422
        assert chained.json()["error"]["code"] == "DUPLICATE_TARGET_INVALID"
        assert [
            item["event_type"]
            for item in client.get(
                f"/api/v1/occurrences/{third}/timeline"
            ).json()
        ] == ["RESPONSE_HANDLING_STARTED"]

    resources.engine.dispose()


def test_tasks_runbook_notes_and_redaction_are_audited_without_execution(
    tmp_path: Path,
) -> None:
    resources = _candidate(tmp_path)
    t0 = datetime(2026, 8, 13, 1, tzinfo=UTC)
    _apply(resources, (_raw(),), t0)

    with TestClient(resources.app) as client:
        occurrence = client.get("/api/v1/occurrences").json()["items"][0]
        occurrence_id = occurrence["id"]
        handling = client.post(
            f"/api/v1/occurrences/{occurrence_id}/start-handling",
            json={"expected_version": 1},
            headers={"Idempotency-Key": "operator-start-before-task"},
        )
        assert handling.status_code == 200
        created = client.post(
            f"/api/v1/occurrences/{occurrence_id}/tasks",
            json={
                "title": "Verify database replication",
                "description": "Follow the runbook and record the observed result.",
                "due_at": "2026-08-13T02:00:00Z",
                "runbook_link": "https://runbooks.internal.example/db/replication",
            },
            headers={"Idempotency-Key": "operator-task-create-0001"},
        )
        assert created.status_code == 201
        task = created.json()["task"]
        assert task["status"] == "TODO"
        assert task["runbook_link"].startswith("https://")
        assert created.json()["timeline"]["event_type"] == "TASK_CREATED"

        blocked_resolve = client.post(
            f"/api/v1/occurrences/{occurrence_id}/resolve",
            json={
                "expected_version": 2,
                "resolution_code": "NO_ACTION",
                "reason": "This must not freeze an unfinished task",
            },
            headers={"Idempotency-Key": "operator-resolve-open-task"},
        )
        assert blocked_resolve.status_code == 422
        assert (
            blocked_resolve.json()["error"]["code"]
            == "INCIDENT_OPEN_TASKS_REMAIN"
        )
        unchanged = client.get(f"/api/v1/occurrences/{occurrence_id}").json()
        assert unchanged["response_state"] == "IN_PROGRESS"
        assert unchanged["version"] == 2
        assert [
            entry["event_type"]
            for entry in client.get(
                f"/api/v1/occurrences/{occurrence_id}/timeline"
            ).json()
        ] == ["RESPONSE_HANDLING_STARTED", "TASK_CREATED"]

        replay = client.post(
            f"/api/v1/occurrences/{occurrence_id}/tasks",
            json={
                "title": "Verify database replication",
                "description": "Follow the runbook and record the observed result.",
                "due_at": "2026-08-13T02:00:00Z",
                "runbook_link": "https://runbooks.internal.example/db/replication",
            },
            headers={"Idempotency-Key": "operator-task-create-0001"},
        )
        assert replay.status_code == 201
        assert replay.json()["replayed"] is True
        assert replay.json()["task"]["id"] == task["id"]

        unsafe = client.post(
            f"/api/v1/occurrences/{occurrence_id}/tasks",
            json={"title": "Do not execute", "runbook_link": "http://unsafe.invalid/run"},
            headers={"Idempotency-Key": "operator-task-create-0002"},
        )
        assert unsafe.status_code == 422
        assert unsafe.json()["error"]["code"] == "RUNBOOK_LINK_HTTPS_REQUIRED"

        started = client.post(
            f"/api/v1/occurrences/{occurrence_id}/tasks/{task['id']}/transition",
            json={"expected_version": 1, "target": "IN_PROGRESS"},
            headers={"Idempotency-Key": "operator-task-start-0001"},
        )
        assert started.status_code == 200
        assert started.json()["task"]["status"] == "IN_PROGRESS"
        assert started.json()["task"]["version"] == 2

        missing_result = client.post(
            f"/api/v1/occurrences/{occurrence_id}/tasks/{task['id']}/transition",
            json={"expected_version": 2, "target": "DONE"},
            headers={"Idempotency-Key": "operator-task-done-invalid"},
        )
        assert missing_result.status_code == 422
        assert missing_result.json()["error"]["code"] == "INCIDENT_TASK_RESULT_REQUIRED"

        done = client.post(
            f"/api/v1/occurrences/{occurrence_id}/tasks/{task['id']}/transition",
            json={
                "expected_version": 2,
                "target": "DONE",
                "result": "Replica caught up; lag stayed below 1s for 10m.",
            },
            headers={"Idempotency-Key": "operator-task-done-0001"},
        )
        assert done.status_code == 200
        assert done.json()["task"]["result"].startswith("Replica")

        note = client.post(
            f"/api/v1/occurrences/{occurrence_id}/notes",
            json={"text": "Customer impact confirmed for tenant alpha."},
            headers={"Idempotency-Key": "operator-note-create-0001"},
        )
        assert note.status_code == 201
        assert note.json()["timeline"]["event_type"] == "MANUAL_NOTE"
        assert note.json()["timeline"]["detail"]["text"] == "Customer impact confirmed for tenant alpha."
        note_sequence = note.json()["timeline"]["sequence"]

        resolved = client.post(
            f"/api/v1/occurrences/{occurrence_id}/resolve",
            json={
                "expected_version": 2,
                "resolution_code": "NO_ACTION",
                "reason": "No further handling is required in this fixture",
            },
            headers={"Idempotency-Key": "operator-resolve-before-redaction"},
        )
        assert resolved.status_code == 200

        redacted = client.post(
            f"/api/v1/occurrences/{occurrence_id}/notes/{note_sequence}/redact",
            json={"reason": "Contains sensitive tenant identity"},
            headers={"Idempotency-Key": "operator-note-redact-0001"},
        )
        assert redacted.status_code == 200
        assert redacted.json()["timeline"]["event_type"] == "NOTE_REDACTED"

        for path, body, key in (
            (
                f"/api/v1/occurrences/{occurrence_id}/tasks",
                {"title": "Must remain read-only"},
                "operator-task-after-resolve",
            ),
            (
                f"/api/v1/occurrences/{occurrence_id}/notes",
                {"text": "Must remain read-only"},
                "operator-note-after-resolve",
            ),
            (
                f"/api/v1/occurrences/{occurrence_id}/tasks/{task['id']}/transition",
                {
                    "expected_version": 3,
                    "target": "CANCELED",
                    "reason": "Must remain read-only",
                },
                "operator-task-transition-after-resolve",
            ),
        ):
            denied = client.post(
                path,
                json=body,
                headers={"Idempotency-Key": key},
            )
            assert denied.status_code == 422
            assert (
                denied.json()["error"]["code"]
                == "INCIDENT_COLLABORATION_RESOLVED_READ_ONLY"
            )

        tasks = client.get(f"/api/v1/occurrences/{occurrence_id}/tasks")
        assert tasks.status_code == 200
        assert tasks.json()[0]["status"] == "DONE"
        timeline = client.get(f"/api/v1/occurrences/{occurrence_id}/timeline")
        assert timeline.status_code == 200
        events = [item["event_type"] for item in timeline.json()]
        assert events == [
            "RESPONSE_HANDLING_STARTED",
            "TASK_CREATED",
            "TASK_STATE_CHANGED",
            "TASK_STATE_CHANGED",
            "MANUAL_NOTE",
            "OCCURRENCE_RESOLVED",
            "NOTE_REDACTED",
        ]
        manual_note = next(item for item in timeline.json() if item["event_type"] == "MANUAL_NOTE")
        assert manual_note["detail"] == {
            "note_id": manual_note["detail"]["note_id"],
            "redacted": True,
            "text": "[REDACTED]",
        }

        with resources.engine.connect() as connection:
            envelope, purge_after = connection.execute(
                text(
                    "SELECT content_envelope, purge_after FROM incident_note_content"
                )
            ).one()
        assert "tenant alpha" not in str(envelope)
        assert purge_after is not None
        resources.incidents.cleanup(
            now=datetime.fromisoformat(str(purge_after)).replace(tzinfo=UTC)
            + timedelta(seconds=1),
            runtime_days=30,
            history_days=365,
        )
        with resources.engine.connect() as connection:
            purged = connection.execute(
                text("SELECT content_envelope FROM incident_note_content")
            ).scalar_one()
        assert purged is None

    resources.engine.dispose()
