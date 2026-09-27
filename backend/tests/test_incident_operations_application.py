"""The incident-operations composition remains isolated from the current product."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.bootstrap import create_job_platform_app, create_platform_app


def test_candidate_has_job_api_and_operational_readiness_only(
    tmp_path: Path,
) -> None:
    key = tmp_path / "master.key"
    key.write_text("existing-test-key\n", encoding="utf-8")
    resources = create_job_platform_app(
        database_path=tmp_path / "incident-operations.db",
        master_key_path=key,
        cursor_secret=b"candidate-cursor-key-at-least-32-bytes",
    )

    with TestClient(resources.app) as client:
        ready = client.get("/health/ready")
        metrics = client.get("/metrics").text
        paths = resources.app.openapi()["paths"]

    assert ready.status_code == 200
    assert [item["name"] for item in ready.json()["checks"]] == [
        "database",
        "migration",
        "disk",
        "master_key",
        "scheduler",
        "job_runner",
    ]
    assert "/api/v1/jobs/{job_id}" in paths
    assert "/api/v1/events" in paths
    assert "/api/v1/events/poll" in paths
    assert "/api/incidents" not in paths
    assert 'incident_operations_local_operational_alert{code="SCHEDULER_DELAY"} 0' in metrics
    assert 'incident_operations_local_operational_alert{code="JOB_RUNNER_HEARTBEAT_STALE"} 0' in metrics
    assert 'incident_operations_local_operational_alert{code="JOB_BACKLOG_HIGH"} 0' in metrics
    assert 'incident_operations_local_operational_alert{code="JOB_LEASE_EXPIRED"} 0' in metrics


def test_candidate_does_not_replace_or_mutate_current_default_app(
    tmp_path: Path,
) -> None:
    from app.main import app as current_app

    key = tmp_path / "master.key"
    key.write_text("existing-test-key\n", encoding="utf-8")
    candidate = create_job_platform_app(
        database_path=tmp_path / "candidate.db",
        master_key_path=key,
        cursor_secret=b"candidate-cursor-key-at-least-32-bytes",
    )

    assert candidate.app is not current_app
    assert "/api/incidents" in current_app.openapi()["paths"]
    assert "/api/incidents" not in candidate.app.openapi()["paths"]
    assert create_platform_app().openapi()["paths"] == {}
