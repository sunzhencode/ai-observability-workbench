"""Dynamic label discovery from current alerts and bounded Thanos ALERTS series."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from app.models import Alert
from app.services.discovery import summarize_alert_labels, summarize_global_labels
from app.sources.thanos import ThanosClient


def test_discovery_combines_current_and_history_with_stable_stats() -> None:
    current = [
        Alert(
            fingerprint="a",
            alertname="PodFailure",
            labels={
                "alertname": "PodFailure",
                "cluster": "prod-a",
                "environment": "legacy-prod",
                "source_id": "legacy-source",
                "namespace": "payments",
            },
        ),
        Alert(
            fingerprint="b",
            alertname="PodFailure",
            labels={"alertname": "PodFailure", "cluster": "prod-b"},
        ),
        Alert(
            fingerprint="watchdog",
            alertname="Watchdog",
            labels={"alertname": "Watchdog", "cluster": "prod-a"},
        ),
    ]
    history = [
        {
            "__name__": "ALERTS",
            "alertname": "PodFailure",
            "alertstate": "firing",
            "cluster": "prod-a",
            "namespace": "checkout",
        }
    ]

    result = summarize_alert_labels(current, history)

    assert [item.alertname for item in result] == ["PodFailure"]
    labels = {item.name: item for item in result[0].labels}
    assert labels["cluster"].coverage == 1.0
    assert labels["cluster"].distinct_count == 2
    assert labels["cluster"].sample_values == ["prod-a", "prod-b"]
    assert labels["cluster"].sources == ["current", "history"]
    assert labels["namespace"].coverage == pytest.approx(2 / 3)
    assert labels["namespace"].sources == ["current", "history"]


def test_global_catalog_excludes_grafana_datasource_metadata() -> None:
    current = [
        Alert(
            fingerprint="a",
            alertname="PodFailure",
            labels={
                "alertname": "PodFailure",
                "cluster": "prod-a",
                "__datasource_uid__": "prometheus-main",
                "__datasource_type__": "prometheus",
            },
        )
    ]

    labels = {
        item.name
        for item in summarize_global_labels(
            current,
            [
                {
                    "__name__": "ALERTS",
                    "alertname": "PodFailure",
                    "cluster": "prod-b",
                    "environment": "legacy-prod",
                    "source_id": "legacy-source",
                    "__datasource_uid__": "thanos-main",
                }
            ],
        )
    }

    assert "cluster" in labels
    assert "alertname" in labels
    assert "__datasource_uid__" not in labels
    assert "__datasource_type__" not in labels
    assert "environment" not in labels
    assert "source_id" not in labels


@pytest.mark.asyncio
async def test_thanos_series_discovery_is_bounded_to_alerts_and_get() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["matcher"] = request.url.params["match[]"]
        captured["start"] = request.url.params["start"]
        captured["end"] = request.url.params["end"]
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": [{"__name__": "ALERTS", "alertname": "PodFailure"}],
            },
        )

    client = ThanosClient(
        base_url="https://thanos.example",
        transport=httpx.MockTransport(handler),
    )
    end = datetime(2026, 7, 16, 10, 0, tzinfo=timezone.utc)
    rows = await client.series_alerts(start=end, end=end, limit=5000)

    assert rows == [{"__name__": "ALERTS", "alertname": "PodFailure"}]
    assert captured["method"] == "GET"
    assert captured["path"] == "/api/v1/series"
    assert captured["matcher"] == 'ALERTS{alertname!=""}'
