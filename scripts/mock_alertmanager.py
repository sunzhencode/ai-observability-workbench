"""Bundled source mocks for local demos and notification acceptance.

Serves a small, time-relative set of active alerts at ``/api/v2/alerts`` so the
workbench can run end-to-end without access to the real internal network. The
active product demo uses Alertmanager and Thanos endpoints. Historical Grafana
read responses remain available for isolated client tests but are not wired to
alerts, Incidents, or aggregation rules.

``/__mock__/scenario`` mutates only this developer process' in-memory fixture;
it never writes to Alertmanager, Thanos, Grafana, or another external system.

Run directly (``python scripts/mock_alertmanager.py``) or via ``./start.sh --mock``.
This is a developer aid, not part of the product.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit

HOST = "127.0.0.1"
PORT = 9999


def _ago(minutes: int) -> str:
    ts = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return ts.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _alert(
    fp: str,
    name: str,
    severity: str,
    mins: int,
    *,
    annotations: dict[str, str] | None = None,
    **labels: str,
) -> dict:
    all_labels = {"alertname": name, "severity": severity, **labels}
    return {
        "fingerprint": fp,
        "labels": all_labels,
        "annotations": {
            "summary": f"{name} on {labels.get('cluster', '?')}",
            **(annotations or {}),
        },
        "startsAt": _ago(mins),
        "endsAt": "0001-01-01T00:00:00Z",
        "status": {"state": "active"},
        # F27: real Alertmanager alerts carry this, and it is the fallback source
        # for the primary curve when the rules endpoint is empty. The shape
        # matters -- the expression lives in the `g0.expr` query parameter, and
        # the host is an in-cluster address the workbench must never request.
        "generatorURL": (
            "http://prometheus-mock-0:9090/graph?g0.expr="
            + quote(GENERATOR_EXPRESSIONS.get(name, f"{name.lower()}_ratio < 0.1"))
            + "&g0.tab=1"
        ),
    }


# F27: one expression per alert name, chosen to exercise all three primary-curve
# tiers. The `or`-joined one is modelled on the user's real ES alert -- the first
# real sample the design met landed on the degraded path, which is why "no tier
# may draw an empty chart" is a requirement (ADR 0009).
# Keys are the alert names this mock actually emits, so every tier is reachable
# by opening a real group in the browser rather than only in a unit test.
GENERATOR_EXPRESSIONS = {
    # Tier 1: a trailing threshold that strips cleanly -> value curve + threshold line.
    "HighMemoryUsage": "node_memory_used_ratio > 0.9",
    "CertificateExpiringSoon": "node_filesystem_used_ratio > 0.85",
    # Tier 2: top-level `or`, nothing to strip -- modelled on the user's real ES
    # alert, the first real sample the design met. The metric name carries it.
    "KubePodCrashLooping": (
        '(elasticsearch_cluster_health_status{color="green"} == 0)'
        ' or (elasticsearch_cluster_health_status{color="yellow"} == 1)'
    ),
    # Tier 2 with a counter: must come back wrapped in rate().
    "TargetDown": "http_requests_errors_total",
    "CheckoutErrorRateHigh": "checkout_http_error_ratio > 0.05",
    # Tier 3: every identifier is a function call, so no metric can be extracted.
    "KubeNodeNotReady": "vector(1)",
    # No series in the store -> METRIC_NOT_FOUND rather than an empty chart.
    "HAMergedAlert": "no_such_metric_here > 1",
    # High cardinality -> SERIES_LIMIT_EXCEEDED.
    "EndpointUniqueAlert": "high_cardinality_metric",
}


SCENARIOS = {
    "baseline",
    "notification_open",
    "notification_member",
    "notification_critical",
    "notification_recovered",
}
SOURCE_IDS = ("source-a", "source-b")
SOURCE_ENDPOINTS = {
    "source-a": ("endpoint-1", "endpoint-2"),
    "source-b": ("endpoint-1",),
}
SOURCE_SCENARIOS = {
    "baseline",
    "duplicate",
    "same_cluster",
    "partial_fail",
    "all_fail",
    "recovery",
    "watchdog_grace",
    "watchdog_missing",
    "watchdog_ignored",
    "source_disable_enable",
    "payload_divergence",
    "noise_burst",
    "noise_flapping",
}
THANOS_SCENARIOS = {"baseline", "overlap"}
DEFAULT_SCENARIO = os.environ.get("ALERT_WORKBENCH_MOCK_SCENARIO", "baseline")
if DEFAULT_SCENARIO not in SCENARIOS:
    raise ValueError(
        "ALERT_WORKBENCH_MOCK_SCENARIO must be one of "
        + ", ".join(sorted(SCENARIOS))
    )
DEFAULT_SOURCE_SCENARIO = os.environ.get(
    "ALERT_WORKBENCH_MOCK_SOURCE_SCENARIO", "baseline"
)
if DEFAULT_SOURCE_SCENARIO not in SOURCE_SCENARIOS:
    raise ValueError(
        "ALERT_WORKBENCH_MOCK_SOURCE_SCENARIO must be one of "
        + ", ".join(sorted(SOURCE_SCENARIOS))
    )
DEFAULT_THANOS_SCENARIO = os.environ.get(
    "ALERT_WORKBENCH_MOCK_THANOS_SCENARIO", "baseline"
)
if DEFAULT_THANOS_SCENARIO not in THANOS_SCENARIOS:
    raise ValueError(
        "ALERT_WORKBENCH_MOCK_THANOS_SCENARIO must be one of "
        + ", ".join(sorted(THANOS_SCENARIOS))
    )


# A group that fires, then stops being reported — the only way to get a
# *recovered* Incident out of a mock Alertmanager, because `/api/v2/alerts`
# only ever returns active alerts and recovery is inferred from absence.
#
# source-b is seeded with `resolution_grace_seconds: 0` and a 10s interval
# (`seed_mock_sources.py`), so after this alert stops being served the backend
# needs one poll to mark it `pending_resolution` and one more to resolve it:
# roughly 30–40s after the stack starts, the alerts page has an active list
# *and* a recovered fold. Before this existed, F25's "已恢复默认收起" assertion
# had nothing to assert against on the bundled mock and always skipped.
TRANSIENT_ALERTNAME = "TransientProbeAlert"
TRANSIENT_POLL_LIMIT = 2


def _source_cluster(source_id: str, scenario: str) -> str:
    if scenario == "same_cluster":
        return "shared-cluster"
    return "cluster-a" if source_id == "source-a" else "cluster-b"


def _drop_fingerprint(alert: dict) -> dict:
    copy = dict(alert)
    copy.pop("fingerprint", None)
    return copy


def _source_watchdogs(source_id: str, scenario: str) -> list[dict]:
    if scenario == "watchdog_missing":
        return []
    cluster = _source_cluster(source_id, scenario)
    alerts = [
        _alert(
            f"{source_id}-watchdog-{cluster}",
            "Watchdog",
            "none",
            1,
            cluster=cluster,
        )
    ]
    if scenario in {"watchdog_grace", "watchdog_ignored"}:
        alerts.append(
            _alert(
                f"{source_id}-watchdog-ignored",
                "Watchdog",
                "none",
                1,
                cluster="ignored-cluster",
            )
        )
    return alerts


def build_source_alerts(
    source_id: str,
    endpoint_id: str,
    *,
    scenario: str = "baseline",
    poll_count: int = 1,
) -> list[dict]:
    """Build source-scoped HA endpoint fixtures without touching legacy alerts.

    `poll_count` is this endpoint's 1-based request number. The transient probe
    and flapping acceptance fixtures read it; every other fixture stays a pure
    function of (source, endpoint, scenario).
    """
    if source_id not in SOURCE_IDS:
        raise ValueError(f"unknown source: {source_id}")
    if endpoint_id not in SOURCE_ENDPOINTS[source_id]:
        raise ValueError(f"unknown endpoint for {source_id}: {endpoint_id}")
    if scenario not in SOURCE_SCENARIOS:
        raise ValueError(f"unknown source scenario: {scenario}")

    cluster = _source_cluster(source_id, scenario)
    if scenario == "recovery":
        return _source_watchdogs(source_id, scenario)

    shared = _alert(
        f"{source_id}-shared-fingerprint",
        "HAMergedAlert",
        "critical",
        2,
        cluster=cluster,
        namespace="payments" if source_id == "source-a" else "checkout",
        pod="api-0",
    )
    if scenario == "payload_divergence" and endpoint_id.endswith("2"):
        shared = _alert(
            f"{source_id}-shared-fingerprint",
            "HAMergedAlert",
            "warning",
            2,
            cluster=cluster,
            namespace="payments",
            pod="api-0",
            annotations={"summary": "divergent payload from HA peer"},
        )

    no_fingerprint = _drop_fingerprint(
        _alert(
            f"{source_id}-labels-only",
            "LabelsOnlyIdentity",
            "warning",
            3,
            cluster=cluster,
            namespace="edge",
            service="gateway",
        )
    )
    endpoint_unique = _alert(
        f"{source_id}-{endpoint_id}-unique",
        "EndpointUniqueAlert",
        "info",
        5,
        cluster=cluster,
        endpoint=endpoint_id,
    )
    checkout_error_rate = _alert(
        "source-a-checkout-error-rate",
        "CheckoutErrorRateHigh",
        "critical",
        25,
        cluster=cluster,
        namespace="checkout",
        service="checkout-api",
        annotations={
            "summary": "结算 API 5xx 错误率持续升高",
            "description": "最近 25 分钟错误率从正常区间持续升高并越过 5% 告警阈值。",
        },
    )

    alerts = [*_source_watchdogs(source_id, scenario), shared]
    if source_id == "source-a":
        alerts.append(checkout_error_rate)
        if endpoint_id == "endpoint-1":
            alerts.append(no_fingerprint)
        else:
            alerts.extend([no_fingerprint, endpoint_unique])
    else:
        alerts.append(
            _alert(
                "source-b-same-name",
                "HAMergedAlert",
                "critical",
                4,
                cluster=cluster,
                namespace="payments",
                pod="api-0",
            )
        )
    if source_id == "source-b" and poll_count <= TRANSIENT_POLL_LIMIT:
        alerts.append(
            _alert(
                "source-b-transient-probe",
                TRANSIENT_ALERTNAME,
                "info",
                1,
                cluster=cluster,
                service="probe",
            )
        )
    if scenario == "duplicate":
        alerts.append(shared)
    if scenario == "source_disable_enable":
        alerts.append(
            _alert(
                f"{source_id}-reenabled-marker",
                "SourceReenabledMarker",
                "info",
                1,
                cluster=cluster,
            )
        )
    if scenario == "noise_burst":
        alerts.extend(
            _alert(
                f"{source_id}-noise-burst-{index}",
                "NoiseBurstAlert",
                "warning",
                0,
                cluster=cluster,
                namespace="noise-acceptance",
                burst_member=str(index),
            )
            for index in range(12)
        )
    # A missing Alert first becomes PENDING_RESOLUTION and only a second
    # complete missing poll can make it RECOVERED, even with zero grace. Keep
    # the alert for one poll and omit it for two so the acceptance fixture
    # exercises real FIRING <-> RECOVERED transitions instead of oscillating
    # forever between FIRING and PENDING_RESOLUTION.
    if scenario == "noise_flapping" and poll_count % 3 == 1:
        alerts.append(
            _alert(
                f"{source_id}-noise-flapping",
                "NoiseFlappingAlert",
                "warning",
                0,
                cluster=cluster,
                namespace="noise-acceptance",
            )
        )
    return alerts


# F27: the metric names the mock store claims to know. `metric_names` uses this
# to answer "does this metric exist here", which is what tells METRIC_NOT_FOUND
# apart from THANOS_UNREACHABLE.
KNOWN_METRICS = {
    "checkout_http_error_ratio",
    "checkout_http_request_rate",
    "labelsonlyidentity_ratio",
    "node_memory_used_ratio",
    "node_filesystem_used_ratio",
    "elasticsearch_cluster_health_status",
    "http_requests_errors_total",
    "some_metric",
}

_METRIC_LABEL_VALUES = {
    metric: {
        "__name__": (metric,),
        "cluster": ("source-a-cluster",),
        "instance": ("10.0.0.5:9100",),
    }
    for metric in KNOWN_METRICS
}
_METRIC_LABEL_VALUES["checkout_http_error_ratio"] = {
    "__name__": ("checkout_http_error_ratio",),
    "cluster": ("cluster-a",),
    "namespace": ("checkout",),
    "service": ("checkout-api",),
}
_METRIC_LABEL_VALUES["checkout_http_request_rate"] = dict(
    _METRIC_LABEL_VALUES["checkout_http_error_ratio"]
)
_METRIC_LABEL_VALUES["checkout_http_request_rate"]["__name__"] = (
    "checkout_http_request_rate",
)

# ---------------------------------------------------------------------------
# Grafana: two read-only endpoints, enough dashboard to exercise every branch
# of the import parser without a real Grafana.
#
# The panels are chosen so that one pass over this dashboard produces all four
# candidate classifications on screen — a directly usable one, ones needing a
# decision, and several distinct reasons for "cannot use this". A mock that only
# produced the happy path would leave the classification UI untested exactly
# where it matters.
# ---------------------------------------------------------------------------

GRAFANA_DASHBOARD_SUMMARIES = [
    {
        "uid": "node-health",
        "title": "Node health",
        "folderTitle": "Platform",
        "type": "dash-db",
    },
    {
        "uid": "service-traffic",
        "title": "Service traffic",
        "folderTitle": "Platform",
        "type": "dash-db",
    },
]

GRAFANA_DASHBOARDS = {
    "node-health": {
        "uid": "node-health",
        "title": "Node health",
        "templating": {
            "list": [
                {
                    "name": "instance",
                    "type": "query",
                    "multi": False,
                    "current": {"text": "10.0.0.5:9100", "value": "10.0.0.5:9100"},
                },
                {
                    "name": "cluster",
                    "type": "query",
                    "multi": True,
                    "current": {"text": ["prod"], "value": ["$__all", "prod"]},
                },
            ]
        },
        "panels": [
            {
                "id": 1,
                "type": "row",
                "title": "Capacity",
                "collapsed": True,
                "panels": [
                    {
                        "id": 2,
                        "type": "timeseries",
                        "title": "Memory used",
                        "datasource": {"type": "prometheus", "uid": "mock-prom"},
                        "fieldConfig": {
                            "defaults": {
                                "unit": "percentunit",
                                "thresholds": {
                                    "steps": [
                                        {"color": "green", "value": None},
                                        {"color": "red", "value": 0.9},
                                    ]
                                },
                            }
                        },
                        "targets": [
                            {
                                "refId": "A",
                                "expr": 'node_memory_used_ratio{instance="$instance"}',
                            }
                        ],
                    }
                ],
            },
            {
                "id": 3,
                "type": "stat",
                "title": "Filesystem used",
                "datasource": {"type": "prometheus", "uid": "mock-prom"},
                "fieldConfig": {"defaults": {"unit": "percentunit"}},
                "targets": [
                    {"refId": "A", "expr": "node_filesystem_used_ratio"}
                ],
            },
            {
                "id": 4,
                "type": "timeseries",
                "title": "Node logs",
                "datasource": {"type": "loki", "uid": "mock-loki"},
                "targets": [
                    {"refId": "A", "expr": '{job="node"} |= "error"'}
                ],
            },
            {
                "id": 5,
                "type": "table",
                "title": "Top consumers",
                "datasource": {"type": "prometheus", "uid": "mock-prom"},
                "targets": [
                    {"refId": "A", "expr": "topk(5, node_memory_used_ratio)"}
                ],
            },
        ],
    },
    "service-traffic": {
        "uid": "service-traffic",
        "title": "Service traffic",
        "templating": {
            "list": [
                {
                    "name": "instance",
                    "type": "query",
                    "multi": False,
                    "current": {"text": "10.0.0.5:9100", "value": "10.0.0.5:9100"},
                }
            ]
        },
        "panels": [
            {
                "id": 1,
                "type": "timeseries",
                "title": "Error rate",
                "datasource": {"type": "prometheus", "uid": "mock-prom"},
                "fieldConfig": {"defaults": {"unit": "reqps"}},
                "targets": [
                    {
                        "refId": "A",
                        "legendFormat": "errors",
                        "expr": "rate(http_requests_errors_total[$__rate_interval])",
                    },
                    {
                        "refId": "B",
                        "legendFormat": "errors over range",
                        "expr": "increase(http_requests_errors_total[$__range])",
                    },
                ],
            },
            {
                "id": 2,
                "type": "timeseries",
                "title": "Errors at window start",
                "datasource": {"type": "prometheus", "uid": "mock-prom"},
                "targets": [
                    {"refId": "A", "expr": "http_requests_errors_total @ $__from"}
                ],
            },
        ],
    },
}

# Only counters here; everything else reads as a gauge. `metadata_missing`
# withholds all of it so the suffix heuristic path gets exercised too.
_METRIC_TYPES = {
    "checkout_http_error_ratio": (
        "gauge",
        "Fraction of checkout API requests returning HTTP 5xx",
    ),
    "checkout_http_request_rate": (
        "gauge",
        "Checkout API requests served per second",
    ),
    "http_requests_errors_total": ("counter", "Total HTTP errors served"),
    "node_memory_used_ratio": ("gauge", "Fraction of node memory in use"),
    "elasticsearch_cluster_health_status": ("gauge", "ES cluster health by colour"),
}


def build_thanos_rule_groups() -> list[dict]:
    """Alerting rules in the shape Prometheus and Thanos Ruler return them."""
    return [
        {
            "name": "mock-rules",
            "file": "/etc/rules/mock.yaml",
            "rules": [
                {
                    "type": "alerting",
                    "name": name,
                    "query": expression,
                    "duration": 300,
                    "labels": {"severity": "warning"},
                    "annotations": {"summary": f"{name} fired"},
                    "state": "firing",
                }
                for name, expression in GENERATOR_EXPRESSIONS.items()
            ]
            + [
                # A recording rule, to prove the client drops non-alerting ones.
                {"type": "recording", "name": "job:mock:rate5m", "query": "sum(up)"}
            ],
        }
    ]


def build_thanos_metadata(metric: str, *, scenario: str = "baseline") -> dict:
    if scenario == "metadata_missing":
        return {}
    entry = _METRIC_TYPES.get(metric)
    if entry is None:
        return {}
    kind, help_text = entry
    return {metric: [{"type": kind, "help": help_text, "unit": ""}]}


def build_thanos_label_names(metric: str) -> list[str]:
    """Return the deterministic label catalog used by Planner DescribeMetric."""
    return sorted(_METRIC_LABEL_VALUES.get(metric, {}))


def build_thanos_label_values(metric: str, label: str) -> list[str]:
    """Return values for one metric label without introducing a second fixture path."""
    return sorted(_METRIC_LABEL_VALUES.get(metric, {}).get(label, ()))


def _metric_from_matchers(query: str) -> str:
    for matcher in parse_qs(query).get("match[]", []):
        metric = matcher.split("{", 1)[0].strip()
        if metric:
            return metric
    return ""


def build_thanos_matrix(query: str, *, scenario: str = "baseline") -> dict:
    """A deterministic matrix whose shape depends on what was asked for.

    Reproduces, without any network, every branch the evidence path has to
    classify: a normal curve, an empty result (METRIC_NOT_FOUND), and a
    high-cardinality answer (SERIES_LIMIT_EXCEEDED).
    """
    now = int(datetime.now(timezone.utc).timestamp())
    step = 300
    stamps = [now - step * index for index in range(24, 0, -1)]

    if "no_such_metric" in query or scenario == "empty_matrix":
        return {"resultType": "matrix", "result": []}

    if "high_cardinality" in query or scenario == "high_cardinality":
        return {
            "resultType": "matrix",
            "result": [
                {
                    "metric": {"pod": f"pod-{index}"},
                    "values": [[stamp, "1"] for stamp in stamps],
                }
                for index in range(40)
            ],
        }

    # A curve that visibly climbs into the alert, so a chart of it is worth
    # looking at rather than a flat line. The positive checkout journey uses
    # values that match its 5% threshold; generic fault fixtures keep the old
    # wide 0.30→0.875 range.
    if "checkout_http_error_ratio" in query:
        values = [
            [stamp, f"{0.012 + (0.075 * index / 23):.4f}"]
            for index, stamp in enumerate(stamps)
        ]
    elif "checkout_http_request_rate" in query:
        values = [
            [stamp, f"{120 + ((index % 5) - 2):.1f}"]
            for index, stamp in enumerate(stamps)
        ]
    else:
        values = [
            [stamp, f"{0.30 + 0.025 * index:.4f}"]
            for index, stamp in enumerate(stamps)
        ]
    return {
        "resultType": "matrix",
        "result": [{"metric": {"instance": "mock-node-0"}, "values": values}],
    }


def build_thanos_series(*, scenario: str = "baseline") -> list[dict[str, str]]:
    series = [
        {
            "__name__": "ALERTS",
            "alertname": "KubePodCrashLooping",
            "alertstate": "firing",
            "cluster": "prod-cn-east",
            "namespace": "checkout",
            "pod": "checkout-api-history",
        },
        {
            "__name__": "ALERTS",
            "alertname": "TargetDown",
            "alertstate": "firing",
            "cluster": "prod-cn-east",
            "job": "cadvisor",
        },
        {
            "__name__": "ALERTS",
            "alertname": "HistoricalOnlyAlert",
            "alertstate": "firing",
            "cluster": "prod-cn-west",
            "service": "catalog",
        },
        {
            "__name__": "ALERTS",
            "alertname": "HAMergedAlert",
            "alertstate": "firing",
            "cluster": "cluster-a",
            "namespace": "payments",
            "source_scope": "source-a",
        },
        {
            "__name__": "ALERTS",
            "alertname": "HAMergedAlert",
            "alertstate": "firing",
            "cluster": "cluster-b",
            "namespace": "checkout",
            "source_scope": "source-b",
        },
    ]
    if scenario == "overlap":
        series.append(
            {
                "__name__": "ALERTS",
                "alertname": "HAMergedAlert",
                "alertstate": "firing",
                "cluster": "cluster-a",
                "namespace": "payments",
                "source_scope": "overlap",
            }
        )
    return series


def _notification_alerts(count: int, *, critical: bool = False) -> list[dict]:
    return [
        _alert(
            f"notify-{index:02d}",
            "NotificationScenario",
            "critical" if critical and index == 0 else "warning",
            1,
            cluster="prod-notify",
            namespace="checkout",
            pod=f"checkout-{index:02d}",
            annotations={
                "summary": f"notification lifecycle member {index:02d}",
            },
        )
        for index in range(count)
    ]


def build_alerts(
    *, include_all_watchdogs: bool = True, scenario: str = "baseline"
) -> list[dict]:
    watchdogs = [
        _alert(
            "watchdog-prod-east-1",
            "Watchdog",
            "none",
            1,
            cluster="prod-cn-east",
        ),
        # A second series for the same cluster proves the workbench counts
        # clusters rather than raw Watchdog series.
        _alert("watchdog-prod-east-2", "Watchdog", "none", 1, cluster="prod-cn-east"),
        _alert("watchdog-prod-north", "Watchdog", "none", 1, cluster="prod-cn-north"),
        _alert("watchdog-prod-south", "Watchdog", "none", 1, cluster="prod-cn-south"),
        _alert("watchdog-prod-hk", "Watchdog", "none", 1, cluster="prod-hk"),
    ]
    if include_all_watchdogs:
        watchdogs.extend(
            [
                _alert("watchdog-prod-west", "Watchdog", "none", 1, cluster="prod-cn-west"),
                _alert("watchdog-prod-sg", "Watchdog", "none", 1, cluster="prod-sg"),
            ]
        )

    alerts = [
        *watchdogs,
        _alert(
            "f01",
            "KubePodCrashLooping",
            "critical",
            6,
            cluster="prod-cn-east",
            namespace="payments",
            pod="pay-api-7f9c",
            annotations={
                "promql": "sum(rate(kube_pod_container_status_restarts_total[5m]))",
            },
        ),
        _alert("f02", "KubePodCrashLooping", "critical", 4, cluster="prod-cn-east", namespace="payments", pod="pay-api-3c2d"),
        _alert("f03", "KubePodCrashLooping", "critical", 2, cluster="prod-cn-east", namespace="payments", pod="pay-api-9a1b"),
        _alert("f10", "KubeNodeNotReady", "critical", 11, cluster="prod-cn-north", node="node-17"),
        _alert("f20", "TargetDown", "warning", 18, cluster="prod-cn-east", job="node-exporter"),
        _alert("f21", "TargetDown", "warning", 15, cluster="prod-cn-east", job="kube-state-metrics"),
        _alert("f30", "HighMemoryUsage", "warning", 23, cluster="prod-cn-north", namespace="search", pod="search-idx-2"),
        _alert("f40", "APIHighLatencyP99", "warning", 9, cluster="prod-cn-east", service="checkout-gw"),
        _alert("f50", "CertificateExpiringSoon", "info", 140, cluster="prod-cn-east", service="ingress"),
        _alert("f60", "DiskWillFillIn24h", "info", 47, cluster="prod-cn-north", instance="10.4.2.11:9100"),
    ]
    if scenario == "notification_open":
        alerts.extend(_notification_alerts(20))
    elif scenario == "notification_member":
        alerts.extend(_notification_alerts(21))
    elif scenario == "notification_critical":
        alerts.extend(_notification_alerts(21, critical=True))
    elif scenario not in {"baseline", "notification_recovered"}:
        raise ValueError(f"unknown mock scenario: {scenario}")
    return alerts


class Handler(BaseHTTPRequestHandler):
    alert_poll_count = 0
    scenario = DEFAULT_SCENARIO
    source_scenarios = {
        source_id: DEFAULT_SOURCE_SCENARIO for source_id in SOURCE_IDS
    }
    source_endpoint_poll_counts = {
        f"{source_id}:{endpoint_id}": 0
        for source_id, endpoint_ids in SOURCE_ENDPOINTS.items()
        for endpoint_id in endpoint_ids
    }
    thanos_scenario = DEFAULT_THANOS_SCENARIO

    def _json(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        parsed = urlsplit(self.path)
        source_endpoint = self._source_endpoint(parsed.path)
        if source_endpoint is not None:
            source_id, endpoint_id = source_endpoint
            scenario = type(self).source_scenarios[source_id]
            key = f"{source_id}:{endpoint_id}"
            type(self).source_endpoint_poll_counts[key] += 1
            if self._source_endpoint_should_fail(source_id, endpoint_id, scenario):
                self._json(
                    {
                        "message": "mock endpoint failure",
                        "source": source_id,
                        "endpoint": endpoint_id,
                        "scenario": scenario,
                    },
                    status=503,
                )
                return
            self._json(
                build_source_alerts(
                    source_id,
                    endpoint_id,
                    scenario=scenario,
                    poll_count=type(self).source_endpoint_poll_counts[key],
                )
            )
        elif parsed.path == "/api/v2/alerts":
            type(self).alert_poll_count += 1
            # The first poll establishes six known clusters. Subsequent polls
            # omit two so the mock UI demonstrates a partial Watchdog failure.
            self._json(
                build_alerts(
                    include_all_watchdogs=self.alert_poll_count == 1,
                    scenario=type(self).scenario,
                )
            )
        elif parsed.path == "/__mock__/state":
            self._json(
                {
                    "scenario": type(self).scenario,
                    "available_scenarios": sorted(SCENARIOS),
                    "alert_poll_count": type(self).alert_poll_count,
                    "sources": {
                        source_id: {
                            "scenario": type(self).source_scenarios[source_id],
                            "endpoints": [
                                {
                                    "id": endpoint_id,
                                    "url_path": f"/{source_id}/{endpoint_id}/api/v2/alerts",
                                    "poll_count": type(self).source_endpoint_poll_counts[
                                        f"{source_id}:{endpoint_id}"
                                    ],
                                }
                                for endpoint_id in SOURCE_ENDPOINTS[source_id]
                            ],
                        }
                        for source_id in SOURCE_IDS
                    },
                    "source_scenarios": sorted(SOURCE_SCENARIOS),
                    "thanos": {
                        "scenario": type(self).thanos_scenario,
                        "available_scenarios": sorted(THANOS_SCENARIOS),
                    },
                }
            )
        elif parsed.path == "/api/search":
            query = parse_qs(parsed.query).get("query", [""])[0].lower()
            self._json(
                [
                    dashboard
                    for dashboard in GRAFANA_DASHBOARD_SUMMARIES
                    if not query or query in dashboard["title"].lower()
                ]
            )
        elif parsed.path.startswith("/api/dashboards/uid/"):
            uid = parsed.path.rsplit("/", 1)[-1]
            dashboard = GRAFANA_DASHBOARDS.get(uid)
            if dashboard is None:
                self.send_response(404)
                self.end_headers()
                return
            self._json({"dashboard": dashboard, "meta": {"canEdit": False}})
        elif parsed.path == "/api/v1/series":
            self._json(
                {
                    "status": "success",
                    "data": build_thanos_series(scenario=type(self).thanos_scenario),
                }
            )
        elif parsed.path == "/api/v1/query":
            # Two callers now: the connection probe (F21) and the import probe,
            # which asks "does this query run, and does it return anything".
            # thanos_scenario == "unreachable" exercises the failure path
            # without touching the network.
            if type(self).thanos_scenario == "unreachable":
                self.send_response(503)
                self.end_headers()
                return
            query = (parse_qs(parsed.query).get("query") or [""])[0]
            # Answering only for metrics this mock claims to know keeps both
            # import outcomes reachable: "runs and has data" and "runs but the
            # window is empty" are different things the page has to say
            # differently.
            known = any(metric in query for metric in KNOWN_METRICS)
            self._json(
                {
                    "status": "success",
                    "data": {
                        "resultType": "vector",
                        "result": (
                            [
                                {
                                    "metric": {"instance": "10.0.0.5:9100"},
                                    "value": [time.time(), "0.83"],
                                }
                            ]
                            if known
                            else []
                        ),
                    },
                }
            )
        elif parsed.path == "/api/v1/query_range":
            query = (parse_qs(parsed.query).get("query") or [""])[0]
            self._json(
                {
                    "status": "success",
                    "data": build_thanos_matrix(
                        query, scenario=type(self).thanos_scenario
                    ),
                }
            )
        elif parsed.path == "/api/v1/rules":
            # `thanos_scenario == "no_ruler"` reproduces the deployment where
            # Thanos Query has no Ruler behind it: the endpoint answers with no
            # groups, and the primary curve has to fall back to generatorURL.
            # That is a normal shape, not an error (ADR 0009).
            groups = (
                []
                if type(self).thanos_scenario == "no_ruler"
                else build_thanos_rule_groups()
            )
            self._json({"status": "success", "data": {"groups": groups}})
        elif parsed.path == "/api/v1/metadata":
            metric = (parse_qs(parsed.query).get("metric") or [""])[0]
            self._json(
                {
                    "status": "success",
                    "data": build_thanos_metadata(
                        metric, scenario=type(self).thanos_scenario
                    ),
                }
            )
        elif parsed.path == "/api/v1/labels":
            self._json(
                {
                    "status": "success",
                    "data": build_thanos_label_names(
                        _metric_from_matchers(parsed.query)
                    ),
                }
            )
        elif parsed.path.startswith("/api/v1/label/") and parsed.path.endswith(
            "/values"
        ):
            label = unquote(
                parsed.path.removeprefix("/api/v1/label/").removesuffix("/values")
            )
            if label == "__name__" and not parse_qs(parsed.query).get("match[]"):
                data = sorted(KNOWN_METRICS)
            else:
                data = build_thanos_label_values(
                    _metric_from_matchers(parsed.query), label
                )
            self._json({"status": "success", "data": data})
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self) -> None:  # noqa: N802 (http.server API)
        path = urlsplit(self.path).path
        if path == "/__mock__/scenario":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                request = json.loads(self.rfile.read(length) or b"{}")
                source_id = request.get("source")
                if source_id is not None:
                    self._set_source_scenario(str(source_id), request)
                    return
                scenario = str(request.get("scenario") or "")
                if scenario not in SCENARIOS:
                    self._json(
                        {
                            "message": "unknown scenario",
                            "available_scenarios": sorted(SCENARIOS),
                        },
                        status=422,
                    )
                    return
                type(self).scenario = scenario
                self._json({"scenario": scenario})
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self._json({"message": f"bad mock scenario: {exc}"}, status=400)
            return
        if path.startswith("/__mock__/sources/") and path.endswith("/scenario"):
            try:
                source_id = path.split("/")[3]
                length = int(self.headers.get("Content-Length", "0"))
                request = json.loads(self.rfile.read(length) or b"{}")
                self._set_source_scenario(source_id, request)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self._json(
                    {"message": f"bad mock source scenario: {exc}"},
                    status=400,
                )
            return
        if path == "/__mock__/thanos/scenario":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                request = json.loads(self.rfile.read(length) or b"{}")
                scenario = str(request.get("scenario") or "")
                if scenario not in THANOS_SCENARIOS:
                    self._json(
                        {
                            "message": "unknown Thanos scenario",
                            "available_scenarios": sorted(THANOS_SCENARIOS),
                        },
                        status=422,
                    )
                    return
                type(self).thanos_scenario = scenario
                self._json({"scenario": scenario})
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self._json({"message": f"bad mock Thanos scenario: {exc}"}, status=400)
            return
        if path != "/api/ds/query":
            self.send_response(404)
            self.end_headers()
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            request = json.loads(self.rfile.read(length) or b"{}")
            start_ms = int(request.get("from", 0))
            end_ms = int(request.get("to", start_ms + 240_000))
            step_ms = max(1_000, (end_ms - start_ms) // 4)
            timestamps = [start_ms + step_ms * index for index in range(5)]
            results = {}
            for index, query in enumerate(request.get("queries", []), start=1):
                ref_id = str(query.get("refId", f"Q{index}"))
                field_name = "restart_count" if "rawSql" in query else "restart_rate"
                values = [float((index * 3) + offset * (index + 1)) for offset in range(5)]
                results[ref_id] = {
                    "status": 200,
                    "frames": [
                        {
                            "schema": {
                                "name": ref_id,
                                "fields": [
                                    {"name": "Time", "type": "time"},
                                    {
                                        "name": field_name,
                                        "type": "number",
                                        "labels": {"source": "mock"},
                                    },
                                ],
                            },
                            "data": {"values": [timestamps, values]},
                        }
                    ],
                }
            self._json({"results": results})
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._json({"message": f"bad mock query: {exc}"}, status=400)

    def log_message(self, *args) -> None:  # silence request logging
        pass

    def _set_source_scenario(self, source_id: str, request: dict) -> None:
        scenario = str(request.get("scenario") or "")
        if source_id not in SOURCE_IDS:
            self._json(
                {
                    "message": "unknown source",
                    "available_sources": list(SOURCE_IDS),
                },
                status=422,
            )
            return
        if scenario not in SOURCE_SCENARIOS:
            self._json(
                {
                    "message": "unknown source scenario",
                    "available_scenarios": sorted(SOURCE_SCENARIOS),
                },
                status=422,
            )
            return
        type(self).source_scenarios[source_id] = scenario
        for endpoint_id in SOURCE_ENDPOINTS[source_id]:
            type(self).source_endpoint_poll_counts[f"{source_id}:{endpoint_id}"] = 0
        self._json({"source": source_id, "scenario": scenario})

    @staticmethod
    def _source_endpoint(path: str) -> tuple[str, str] | None:
        parts = [part for part in path.split("/") if part]
        if len(parts) != 5 or parts[2:] != ["api", "v2", "alerts"]:
            return None
        source_id, endpoint_id = parts[0], parts[1]
        if source_id not in SOURCE_ENDPOINTS:
            return None
        if endpoint_id not in SOURCE_ENDPOINTS[source_id]:
            return None
        return source_id, endpoint_id

    @staticmethod
    def _source_endpoint_should_fail(
        source_id: str,
        endpoint_id: str,
        scenario: str,
    ) -> bool:
        if scenario == "all_fail":
            return True
        return (
            scenario == "partial_fail"
            and source_id == "source-a"
            and endpoint_id == "endpoint-2"
        )


if __name__ == "__main__":
    print(f"Mock source APIs on http://{HOST}:{PORT}")
    print("  legacy Alertmanager: /api/v2/alerts")
    for source_id, endpoint_ids in SOURCE_ENDPOINTS.items():
        for endpoint_id in endpoint_ids:
            print(f"  {source_id} {endpoint_id}: /{source_id}/{endpoint_id}/api/v2/alerts")
    HTTPServer((HOST, PORT), Handler).serve_forever()
