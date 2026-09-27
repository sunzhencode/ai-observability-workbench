"""REST contract for standalone aggregation rule management."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

from app.api import aggregation_rules
from app.db import get_session
from app.models import AggregationRule
from app.services.ingest import ingest_alerts


class UnconfiguredThanos:
    configured = False


def make_client(session) -> TestClient:
    app = FastAPI()
    app.include_router(aggregation_rules.router, prefix="/api")
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[aggregation_rules.get_thanos_client] = lambda: UnconfiguredThanos()
    return TestClient(app)


def _payload(**overrides):
    payload = {
        "name": "CrashLoop by namespace",
        "priority": 10,
        "enabled": True,
        "matchers": [
            {"label": "alertname", "operator": "=", "value": "KubePodCrashLooping"}
        ],
        "group_by_labels": ["cluster", "namespace"],
    }
    payload.update(overrides)
    return payload


def test_create_list_preview_and_update_rule(session, sample_alerts) -> None:
    ingest_alerts(session, sample_alerts)
    client = make_client(session)

    preview = client.post("/api/aggregation-rules/preview", json=_payload())
    assert preview.status_code == 200
    assert preview.json()["selected_alert_count"] == 2
    assert session.exec(select(AggregationRule)).all() == []

    created = client.post("/api/aggregation-rules", json=_payload())
    assert created.status_code == 201
    body = created.json()
    assert body["id"] > 0
    assert body["version"] == 1
    assert body["matchers"][0]["label"] == "alertname"

    listing = client.get("/api/aggregation-rules")
    assert listing.status_code == 200
    assert listing.json() == [body]

    updated = client.put(
        f"/api/aggregation-rules/{body['id']}",
        json=_payload(name="CrashLoop by cluster", enabled=False, group_by_labels=["cluster"]),
    )
    assert updated.status_code == 200
    assert updated.json()["version"] == 2
    assert updated.json()["enabled"] is False
    assert updated.json()["group_by_labels"] == ["cluster"]


def test_rule_validation_and_duplicate_name_return_422_or_409(session) -> None:
    client = make_client(session)
    bad = client.post(
        "/api/aggregation-rules",
        json=_payload(matchers=[{"label": "pod", "operator": "=~", "value": "["}]),
    )
    assert bad.status_code == 422

    assert client.post("/api/aggregation-rules", json=_payload()).status_code == 201
    duplicate = client.post("/api/aggregation-rules", json=_payload())
    assert duplicate.status_code == 409


def test_global_label_catalog_is_not_scoped_to_an_alertname(session, sample_alerts) -> None:
    ingest_alerts(session, sample_alerts)
    client = make_client(session)

    response = client.get("/api/aggregation-labels?lookback_hours=168")
    assert response.status_code == 200
    body = response.json()
    assert body["history_status"] == "unconfigured"
    labels = {item["name"]: item for item in body["labels"]}
    assert "alertname" in labels
    assert set(labels["alertname"]["sample_values"]) >= {
        "KubePodCrashLooping",
        "TargetDown",
    }
    assert "cluster" in labels
