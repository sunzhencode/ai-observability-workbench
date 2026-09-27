"""Thanos ALERTS reconstruction, budget, and backfill-ingest tests."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app import main
from app.registry_models import F20Model
from app.models import Alert, Incident
from app.services.backfill import backfill_window, thanos_matrix_to_alerts
from app.services.ingest import ingest_alerts
from app.sources.thanos import ThanosClient
from app.services.thanos_history import ThanosConnection


@pytest.fixture
def thanos_matrix() -> dict:
    from tests.conftest import FIXTURES

    with open(FIXTURES / "thanos_alerts_matrix.json", encoding="utf-8") as handle:
        return json.load(handle)


def test_backfill_window_clamps_to_seven_days() -> None:
    now = datetime(2026, 7, 16, 0, 0, tzinfo=timezone.utc)
    window = backfill_window(now, requested_hours=240, hard_limit_hours=168)

    assert window.effective_hours == 168
    assert window.start == datetime(2026, 7, 9, 0, 0, tzinfo=timezone.utc)
    assert window.end == now
    assert window.truncated_reason == "requested 240h exceeds hard limit 168h"


def test_thanos_matrix_reconstructs_stable_alert(thanos_matrix) -> None:
    first = thanos_matrix_to_alerts(thanos_matrix)
    second = thanos_matrix_to_alerts(thanos_matrix)

    assert first == second
    assert len(first) == 1
    alert = first[0]
    assert alert["fingerprint"].startswith("backfill-")
    assert alert["labels"] == {
        "alertname": "TargetDown",
        "severity": "warning",
        "cluster": "cluster-a",
        "job": "node-exporter",
    }
    assert alert["startsAt"] == "2026-07-16T00:01:00Z"
    assert alert["endsAt"] == "2026-07-16T00:02:00Z"
    assert alert["annotations"] == {}


def test_backfill_ingest_marks_reconstructed_and_is_idempotent(session, thanos_matrix) -> None:
    raw_alerts = thanos_matrix_to_alerts(thanos_matrix)
    ingest_alerts(
        session,
        raw_alerts,
        origin="backfill",
        evidence_completeness="reconstructed",
        reconcile_lifecycle=False,
    )
    ingest_alerts(
        session,
        raw_alerts,
        origin="backfill",
        evidence_completeness="reconstructed",
        reconcile_lifecycle=False,
    )

    alerts = session.exec(select(Alert)).all()
    incidents = session.exec(select(Incident)).all()
    assert len(alerts) == 1
    assert len(incidents) == 1
    assert alerts[0].origin == "backfill"
    assert alerts[0].evidence_completeness == "reconstructed"
    assert alerts[0].source_state == "resolved"
    assert incidents[0].source_state == "recovered"

    ingest_alerts(session, raw_alerts)
    alert = session.exec(select(Alert)).one()
    assert alert.origin == "live"
    assert alert.evidence_completeness == "complete"
    assert alert.source_state == "firing"


@pytest.mark.asyncio
async def test_thanos_client_uses_read_only_query_range(thanos_matrix) -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["query"] = str(request.url.params.get("query"))
        return httpx.Response(200, json=thanos_matrix)

    client = ThanosClient(
        base_url="https://thanos.example",
        token="viewer-token",
        transport=httpx.MockTransport(handler),
    )
    response = await client.query_alerts(
        start=datetime(2026, 7, 15, tzinfo=timezone.utc),
        end=datetime(2026, 7, 16, tzinfo=timezone.utc),
        step_seconds=60,
    )

    assert response == thanos_matrix
    assert captured == {
        "method": "GET",
        "path": "/api/v1/query_range",
        "query": 'ALERTS{alertstate=~"firing|pending"}',
    }


@pytest.mark.asyncio
async def test_backfill_isolates_one_source_failure_and_commits_other_history(
    tmp_path, monkeypatch, thanos_matrix
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'backfill-isolation.db'}")
    SQLModel.metadata.create_all(engine)
    F20Model.metadata.create_all(engine)
    connections = [
        ThanosConnection(
            source_id="src_bad",
            source_name="Bad",
            base_url="https://bad.invalid",
        ),
        ThanosConnection(
            source_id="src_good",
            source_name="Good",
            base_url="https://good.invalid",
        ),
    ]

    class FakeClient:
        def __init__(self, *, base_url: str, **_kwargs) -> None:
            self.base_url = base_url

        async def query_alerts(self, **_kwargs):
            if self.base_url == "https://bad.invalid":
                raise RuntimeError("remote detail must not leak")
            return thanos_matrix

    monkeypatch.setattr(main, "get_engine", lambda: engine)
    monkeypatch.setattr(
        main, "enabled_thanos_connections", lambda _session: connections
    )
    monkeypatch.setattr(main, "ThanosClient", FakeClient)

    await main.backfill_once()

    with Session(engine) as session:
        alert = session.exec(select(Alert)).one()
        assert alert.source_id == "src_good"
        assert alert.origin == "backfill"
    assert main.backfill_status.last_backfill_ok is False
    assert main.backfill_status.alerts_reconstructed == 1
    assert main.backfill_status.last_backfill_error == "src_bad:QUERY_FAILED"
