"""F20 multi-source mock harness fixtures."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _mock_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "mock_alertmanager.py"
    spec = importlib.util.spec_from_file_location("mock_alertmanager", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_source_a_ha_endpoints_cover_union_dedupe_and_labels_hash() -> None:
    mock = _mock_module()
    first = mock.build_source_alerts("source-a", "endpoint-1", scenario="duplicate")
    second = mock.build_source_alerts("source-a", "endpoint-2", scenario="duplicate")
    fingerprints = [item.get("fingerprint") for item in [*first, *second]]
    assert fingerprints.count("source-a-shared-fingerprint") >= 4
    labels_only = [
        item
        for item in [*first, *second]
        if item["labels"]["alertname"] == "LabelsOnlyIdentity"
    ]
    assert len(labels_only) == 2
    assert all("fingerprint" not in item for item in labels_only)
    assert any(
        item.get("fingerprint") == "source-a-endpoint-2-unique"
        for item in second
    )


def test_source_a_baseline_has_a_coherent_positive_investigation_story() -> None:
    mock = _mock_module()
    first = mock.build_source_alerts("source-a", "endpoint-1")
    second = mock.build_source_alerts("source-a", "endpoint-2")
    matches = [
        item for item in [*first, *second]
        if item["labels"]["alertname"] == "CheckoutErrorRateHigh"
    ]

    assert len(matches) == 2
    assert {item["labels"]["service"] for item in matches} == {"checkout-api"}
    assert all("结算 API" in item["annotations"]["summary"] for item in matches)
    assert "checkout_http_error_ratio" in mock.GENERATOR_EXPRESSIONS[
        "CheckoutErrorRateHigh"
    ]
    assert "checkout_http_error_ratio" in mock.KNOWN_METRICS
    assert "checkout_http_request_rate" in mock.KNOWN_METRICS
    matrix = mock.build_thanos_matrix("checkout_http_error_ratio")
    values = [float(point[1]) for point in matrix["result"][0]["values"]]
    assert values[-1] > values[0]
    request_values = [
        float(point[1])
        for point in mock.build_thanos_matrix("checkout_http_request_rate")["result"][0]["values"]
    ]
    assert max(request_values) - min(request_values) < 10


def test_source_b_same_cluster_keeps_source_identity_distinct() -> None:
    mock = _mock_module()
    source_a = mock.build_source_alerts(
        "source-a", "endpoint-1", scenario="same_cluster"
    )
    source_b = mock.build_source_alerts(
        "source-b", "endpoint-1", scenario="same_cluster"
    )
    assert {item["labels"]["cluster"] for item in source_a} == {"shared-cluster"}
    assert {item["labels"]["cluster"] for item in source_b} == {"shared-cluster"}
    assert {item.get("fingerprint") for item in source_a} != {
        item.get("fingerprint") for item in source_b
    }


def test_transient_probe_stops_being_served_so_a_group_can_recover() -> None:
    """The bundled mock's only route to a *recovered* Incident.

    `/api/v2/alerts` only returns active alerts, so recovery is inferred from
    absence: this alert has to actually disappear. Serving it forever (or never)
    both leave the alerts page without a recovered fold, which is what made
    F25's "已恢复默认收起" assertion permanently skip.
    """
    mock = _mock_module()

    def alertnames(poll_count: int) -> set[str]:
        return {
            item["labels"]["alertname"]
            for item in mock.build_source_alerts(
                "source-b", "endpoint-1", poll_count=poll_count
            )
        }

    for poll_count in range(1, mock.TRANSIENT_POLL_LIMIT + 1):
        assert mock.TRANSIENT_ALERTNAME in alertnames(poll_count)
    assert mock.TRANSIENT_ALERTNAME not in alertnames(mock.TRANSIENT_POLL_LIMIT + 1)

    # It must not leave the source's other groups behind, or the page would have
    # a fold and an empty main list — the branch the assertion is not testing.
    assert alertnames(mock.TRANSIENT_POLL_LIMIT + 1) - {mock.TRANSIENT_ALERTNAME}

    # source-a stays a pure function of (endpoint, scenario): one source's probe
    # must not make the HA merge path time-dependent.
    assert mock.build_source_alerts(
        "source-a", "endpoint-1", poll_count=1
    ) == mock.build_source_alerts("source-a", "endpoint-1", poll_count=99)


def test_fault_scenarios_describe_partial_failed_and_recovery() -> None:
    mock = _mock_module()
    assert mock.Handler._source_endpoint_should_fail(
        "source-a", "endpoint-2", "partial_fail"
    )
    assert not mock.Handler._source_endpoint_should_fail(
        "source-a", "endpoint-1", "partial_fail"
    )
    assert mock.Handler._source_endpoint_should_fail(
        "source-b", "endpoint-1", "all_fail"
    )
    recovered = mock.build_source_alerts(
        "source-a", "endpoint-1", scenario="recovery"
    )
    assert recovered
    assert {item["labels"]["alertname"] for item in recovered} == {"Watchdog"}


def test_noise_acceptance_scenarios_are_source_bound_and_repeatable() -> None:
    mock = _mock_module()
    first = mock.build_source_alerts(
        "source-a", "endpoint-1", scenario="noise_burst"
    )
    second = mock.build_source_alerts(
        "source-a", "endpoint-2", scenario="noise_burst"
    )
    first_burst = {
        item["fingerprint"]
        for item in first
        if item["labels"]["alertname"] == "NoiseBurstAlert"
    }
    second_burst = {
        item["fingerprint"]
        for item in second
        if item["labels"]["alertname"] == "NoiseBurstAlert"
    }
    assert len(first_burst) == 12
    assert first_burst == second_burst
    assert all(item.startswith("source-a-") for item in first_burst)

    flap_cycles = [
        mock.build_source_alerts(
            "source-b", "endpoint-1", scenario="noise_flapping", poll_count=count
        )
        for count in range(1, 5)
    ]
    flap_present = [
        any(item["labels"]["alertname"] == "NoiseFlappingAlert" for item in alerts)
        for alerts in flap_cycles
    ]
    assert flap_present == [True, False, False, True]


def test_watchdog_and_thanos_overlap_scenarios() -> None:
    mock = _mock_module()
    missing = mock.build_source_alerts(
        "source-a", "endpoint-1", scenario="watchdog_missing"
    )
    assert all(item["labels"]["alertname"] != "Watchdog" for item in missing)
    ignored = mock.build_source_alerts(
        "source-a", "endpoint-1", scenario="watchdog_ignored"
    )
    assert any(item["labels"].get("cluster") == "ignored-cluster" for item in ignored)
    overlap = mock.build_thanos_series(scenario="overlap")
    assert len(overlap) > len(mock.build_thanos_series())
    assert any(item.get("source_scope") == "overlap" for item in overlap)


def test_thanos_catalog_endpoints_cover_planner_describe_metric() -> None:
    """The mock must implement every read used by DescribeMetric.

    A successful zero-hop query is not enough: the Planner subsequently reads
    metadata, label names, and selected label values. Missing either label
    endpoint used to turn the documented source-a positive path into a 404 and
    a SOURCE_UNAVAILABLE P1 step.
    """
    mock = _mock_module()

    class DummyHandler(mock.Handler):
        def __init__(self, path: str) -> None:
            self.path = path
            self.statuses: list[int] = []
            self.payloads: list[object] = []

        def _json(self, payload: object, status: int = 200) -> None:
            self.statuses.append(status)
            self.payloads.append(payload)

        def send_response(self, code: int, message: str | None = None) -> None:
            del message
            self.statuses.append(code)

        def end_headers(self) -> None:
            return None

    labels = DummyHandler(
        "/api/v1/labels?match%5B%5D=labelsonlyidentity_ratio"
    )
    labels.do_GET()
    assert labels.statuses == [200]
    assert labels.payloads == [
        {"status": "success", "data": ["__name__", "cluster", "instance"]}
    ]

    values = DummyHandler(
        "/api/v1/label/cluster/values?match%5B%5D=labelsonlyidentity_ratio"
    )
    values.do_GET()
    assert values.statuses == [200]
    assert values.payloads == [
        {"status": "success", "data": ["source-a-cluster"]}
    ]


def test_control_endpoints_keep_source_scenarios_independent() -> None:
    mock = _mock_module()
    mock.Handler.source_scenarios = {
        source_id: "baseline" for source_id in mock.SOURCE_IDS
    }
    mock.Handler.source_endpoint_poll_counts = {
        f"{source_id}:{endpoint_id}": 0
        for source_id, endpoint_ids in mock.SOURCE_ENDPOINTS.items()
        for endpoint_id in endpoint_ids
    }
    mock.Handler.thanos_scenario = "baseline"

    class DummyHandler(mock.Handler):
        responses: list[tuple[int, object]]

        def __init__(self) -> None:
            self.responses = []

        def _json(self, payload: object, status: int = 200) -> None:
            self.responses.append((status, payload))

    handler = DummyHandler()
    mock.Handler._set_source_scenario(
        handler,
        "source-a",
        {"scenario": "partial_fail"},
    )

    assert handler.responses == [
        (200, {"source": "source-a", "scenario": "partial_fail"})
    ]
    assert mock.Handler.source_scenarios["source-a"] == "partial_fail"
    assert mock.Handler.source_scenarios["source-b"] == "baseline"
    assert mock.Handler._source_endpoint(
        "/source-a/endpoint-2/api/v2/alerts"
    ) == ("source-a", "endpoint-2")
    assert mock.Handler._source_endpoint("/source-b/endpoint-2/api/v2/alerts") is None
