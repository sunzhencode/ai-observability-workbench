"""Watchdog is a per-cluster pipeline-health signal, never an Incident."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

from app.api import health, incidents
from app.db import get_session
from app.models import Alert, Incident
from app.services.ingest import ingest_alerts
from app.services.source_identity import active_source_id
from app.state import poll_status


def _watchdog(fingerprint: str, cluster: str | None) -> dict:
    labels = {
        "alertname": "Watchdog",
        "severity": "none",
    }
    if cluster is not None:
        labels["cluster"] = cluster
    return {
        "fingerprint": fingerprint,
        "labels": labels,
        "annotations": {"summary": "Monitoring pipeline heartbeat"},
        "startsAt": "2026-07-16T00:00:00Z",
        "endsAt": "0001-01-01T00:00:00Z",
    }


def _client(session) -> TestClient:
    app = FastAPI()
    app.include_router(health.router, prefix="/api")
    app.include_router(incidents.router, prefix="/api")
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def _record_success(result) -> None:
    poll_status.last_poll_at = result.poll_time
    poll_status.last_poll_ok = True
    poll_status.last_poll_error = None
    poll_status.source_id = active_source_id()
    poll_status.watchdog_current_clusters = set(result.watchdog_clusters_seen)


def test_watchdog_is_cached_but_never_grouped(session) -> None:
    now = datetime(2026, 7, 16, 10, 0, tzinfo=timezone.utc)
    result = ingest_alerts(
        session,
        [
            _watchdog("watchdog-prod-a-1", "prod-a"),
            _watchdog("watchdog-prod-a-2", "prod-a"),
            _watchdog("watchdog-prod-b", "prod-b"),
        ],
        poll_time=now,
    )

    alerts = session.exec(select(Alert).order_by(Alert.fingerprint)).all()
    assert len(alerts) == 3
    assert all(alert.alertname == "Watchdog" for alert in alerts)
    assert all(alert.incident_id is None for alert in alerts)
    assert session.exec(select(Incident)).all() == []
    assert result.watchdog_clusters_seen == {"prod-a": now, "prod-b": now}
    assert result.incidents_touched == 0


def test_watchdog_health_deduplicates_clusters_and_marks_partial_missing(session) -> None:
    client = _client(session)
    first_poll = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=5)
    first = ingest_alerts(
        session,
        [
            _watchdog("watchdog-prod-a-1", "prod-a"),
            _watchdog("watchdog-prod-a-2", "prod-a"),
            _watchdog("watchdog-prod-b", "prod-b"),
        ],
        poll_time=first_poll,
    )
    _record_success(first)

    healthy = client.get("/api/health").json()
    assert healthy["watchdog"]["overall_status"] == "healthy"
    assert healthy["watchdog"]["summary"] == {
        "total": 2,
        "healthy": 2,
        "missing": 0,
        "unknown": 0,
    }
    assert [item["cluster"] for item in healthy["watchdog"]["clusters"]] == [
        "prod-a",
        "prod-b",
    ]

    second_poll = first_poll + timedelta(minutes=5)
    second = ingest_alerts(
        session,
        [_watchdog("watchdog-prod-a-1", "prod-a")],
        poll_time=second_poll,
    )
    _record_success(second)

    partial = client.get("/api/health").json()
    assert partial["watchdog"]["overall_status"] == "missing"
    assert partial["watchdog"]["summary"] == {
        "total": 2,
        "healthy": 1,
        "missing": 1,
        "unknown": 0,
    }
    assert [
        (item["cluster"], item["status"])
        for item in partial["watchdog"]["clusters"]
    ] == [("prod-b", "missing"), ("prod-a", "healthy")]


def test_watchdog_source_failure_marks_all_known_clusters_unknown(session) -> None:
    client = _client(session)
    seen_at = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=5)
    result = ingest_alerts(
        session,
        [
            _watchdog("watchdog-prod-a", "prod-a"),
            _watchdog("watchdog-prod-b", "prod-b"),
        ],
        poll_time=seen_at,
    )
    _record_success(result)

    poll_status.last_poll_at = seen_at + timedelta(minutes=5)
    poll_status.last_poll_ok = False
    poll_status.last_poll_error = "Alertmanager timeout"

    unknown = client.get("/api/health").json()["watchdog"]
    assert unknown["overall_status"] == "unknown"
    assert unknown["summary"] == {
        "total": 2,
        "healthy": 0,
        "missing": 0,
        "unknown": 2,
    }
    assert {item["status"] for item in unknown["clusters"]} == {"unknown"}


def test_watchdog_no_data_and_missing_cluster_are_explicit(session) -> None:
    client = _client(session)
    poll_status.last_poll_at = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=5)
    poll_status.last_poll_ok = True
    poll_status.last_poll_error = None
    poll_status.watchdog_current_clusters = set()

    no_data = client.get("/api/health").json()["watchdog"]
    assert no_data["overall_status"] == "no_data"
    assert no_data["summary"] == {
        "total": 0,
        "healthy": 0,
        "missing": 0,
        "unknown": 0,
    }
    assert no_data["clusters"] == []

    next_poll = datetime.now(timezone.utc).replace(microsecond=0)
    result = ingest_alerts(
        session,
        [_watchdog("watchdog-no-cluster", None)],
        poll_time=next_poll,
    )
    _record_success(result)

    explicit = client.get("/api/health").json()["watchdog"]
    assert explicit["summary"]["total"] == 1
    assert explicit["clusters"][0]["cluster"] == "<no-cluster>"
    assert explicit["clusters"][0]["status"] == "healthy"


def test_migrated_watchdog_incident_is_hidden_from_operational_views(session) -> None:
    client = _client(session)
    seen_at = datetime.now(timezone.utc).replace(microsecond=0)
    result = ingest_alerts(
        session,
        [_watchdog("watchdog-prod-a", "prod-a")],
        poll_time=seen_at,
    )
    _record_success(result)
    session.add(
        Incident(
            group_key="env=prod|alertname=Watchdog|cluster=prod-a",
            title="Watchdog · prod-a",
            source_state="recovered",
        )
    )
    session.commit()

    body = client.get("/api/health").json()
    assert body["incident_count"] == 0
    assert client.get("/api/incidents").json() == []
