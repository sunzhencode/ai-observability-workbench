"""Deterministic metric, Grafana and model-channel contracts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.domains.metrics.models import (
    MAX_QUERIES_PER_ALERT,
    MAX_RANGE_SECONDS,
    MAX_SERIES_PER_QUERY,
    MetricReadStatus,
    MetricTemplate,
    QueryBudgetExceeded,
    QueryWindow,
    backfill_window,
    classify_read,
    expr_from_generator_url,
    extract_grafana_candidates,
    grafana_deep_link,
    render_metric_template,
    reconstruct_alerts,
    select_templates,
    strip_numeric_comparison,
)
from app.domains.model_channels.models import (
    ChannelState,
    ModelChannelDraft,
    ModelKind,
    activate_revision,
)

UTC = timezone.utc


def test_metric_budget_refuses_before_read_and_never_slices_series() -> None:
    now = datetime(2026, 8, 12, tzinfo=UTC)
    QueryWindow(now - timedelta(seconds=MAX_RANGE_SECONDS), now, 60).validate()
    with pytest.raises(QueryBudgetExceeded, match="METRIC_RANGE_TOO_LONG"):
        QueryWindow(now - timedelta(seconds=MAX_RANGE_SECONDS + 1), now, 60).validate()
    with pytest.raises(QueryBudgetExceeded, match="METRIC_TOO_MANY_QUERIES"):
        QueryWindow(now - timedelta(minutes=5), now, 15).validate(
            query_count=MAX_QUERIES_PER_ALERT + 1
        )
    with pytest.raises(QueryBudgetExceeded, match="METRIC_TOO_MANY_SERIES"):
        classify_read(
            {
                "resultType": "matrix",
                "result": [
                    {"metric": {"i": str(index)}, "values": [[1, "1"]]}
                    for index in range(MAX_SERIES_PER_QUERY + 1)
                ],
            }
        )


def test_empty_data_and_source_unavailable_are_distinct_types() -> None:
    assert classify_read({"resultType": "matrix", "result": []}).status is (
        MetricReadStatus.EMPTY_NO_DATA
    )
    assert MetricReadStatus.SOURCE_UNAVAILABLE is not MetricReadStatus.EMPTY_NO_DATA
    assert MetricReadStatus.SOURCE_UNAVAILABLE.value != MetricReadStatus.EMPTY_NO_DATA.value


def test_backfill_window_and_reconstruction_are_bounded_and_stable() -> None:
    now = datetime(2026, 8, 12, tzinfo=UTC)
    window = backfill_window(now, requested_hours=240, hard_limit_hours=168)
    assert window.effective_hours == 168 and window.truncated is True
    data = {
        "resultType": "matrix",
        "result": [
            {
                "metric": {
                    "__name__": "ALERTS",
                    "alertstate": "firing",
                    "alertname": "TargetDown",
                    "severity": "warning",
                },
                "values": [[1_786_496_460, "1"], [1_786_496_520, "1"]],
            }
        ],
    }
    first = reconstruct_alerts(data)
    assert first == reconstruct_alerts(data)
    assert first[0]["fingerprint"].startswith("backfill-")
    assert first[0]["labels"] == {
        "alertname": "TargetDown",
        "severity": "warning",
    }


def test_template_selection_is_source_scoped_priority_ordered_and_bounded() -> None:
    templates = tuple(
        MetricTemplate(
            id=index,
            name=f"metric-{index}",
            promql="up",
            priority=20 - index,
            enabled=True,
            source_ids=("src-a",) if index % 2 else (),
            origin_kind="MANUAL",
        )
        for index in range(1, 12)
    )
    selected = select_templates(templates, source_id="src-a")
    assert len(selected) == MAX_QUERIES_PER_ALERT - 1
    assert list(selected) == sorted(selected, key=lambda item: (item.priority, item.id))
    assert all(not item.source_ids or "src-a" in item.source_ids for item in selected)


def test_grafana_parser_keeps_imported_promql_before_binding_and_no_baseline() -> None:
    dashboard = {
        "title": "Kubernetes",
        "panels": [
            {
                "id": 7,
                "title": "CPU",
                "datasource": {"type": "prometheus"},
                "targets": [
                    {
                        "refId": "A",
                        "expr": 'rate(container_cpu_usage_seconds_total{namespace="$namespace"}[$__rate_interval])',
                        "legendFormat": "{{pod}}",
                    }
                ],
                "fieldConfig": {"defaults": {"unit": "percentunit"}},
            }
        ],
    }
    candidates = extract_grafana_candidates(dashboard, dashboard_uid="kube-main")
    assert len(candidates) == 1
    candidate = candidates[0]
    assert "$namespace" in candidate.imported_promql
    assert "$__rate_interval" not in candidate.imported_promql
    assert candidate.required_variables == ("namespace",)
    assert candidate.legend_format == "{{pod}}"
    assert candidate.unit == "percentunit"
    assert candidate.status == "NEEDS_DECISION"
    assert not hasattr(candidate, "baseline")


def test_grafana_parser_keeps_unsupported_candidates_with_safe_reasons() -> None:
    dashboard = {
        "title": "Node health",
        "panels": [
            {
                "id": 1,
                "type": "timeseries",
                "title": "Logs",
                "datasource": {"type": "loki"},
                "targets": [{"refId": "A", "expr": '{job="node"}'}],
            },
            {
                "id": 2,
                "type": "table",
                "title": "Top consumers",
                "datasource": {"type": "prometheus"},
                "targets": [{"refId": "A", "expr": "topk(5, cpu_usage)"}],
            },
            {
                "id": 3,
                "type": "timeseries",
                "title": "Memory",
                "datasource": {"type": "prometheus"},
                "targets": [
                    {"refId": "A", "expr": 'memory_usage{instance="$instance"}'}
                ],
            },
        ],
    }

    candidates = extract_grafana_candidates(dashboard, dashboard_uid="node-health")

    assert [item.status for item in candidates] == [
        "UNSUPPORTED",
        "UNSUPPORTED",
        "NEEDS_DECISION",
    ]
    assert "loki" in candidates[0].reason
    assert "table" in candidates[1].reason


def test_template_render_generator_fallback_and_deep_link_are_bounded() -> None:
    rendered = render_metric_template(
        'up{cluster="{{cluster}}"}',
        {"cluster": 'prod"} or on() vector(1) #'},
        required_labels=("cluster",),
    )
    assert rendered == 'up{cluster="prod\\"} or on() vector(1) #"}'
    assert render_metric_template(
        'up{cluster="{{cluster}}"}', {}, required_labels=("cluster",)
    ) is None
    assert (
        expr_from_generator_url(
            "http://prometheus.internal/graph?g0.expr=rate%28cpu_total%5B5m%5D%29%20%3E%200.5"
        )
        == "rate(cpu_total[5m]) > 0.5"
    )
    assert strip_numeric_comparison("rate(cpu_total[5m]) > 0.5") == "rate(cpu_total[5m])"
    assert strip_numeric_comparison("a > b") is None
    assert grafana_deep_link(
        "http://grafana.internal/",
        dashboard_uid="kube-main",
        panel_id=7,
        start_ms=1000,
        end_ms=2000,
    ) == "http://grafana.internal/d/kube-main?viewPanel=7&from=1000&to=2000"


def test_model_channel_kind_is_immutable_and_activation_does_not_require_test() -> None:
    draft = ModelChannelDraft(
        channel_id="model-a",
        kind=ModelKind.OPENAI_COMPATIBLE,
        name="Primary model",
        base_url="https://models.example.invalid/v1",
        model="gpt-compatible",
        state=ChannelState.DRAFT,
        tested_ok=False,
    )
    active = activate_revision(draft)
    assert active.state is ChannelState.ACTIVE
    with pytest.raises(ValueError, match="MODEL_KIND_IMMUTABLE"):
        active.with_update(kind=ModelKind.LOCAL_LOOPBACK)
