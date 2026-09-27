"""API contract tests for local panel mappings and incident links."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlmodel import Session, create_engine

from app.api import incidents, panel_mappings
from app.db import get_session
from app.models import PanelMapping
from app.services.ingest import ingest_alerts


def make_client(session) -> TestClient:
    app = FastAPI()
    app.include_router(incidents.router, prefix="/api")
    app.include_router(panel_mappings.router, prefix="/api")
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def test_mapping_put_get_sort_replace_and_delete(session) -> None:
    client = make_client(session)

    assert client.get("/api/panel-mappings").json() == {"mappings": []}

    target = client.put(
        "/api/panel-mappings",
        json={"alertname": "TargetDown", "links": ["https://grafana.example/d/target"]},
    )
    crash = client.put(
        "/api/panel-mappings",
        json={
            "alertname": "KubePodCrashLooping",
            "links": [
                " https://grafana.example/d/crash ",
                "https://grafana.example/d/crash",
            ],
        },
    )
    assert target.status_code == 200
    assert crash.status_code == 200
    assert crash.json() == {
        "alertname": "KubePodCrashLooping",
        "links": ["https://grafana.example/d/crash"],
    }

    assert client.get("/api/panel-mappings").json() == {
        "mappings": [
            {
                "alertname": "KubePodCrashLooping",
                "links": ["https://grafana.example/d/crash"],
            },
            {"alertname": "TargetDown", "links": ["https://grafana.example/d/target"]},
        ]
    }

    replaced = client.put(
        "/api/panel-mappings",
        json={"alertname": "TargetDown", "links": ["https://grafana.example/d/new"]},
    )
    assert replaced.json()["links"] == ["https://grafana.example/d/new"]

    deleted = client.put(
        "/api/panel-mappings", json={"alertname": "TargetDown", "links": []}
    )
    assert deleted.status_code == 204
    assert client.get("/api/panel-mappings").json() == {
        "mappings": [
            {
                "alertname": "KubePodCrashLooping",
                "links": ["https://grafana.example/d/crash"],
            }
        ]
    }


def test_mapping_put_rejects_non_http_url(session) -> None:
    client = make_client(session)
    response = client.put(
        "/api/panel-mappings",
        json={"alertname": "TargetDown", "links": ["javascript:alert(1)"]},
    )
    assert response.status_code == 422
    assert client.get("/api/panel-mappings").json() == {"mappings": []}


def test_legacy_mapping_is_retained_but_not_used_by_incident_detail(session) -> None:
    client = make_client(session)
    ingest_alerts(
        session,
        [
            {
                "fingerprint": "target-1",
                "labels": {
                    "alertname": "TargetDown",
                    "severity": "warning",
                    "cluster": "cluster-a",
                },
                "annotations": {},
                "startsAt": "2026-07-16T00:00:00Z",
            }
        ],
    )
    client.put(
        "/api/panel-mappings",
        json={"alertname": "TargetDown", "links": ["https://grafana.example/d/target"]},
    )

    incident_id = client.get("/api/incidents").json()[0]["id"]
    detail = client.get(f"/api/incidents/{incident_id}")

    assert detail.status_code == 200
    assert client.get("/api/panel-mappings").json()["mappings"] == [
        {
            "alertname": "TargetDown",
            "links": ["https://grafana.example/d/target"],
        }
    ]
    assert "panel_links" not in detail.json()


def test_existing_row_per_link_schema_remains_readable(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'existing.db'}")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE panelmapping (
                    id INTEGER NOT NULL PRIMARY KEY,
                    alertname VARCHAR NOT NULL,
                    url VARCHAR NOT NULL,
                    label VARCHAR NOT NULL,
                    created_at DATETIME NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO panelmapping (id, alertname, url, label, created_at)
                VALUES (1, 'TargetDown', 'https://grafana.example/d/target',
                        'Target health', '2026-07-16 00:00:00')
                """
            )
        )

    with Session(engine) as session:
        client = make_client(session)
        assert client.get("/api/panel-mappings").json() == {
            "mappings": [
                {"alertname": "TargetDown", "links": ["https://grafana.example/d/target"]}
            ]
        }
        row = session.get(PanelMapping, 1)
        assert row is not None
        assert row.label == "Target health"
