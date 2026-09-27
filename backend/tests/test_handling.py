"""Handling-state transition and audit tests."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import incidents
from app.db import get_session
from app.services.ingest import ingest_alerts


def make_client(session) -> TestClient:
    app = FastAPI()
    app.include_router(incidents.router, prefix="/api")
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def create_incident(client: TestClient, session, sample_alerts) -> int:
    ingest_alerts(session, [sample_alerts[2]])
    return client.get("/api/incidents").json()[0]["id"]


def test_handling_transitions_are_audited_in_detail(session, sample_alerts) -> None:
    client = make_client(session)
    incident_id = create_incident(client, session, sample_alerts)

    started = client.patch(
        f"/api/incidents/{incident_id}/handling",
        json={"state": "IN_PROGRESS", "reason": "Investigating target loss"},
    )
    assert started.status_code == 200
    assert started.json()["actor"] == "local-user"
    assert started.json()["from_state"] == "NEW"
    assert started.json()["to_state"] == "IN_PROGRESS"
    assert started.json()["created_at"].endswith("Z")

    closed = client.patch(
        f"/api/incidents/{incident_id}/handling",
        json={"state": "CLOSED", "reason": "Exporter restored", "actor": "zhensun"},
    )
    assert closed.status_code == 200
    assert closed.json()["actor"] == "zhensun"

    detail = client.get(f"/api/incidents/{incident_id}").json()
    assert detail["handling_state"] == "CLOSED"
    assert [item["to_state"] for item in detail["handling_history"]] == [
        "IN_PROGRESS",
        "CLOSED",
    ]


def test_terminal_and_noop_transitions_are_rejected_without_audit(
    session, sample_alerts
) -> None:
    client = make_client(session)
    incident_id = create_incident(client, session, sample_alerts)
    client.patch(
        f"/api/incidents/{incident_id}/handling",
        json={"state": "FALSE_POSITIVE", "reason": "Known noisy rule"},
    )

    invalid = client.patch(
        f"/api/incidents/{incident_id}/handling",
        json={"state": "IN_PROGRESS", "reason": "Try to reopen"},
    )
    assert invalid.status_code == 409

    detail = client.get(f"/api/incidents/{incident_id}").json()
    assert detail["handling_state"] == "FALSE_POSITIVE"
    assert len(detail["handling_history"]) == 1


def test_handling_requires_non_empty_reason(session, sample_alerts) -> None:
    client = make_client(session)
    incident_id = create_incident(client, session, sample_alerts)
    response = client.patch(
        f"/api/incidents/{incident_id}/handling",
        json={"state": "IN_PROGRESS", "reason": "   "},
    )
    assert response.status_code == 422
    assert client.get(f"/api/incidents/{incident_id}").json()["handling_history"] == []


def test_source_recovery_does_not_close_handling_state(session, sample_alerts) -> None:
    client = make_client(session)
    incident_id = create_incident(client, session, sample_alerts)
    client.patch(
        f"/api/incidents/{incident_id}/handling",
        json={"state": "IN_PROGRESS", "reason": "Still verifying recovery"},
    )

    ingest_alerts(session, [], resolution_grace_seconds=0)
    ingest_alerts(session, [], resolution_grace_seconds=0)
    detail = client.get(f"/api/incidents/{incident_id}").json()

    assert detail["source_state"] == "recovered"
    assert detail["handling_state"] == "IN_PROGRESS"


def test_handling_incident_not_found(session) -> None:
    client = make_client(session)
    response = client.patch(
        "/api/incidents/999/handling",
        json={"state": "IN_PROGRESS", "reason": "Investigating"},
    )
    assert response.status_code == 404
