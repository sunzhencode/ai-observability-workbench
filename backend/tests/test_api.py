"""API tests using an in-memory database and offline fixtures."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import alerts, health, incidents
from app.db import get_session
from app.services.ingest import ingest_alerts
from app.services.aggregation_rules import create_aggregation_rule


def configure_sample_grouping(session) -> None:
    create_aggregation_rule(
        session,
        name="CrashLoop by namespace",
        priority=10,
        enabled=True,
        matchers=[
            {"label": "alertname", "operator": "=", "value": "KubePodCrashLooping"}
        ],
        group_by_labels=["cluster", "namespace"],
    )


def make_client(session) -> TestClient:
    app = FastAPI()
    app.include_router(health.router, prefix="/api")
    app.include_router(incidents.router, prefix="/api")
    app.include_router(alerts.router, prefix="/api")
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def test_list_incidents_sorted_by_severity(session, sample_alerts):
    configure_sample_grouping(session)
    ingest_alerts(session, sample_alerts)
    client = make_client(session)

    resp = client.get("/api/incidents")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 3
    assert all("environment" not in item for item in data)
    # Critical incident should be first.
    assert data[0]["severity"] == "critical"
    assert data[0]["member_count"] == 2
    assert data[0]["aggregation_rule_name"] == "CrashLoop by namespace"
    assert data[0]["aggregation_status"] == "matched"
    severities = [i["severity"] for i in data]
    assert severities == ["critical", "warning", "info"]


def test_incident_detail_returns_members(session, sample_alerts):
    configure_sample_grouping(session)
    ingest_alerts(session, sample_alerts)
    client = make_client(session)

    listing = client.get("/api/incidents").json()
    crit_id = listing[0]["id"]

    detail = client.get(f"/api/incidents/{crit_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["member_count"] == 2
    assert len(body["members"]) == 2
    assert body["grouping_explanation"]
    assert body["aggregation_rule_name"] == "CrashLoop by namespace"
    assert "panel_links" not in body
    assert "environment" not in body
    assert all("environment" not in member for member in body["members"])


def test_incident_not_found(session):
    client = make_client(session)
    assert client.get("/api/incidents/999").status_code == 404


def test_health_reports_incident_count(session, sample_alerts):
    configure_sample_grouping(session)
    ingest_alerts(session, sample_alerts)
    client = make_client(session)
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert "environment" not in body
    assert body["incident_count"] == 3
