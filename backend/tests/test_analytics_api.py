"""HTTP contract for the read-only deterministic Analytics overview."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.adapters.persistence.analytics import SqlAlchemyAnalyticsStore
from app.api.v1.analytics import create_analytics_router
from app.platform.persistence.database import (
    SqliteDatabaseConfig,
    create_session_factory,
    create_sqlite_engine,
)
from app.platform.persistence.migrations import upgrade_database


UTC = timezone.utc


def _client(tmp_path: Path) -> tuple[TestClient, SqlAlchemyAnalyticsStore]:
    engine = create_sqlite_engine(SqliteDatabaseConfig(path=tmp_path / "api.db"))
    upgrade_database(engine)
    store = SqlAlchemyAnalyticsStore(create_session_factory(engine))
    app = FastAPI()
    app.include_router(
        create_analytics_router(
            store=store,
            now=lambda: datetime(2026, 9, 8, 18, 47, tzinfo=UTC),
        )
    )
    return TestClient(app), store


def test_overview_defaults_to_7d_and_returns_explicit_pending_snapshot(
    tmp_path: Path,
) -> None:
    client, _ = _client(tmp_path)

    response = client.get("/api/v1/analytics/overview")

    assert response.status_code == 200
    body = response.json()
    assert body["freshness"] == "ROLLUP_PENDING"
    assert body["range"] == "7d"
    assert body["from_utc"] == "2026-09-01T18:00:00Z"
    assert body["to_utc"] == "2026-09-08T18:00:00Z"
    assert body["signal"]["compression"] == {
        "numerator": 0,
        "denominator": 0,
        "ratio": None,
    }


def test_filters_are_echoed_and_invalid_values_are_rejected(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    store.refresh(now=datetime(2026, 9, 8, 18, 47, tzinfo=UTC))

    response = client.get(
        "/api/v1/analytics/overview",
        params={
            "range": "24h",
            "source_id": "source-a",
            "service_id": 7,
            "signal_severity": "warning",
        },
    )

    assert response.status_code == 200
    assert response.json()["freshness"] == "READY"
    assert response.json()["source_id"] == "source-a"
    assert response.json()["service_id"] == 7
    assert response.json()["signal_severity"] == "warning"
    assert client.get(
        "/api/v1/analytics/overview", params={"range": "90d"}
    ).status_code == 422
    assert client.get(
        "/api/v1/analytics/overview", params={"service_id": 0}
    ).status_code == 422
