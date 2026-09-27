"""HTTP contracts for deterministic platform-notification noise controls."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.application.sources import EndpointDraft, SourceDraft
from app.bootstrap import create_job_platform_app
from app.domains.sources.models import EndpointObservation, merge_endpoint_observations


UTC = timezone.utc


def _candidate(tmp_path: Path):
    key = tmp_path / "master.key"
    key.write_text("existing-test-key\n", encoding="utf-8")
    resources = create_job_platform_app(
        database_path=tmp_path / "noise-api.db",
        master_key_path=key,
        cursor_secret=b"noise-api-cursor-key-at-least-32-bytes",
    )
    resources.sources.create_source(
        SourceDraft(
            "src-a",
            "Primary AM",
            (EndpointDraft(0, "https://am.invalid"),),
            resolution_grace_seconds=0,
        ),
        now=datetime(2026, 8, 24, tzinfo=UTC),
    )
    return resources


def _apply(resources, now: datetime) -> None:
    alert = {
        "fingerprint": "checkout-down",
        "labels": {
            "alertname": "CheckoutDown",
            "severity": "warning",
            "cluster": "cluster-a",
        },
        "annotations": {"summary": "checkout unavailable"},
        "startsAt": "2026-08-24T00:00:00Z",
    }
    snapshot = resources.sources.load_snapshot("src-a", expected_version=1)
    outcome = merge_endpoint_observations(
        (EndpointObservation(snapshot.endpoints[0], "SUCCESS", (alert,), 1),)
    )
    assert resources.sources.apply_collection(snapshot, outcome, observed_at=now).committed


def test_source_controls_and_maintenance_are_bounded_and_versioned(
    tmp_path: Path,
) -> None:
    resources = _candidate(tmp_path)
    with TestClient(resources.app) as client:
        controls = client.get("/api/v1/sources/src-a/noise-controls")
        assert controls.status_code == 200
        assert controls.json() == {
            "source_id": "src-a",
            "flapping_enabled": True,
            "storm_enabled": True,
            "storm_alert_threshold": 100,
            "storm_occurrence_threshold": 20,
            "storm_active": False,
            "storm_started_at": None,
            "version": 1,
        }
        updated = client.put(
            "/api/v1/sources/src-a/noise-controls",
            json={
                "flapping_enabled": False,
                "storm_enabled": True,
                "storm_alert_threshold": 200,
                "storm_occurrence_threshold": 30,
                "expected_version": 1,
            },
        )
        assert updated.status_code == 200
        assert updated.json()["version"] == 2
        invalid = client.put(
            "/api/v1/sources/src-a/noise-controls",
            json={
                "flapping_enabled": True,
                "storm_enabled": True,
                "storm_alert_threshold": 9,
                "storm_occurrence_threshold": 20,
                "expected_version": 2,
            },
        )
        assert invalid.status_code == 422

        now = datetime.now(UTC)
        created = client.post(
            "/api/v1/maintenance-windows",
            headers={"Idempotency-Key": "maintenance-source-window"},
            json={
                "scope_kind": "SOURCE",
                "source_id": "src-a",
                "starts_at": now.isoformat(),
                "ends_at": (now + timedelta(hours=1)).isoformat(),
                "reason": "planned source maintenance",
            },
        )
        assert created.status_code == 201
        window = created.json()
        assert window["scope_kind"] == "SOURCE"
        assert window["reason"] == "planned source maintenance"
        assert len(client.get("/api/v1/maintenance-windows").json()) == 1
        ended = client.post(
            f"/api/v1/maintenance-windows/{window['id']}/end",
            json={"expected_version": window["version"]},
        )
        assert ended.status_code == 200
        assert ended.json()["status"] == "ENDED"
        assert client.get("/api/v1/maintenance-windows").json() == []

        too_long = client.post(
            "/api/v1/maintenance-windows",
            headers={"Idempotency-Key": "maintenance-too-long"},
            json={
                "scope_kind": "SOURCE",
                "source_id": "src-a",
                "starts_at": now.isoformat(),
                "ends_at": (now + timedelta(days=8)).isoformat(),
                "reason": "invalid duration",
            },
        )
        assert too_long.status_code == 422


def test_occurrence_suppression_is_visible_audited_and_not_backfilled(
    tmp_path: Path,
) -> None:
    resources = _candidate(tmp_path)
    _apply(resources, datetime.now(UTC) - timedelta(minutes=1))
    with TestClient(resources.app) as client:
        occurrence = client.get("/api/v1/occurrences").json()["items"][0]
        occurrence_id = occurrence["id"]
        created = client.post(
            f"/api/v1/occurrences/{occurrence_id}/suppression",
            headers={"Idempotency-Key": "suppress-checkout-once"},
            json={"duration_seconds": 900, "reason": "duplicate paging during rollout"},
        )
        assert created.status_code == 201
        suppression = created.json()
        active = client.get(f"/api/v1/occurrences/{occurrence_id}/noise")
        assert active.status_code == 200
        assert active.json()["state"] == "SUPPRESSED"
        assert active.json()["scope"] == "OCCURRENCE"
        queue = client.get("/api/v1/occurrences").json()["items"]
        assert [item["id"] for item in queue] == [occurrence_id]
        assert queue[0]["noise_state"] == "SUPPRESSED"
        assert queue[0]["noise_reason"] == "duplicate paging during rollout"
        assert queue[0]["noise_remaining_seconds"] > 0

        ended = client.post(
            f"/api/v1/occurrences/{occurrence_id}/suppression/end",
            json={"expected_version": suppression["version"]},
        )
        assert ended.status_code == 200
        assert ended.json()["status"] == "ENDED"
        assert client.get(f"/api/v1/occurrences/{occurrence_id}/noise").json()[
            "state"
        ] == "NONE"
        timeline = client.get(f"/api/v1/occurrences/{occurrence_id}/timeline").json()
        assert [item["event_type"] for item in timeline][-2:] == [
            "SUPPRESSION_STARTED",
            "SUPPRESSION_ENDED",
        ]
        assert timeline[-1]["detail"]["no_backfill"] is True
