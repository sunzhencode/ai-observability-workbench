"""Server-side management health projection contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import time

from fastapi.testclient import TestClient

from app.application.sources import EndpointDraft, SourceDraft
from app.application.observability import MonitoringConnectionDraft
from app.adapters.persistence.notifications import NotificationPolicyRevisionRecord
from app.adapters.persistence.sources import PollRunRecord, SourceRecord
from app.domains.operations.jobs import JobPool, JobSpec
from app.platform.persistence.database import create_session_factory
from app.bootstrap import create_job_platform_app
from app.domains.sources.models import (
    EndpointObservation,
    EndpointSnapshot,
    SourceState,
)


UTC = timezone.utc


class PartialReader:
    async def fetch(self, endpoint: EndpointSnapshot) -> EndpointObservation:
        if endpoint.position == 1:
            return EndpointObservation(
                endpoint,
                "TIMEOUT",
                (),
                15,
                safe_error_code="ENDPOINT_TIMEOUT",
            )
        return EndpointObservation(
            endpoint,
            "SUCCESS",
            (
                {
                    "fingerprint": "checkout-error",
                    "labels": {
                        "alertname": "CheckoutErrorRateHigh",
                        "severity": "warning",
                        "cluster": "local-cluster",
                    },
                    "annotations": {"summary": "checkout errors"},
                    "startsAt": "2026-09-09T00:00:00Z",
                },
                {
                    "fingerprint": "watchdog-local",
                    "labels": {
                        "alertname": "Watchdog",
                        "severity": "info",
                        "cluster": "local-cluster",
                    },
                    "annotations": {},
                    "startsAt": "2026-09-09T00:00:00Z",
                },
            ),
            5,
        )


def _candidate(tmp_path: Path, *, source_reader=None):
    key = tmp_path / "master.key"
    key.write_text("existing-test-key\n", encoding="utf-8")
    return create_job_platform_app(
        database_path=tmp_path / "incident-operations.db",
        master_key_path=key,
        cursor_secret=b"platform-health-cursor-key-at-least-32-bytes",
        source_reader=source_reader,
        model_fake_mode=True,
        notification_fake_mode=True,
    )


class BackfillReader:
    async def query_alerts(self, start, end, step_seconds):
        return {
            "resultType": "matrix",
            "result": [
                {
                    "metric": {
                        "__name__": "ALERTS",
                        "alertstate": "firing",
                        "alertname": "HistoricalTargetDown",
                        "severity": "warning",
                    },
                    "values": [[1_786_496_460, "1"], [1_786_496_520, "1"]],
                }
            ],
        }


def _wait_for_job(client: TestClient, job_id: str) -> str:
    deadline = time.monotonic() + 2
    state = "PENDING"
    while time.monotonic() < deadline:
        state = client.get(f"/api/v1/jobs/{job_id}").json()["state"]
        if state in {"SUCCEEDED", "FAILED", "CANCELED"}:
            return state
        time.sleep(0.01)
    return state


def test_platform_health_is_one_server_snapshot_with_ready_runtime(tmp_path: Path) -> None:
    resources = _candidate(tmp_path)
    with TestClient(resources.app) as client:
        response = client.get("/api/v1/platform-health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["source_mode"] == "UNCONFIGURED"
    assert body["configuration_status"] == "unconfigured"
    assert body["last_poll_at"] is None
    assert body["incident_count"] == 0
    assert body["backfill_skipped"] is True
    assert body["sources"] == []
    assert body["watchdog"]["overall_status"] == "no_data"
    assert body["notifications"]["status"] == "disabled"
    assert body["readiness"]["status"] == "ready"
    assert all(item["code"] == "OK" for item in body["readiness"]["checks"])
    assert body["jobs"]["scheduler_running"] is True
    assert body["jobs"]["runner_running"] is True
    assert body["jobs"]["scheduler_heartbeat_at"] is not None
    assert body["jobs"]["runner_heartbeat_at"] is not None
    assert body["jobs"]["pending"] >= 0
    assert body["jobs"]["running"] >= 0
    assert body["jobs"]["expired_leases"] == 0
    resources.engine.dispose()


def test_platform_health_preserves_partial_source_and_watchdog_semantics(
    tmp_path: Path,
) -> None:
    resources = _candidate(tmp_path, source_reader=PartialReader())
    resources.sources.create_source(
        SourceDraft(
            id="source-a",
            name="Source A",
            endpoints=(
                EndpointDraft(0, "https://am-a.invalid"),
                EndpointDraft(1, "https://am-b.invalid"),
            ),
            watchdog_enabled=True,
        ),
        now=datetime(2026, 9, 9, tzinfo=UTC),
    )
    resources.sources.set_source_state(
        "source-a",
        target=SourceState.ENABLED,
        expected_version=1,
        now=datetime(2026, 9, 9, 0, 0, 1, tzinfo=UTC),
    )

    with TestClient(resources.app) as client:
        accepted = client.post(
            "/api/v1/sources/source-a/collect",
            headers={"Idempotency-Key": "platform-health-source-collect"},
            json={"expected_version": 2},
        )
        assert accepted.status_code == 202
        assert _wait_for_job(client, accepted.json()["job_id"]) == "SUCCEEDED"
        response = client.get("/api/v1/platform-health")

    assert response.status_code == 200
    body = response.json()
    assert body["source_mode"] == "REGISTRY"
    assert body["last_poll_ok"] is False
    assert body["last_poll_error"] == "ENDPOINT_TIMEOUT"
    assert body["incident_count"] == 1
    assert body["sources"] == [
        {
            "source_id": "source-a",
            "source_name": "Source A",
            "lifecycle_state": "ENABLED",
            "health": "DEGRADED",
            "endpoint_total": 2,
            "endpoint_succeeded": 1,
            "endpoint_failed": 1,
            "last_poll_at": body["sources"][0]["last_poll_at"],
            "last_complete_success_at": None,
            "last_any_success_at": body["sources"][0]["last_any_success_at"],
            "safe_error_codes": ["ENDPOINT_TIMEOUT", "PARTIAL_POLL"],
        }
    ]
    assert body["sources"][0]["last_poll_at"] is not None
    assert body["watchdog"]["summary"] == {
        "total": 1,
        "healthy": 1,
        "missing": 0,
        "unknown": 0,
    }
    assert body["watchdog"]["sources"][0]["source_health"] == "DEGRADED"
    assert body["watchdog"]["sources"][0]["clusters"][0]["status"] == "healthy"
    assert body["readiness"]["status"] == "ready"
    resources.engine.dispose()


def test_platform_health_reports_persisted_backfill_outcome(tmp_path: Path) -> None:
    key = tmp_path / "master.key"
    key.write_text("existing-test-key\n", encoding="utf-8")
    resources = create_job_platform_app(
        database_path=tmp_path / "incident-operations.db",
        master_key_path=key,
        cursor_secret=b"platform-health-cursor-key-at-least-32-bytes",
        thanos_factory=lambda _url, _secret: BackfillReader(),
        model_fake_mode=True,
        notification_fake_mode=True,
    )
    resources.sources.create_source(
        SourceDraft(
            id="source-a",
            name="Source A",
            endpoints=(EndpointDraft(0, "https://am-a.invalid"),),
        ),
        now=datetime(2026, 9, 9, tzinfo=UTC),
    )
    resources.sources.set_source_state(
        "source-a",
        target=SourceState.ENABLED,
        expected_version=1,
        now=datetime(2026, 9, 9, 0, 0, 1, tzinfo=UTC),
    )
    resources.observability.save_monitoring_connection(
        MonitoringConnectionDraft("source-a", "THANOS", "https://thanos.invalid"),
        expected_version=None,
        now=datetime(2026, 9, 9, 0, 0, 2, tzinfo=UTC),
    )
    resources.observability.record_monitoring_test(
        "source-a",
        "THANOS",
        expected_version=1,
        ok=True,
        safe_error_code="OK",
        now=datetime(2026, 9, 9, 0, 0, 3, tzinfo=UTC),
    )

    with TestClient(resources.app) as client:
        queued = resources.queue.enqueue(
            JobSpec(
                kind="metrics.backfill",
                pool=JobPool.SOURCE,
                subject_type="event_source",
                subject_id="source-a",
                payload={"expected_connection_version": 1},
                payload_revision=1,
                idempotency_key="platform-health-backfill",
            )
        )
        assert _wait_for_job(client, queued.id) == "SUCCEEDED"
        body = client.get("/api/v1/platform-health").json()
        assert body["last_backfill_ok"] is True
        assert body["last_backfill_error"] is None
        assert body["backfill_skipped"] is False
        assert body["backfill_effective_hours"] == 24
        assert body["backfill_truncated_reason"] is None
        assert body["alerts_reconstructed"] == 1

        failed = resources.queue.enqueue(
            JobSpec(
                kind="metrics.backfill",
                pool=JobPool.SOURCE,
                subject_type="event_source",
                subject_id="missing-source",
                payload={"expected_connection_version": 1},
                payload_revision=1,
                idempotency_key="platform-health-failed-backfill",
            )
        )
        assert _wait_for_job(client, failed.id) == "FAILED"
        failed_body = client.get("/api/v1/platform-health").json()

    assert failed_body["last_backfill_ok"] is False
    assert failed_body["last_backfill_error"] == "JOB_HANDLER_FAILED"
    assert failed_body["backfill_effective_hours"] == 0
    assert failed_body["backfill_truncated_reason"] is None
    assert failed_body["alerts_reconstructed"] == 0
    resources.engine.dispose()


def test_platform_health_reports_notification_configuration_failure(
    tmp_path: Path,
) -> None:
    resources = _candidate(tmp_path)
    sessions = create_session_factory(resources.engine)
    now = datetime(2026, 9, 9, tzinfo=UTC).replace(tzinfo=None)
    with sessions.begin() as session:
        session.add(
            NotificationPolicyRevisionRecord(
                logical_id="policy-a",
                version=1,
                name="Critical alerts",
                state="ACTIVE",
                priority=1,
                matchers_json="[]",
                scope_mode="ALL",
                source_ids_json="[]",
                repeat_interval_seconds=300,
                created_at=now,
                updated_at=now,
                activated_at=now,
            )
        )

    with TestClient(resources.app) as client:
        body = client.get("/api/v1/platform-health").json()

    assert body["notifications"] == {
        "status": "degraded",
        "worker_last_run_at": body["notifications"]["worker_last_run_at"],
        "pending": 0,
        "retrying": 0,
        "permanent_failed_24h": 0,
        "oldest_pending_seconds": None,
        "active_channels": 0,
        "active_policies": 1,
        "last_error_code": "CHANNEL_UNAVAILABLE",
    }
    assert body["readiness"]["status"] == "ready"
    resources.engine.dispose()


def test_platform_health_projection_is_bounded_for_many_sources(tmp_path: Path) -> None:
    resources = _candidate(tmp_path)
    sessions = create_session_factory(resources.engine)
    observed = datetime(2026, 9, 9, tzinfo=UTC).replace(tzinfo=None)
    with sessions.begin() as session:
        for index in range(200):
            source_id = f"source-{index:03d}"
            session.add(
                SourceRecord(
                    id=source_id,
                    name=f"Source {index:03d}",
                    state="ENABLED",
                    version=1,
                    poll_interval_seconds=30,
                    resolution_grace_seconds=60,
                    max_parallel_endpoints=2,
                    watchdog_enabled=False,
                    watchdog_alertname="Watchdog",
                    watchdog_identity_label="cluster",
                    watchdog_missing_after_seconds=300,
                    created_at=observed,
                    updated_at=observed,
                )
            )
        session.flush()
        for index in range(200):
            source_id = f"source-{index:03d}"
            session.add(
                PollRunRecord(
                    source_id=source_id,
                    source_version=1,
                    started_at=observed,
                    finished_at=observed,
                    completeness="COMPLETE",
                    endpoint_total=2,
                    endpoint_succeeded=2,
                    alert_count=0,
                    safe_error_codes_json="[]",
                )
            )

    with TestClient(resources.app) as client:
        started = time.monotonic()
        response = client.get("/api/v1/platform-health")
        elapsed = time.monotonic() - started

    assert response.status_code == 200
    assert len(response.json()["sources"]) == 200
    assert elapsed < 2.5
    resources.engine.dispose()
