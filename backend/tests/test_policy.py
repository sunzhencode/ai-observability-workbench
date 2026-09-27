"""Grouping-policy API validation, versioning, and atomic regrouping tests."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

from app.api import incidents, policies
from app.db import get_session
from app.models import GroupingPolicy, Incident
from app.services.ingest import ingest_alerts


def make_client(session) -> TestClient:
    app = FastAPI()
    app.include_router(incidents.router, prefix="/api")
    app.include_router(policies.router, prefix="/api")
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def test_policy_get_and_put_regroups_existing_alerts(session, sample_alerts) -> None:
    ingest_alerts(session, sample_alerts)
    client = make_client(session)

    current = client.get("/api/policies")
    assert current.status_code == 200
    assert current.json()["version"] == 1
    assert current.json()["group_by"] == ["alertname", "cluster", "severity"]
    assert current.json()["enabled"] is True

    changed = client.put("/api/policies", json={"group_by": ["cluster"]})
    assert changed.status_code == 200
    assert changed.json()["version"] == 2
    assert changed.json()["group_by"] == ["cluster"]

    rows = session.exec(select(GroupingPolicy).order_by(GroupingPolicy.version)).all()
    assert [(row.version, row.enabled) for row in rows] == [(1, False), (2, True)]

    incidents_after = client.get("/api/incidents").json()
    assert len(incidents_after) == 2
    assert {item["member_count"] for item in incidents_after} == {1, 3}
    assert all(item.policy_version == 2 for item in session.exec(select(Incident)).all())


def test_policy_rejects_unknown_or_duplicate_fields_without_change(
    session, sample_alerts
) -> None:
    ingest_alerts(session, sample_alerts)
    client = make_client(session)

    duplicate = client.put(
        "/api/policies", json={"group_by": ["alertname", "alertname"]}
    )
    unknown = client.put("/api/policies", json={"group_by": ["namespace"]})
    empty = client.put("/api/policies", json={"group_by": []})

    assert duplicate.status_code == 422
    assert unknown.status_code == 422
    assert empty.status_code == 422
    assert client.get("/api/policies").json()["version"] == 1
    assert len(session.exec(select(GroupingPolicy)).all()) == 1
