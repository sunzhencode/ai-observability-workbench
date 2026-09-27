"""F27 evidence reads on the Thanos client: shape, boundedness, and no back door.

Every request is served by a MockTransport — this suite never touches a network.
The structural test at the bottom is the load-bearing one: it fails if anyone
adds a method that would accept a caller-supplied URL, because that is what makes
the read-only boundary auditable (ADR 0009 / AGENTS.md).
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.sources import thanos as thanos_module
from app.sources.thanos import ThanosClient, safe_error_code

ADDRESS = "https://thanos.internal.example:10902"
END = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
START = END - timedelta(hours=2)

_CAPTURED: list[httpx.Request] = []


def _client(handler) -> ThanosClient:
    def recording(request: httpx.Request) -> httpx.Response:
        _CAPTURED.append(request)
        return handler(request)

    return ThanosClient(
        base_url=ADDRESS, token="tok", transport=httpx.MockTransport(recording)
    )


@pytest.fixture(autouse=True)
def _clear_captured():
    _CAPTURED.clear()
    yield
    _CAPTURED.clear()


def _ok(data):
    return lambda request: httpx.Response(200, json={"status": "success", "data": data})


# --------------------------------------------------------------------------
# query_range / query_instant
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_range_sends_the_expression_verbatim() -> None:
    client = _client(_ok({"resultType": "matrix", "result": []}))
    await client.query_range("up{job=\"x\"}", START, END, 60)

    request = _CAPTURED[-1]
    assert request.method == "GET"
    assert request.url.path == "/api/v1/query_range"
    assert request.url.params["query"] == 'up{job="x"}'
    assert request.url.params["step"] == "60"


@pytest.mark.asyncio
async def test_query_range_floors_a_zero_step() -> None:
    """A zero step would make the upstream reject the request outright."""

    client = _client(_ok({"resultType": "matrix", "result": []}))
    await client.query_range("up", START, END, 0)
    assert _CAPTURED[-1].url.params["step"] == "1"


@pytest.mark.asyncio
async def test_query_instant_hits_the_instant_endpoint() -> None:
    client = _client(_ok({"resultType": "vector", "result": []}))
    await client.query_instant("up", END)
    assert _CAPTURED[-1].url.path == "/api/v1/query"
    assert _CAPTURED[-1].url.params["time"] == str(END.timestamp())


@pytest.mark.asyncio
async def test_non_success_status_is_rejected() -> None:
    client = _client(lambda r: httpx.Response(200, json={"status": "error"}))
    with pytest.raises(ValueError):
        await client.query_range("up", START, END, 60)


@pytest.mark.asyncio
async def test_unexpected_payload_shape_is_rejected() -> None:
    """A success envelope around the wrong body must not reach the caller."""

    client = _client(_ok(["not", "a", "matrix"]))
    with pytest.raises(ValueError):
        await client.query_range("up", START, END, 60)


# --------------------------------------------------------------------------
# series / metric_names
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_series_passes_matchers_and_bounds_the_limit() -> None:
    client = _client(_ok([{"__name__": "up", "job": "a"}, {"__name__": "up", "job": "b"}]))
    rows = await client.series(['up{job="a"}'], START, END, limit=1)

    assert _CAPTURED[-1].url.path == "/api/v1/series"
    assert _CAPTURED[-1].url.params.get_list("match[]") == ['up{job="a"}']
    assert len(rows) == 1, "the limit must be applied to the response, not just sent"


@pytest.mark.asyncio
async def test_series_limit_floor_is_one() -> None:
    client = _client(_ok([{"__name__": "up"}]))
    await client.series(["up"], START, END, limit=0)
    assert _CAPTURED[-1].url.params["limit"] == "1"


@pytest.mark.asyncio
async def test_metric_names_returns_plain_strings() -> None:
    client = _client(_ok(["up", "node_load1", 42]))
    names = await client.metric_names(START, END)
    assert names == ["up", "node_load1", "42"]
    assert _CAPTURED[-1].url.path == "/api/v1/label/__name__/values"


@pytest.mark.asyncio
async def test_metric_names_rejects_a_non_list_payload() -> None:
    client = _client(_ok({"not": "a list"}))
    with pytest.raises(ValueError):
        await client.metric_names(START, END)


# --------------------------------------------------------------------------
# metric_metadata — the counter/gauge decision
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metadata_returns_lowercased_type_and_help() -> None:
    client = _client(
        _ok({"x_total": [{"type": "COUNTER", "help": "Total things", "unit": ""}]})
    )
    meta = await client.metric_metadata("x_total")
    assert meta == {"type": "counter", "help": "Total things", "unit": ""}


@pytest.mark.asyncio
async def test_metadata_absent_for_this_metric_is_none_not_an_error() -> None:
    """Stores routinely lack metadata; that is "unknown type", not a failure."""

    client = _client(_ok({}))
    assert await client.metric_metadata("x") is None


@pytest.mark.asyncio
async def test_metadata_empty_entry_list_is_none() -> None:
    client = _client(_ok({"x": []}))
    assert await client.metric_metadata("x") is None


# --------------------------------------------------------------------------
# alert_rules — the authoritative expression source
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_label_names_returns_the_metrics_real_schema() -> None:
    """D35: a fixed allowlist drops exporter-specific labels like `color`."""

    client = _client(_ok(["__name__", "cluster", "color", "instance"]))
    names = await client.metric_label_names("elasticsearch_cluster_health_status", START, END)

    assert names == ["__name__", "cluster", "color", "instance"]
    assert _CAPTURED[-1].url.path == "/api/v1/labels"
    assert _CAPTURED[-1].url.params.get_list("match[]") == [
        "elasticsearch_cluster_health_status"
    ]


@pytest.mark.asyncio
async def test_label_names_rejects_a_non_list_payload() -> None:
    client = _client(_ok({"not": "a list"}))
    with pytest.raises(ValueError):
        await client.metric_label_names("x", START, END)


@pytest.mark.asyncio
async def test_label_values_reports_what_a_label_really_contains() -> None:
    """ADR 0012: query authoring has to see the real values, or the model falls
    back to guessing namespaces from training data."""

    client = _client(_ok(["kube-system", "monitoring", "nonprod-infra"]))
    values = await client.metric_label_values("kube_pod_info", "namespace", START, END)

    assert values == ["kube-system", "monitoring", "nonprod-infra"]
    assert _CAPTURED[-1].url.path == "/api/v1/label/namespace/values"
    assert _CAPTURED[-1].url.params.get_list("match[]") == ["kube_pod_info"]


@pytest.mark.asyncio
async def test_a_label_name_is_encoded_into_the_path_not_interpolated() -> None:
    """The label name reaches a URL path, so it is escaped rather than pasted."""

    client = _client(_ok([]))
    await client.metric_label_values("up", "we/ird name", START, END)
    # `.path` decodes on the way out, so assert on the bytes actually sent:
    # an unescaped `/` here would silently address a different endpoint.
    raw = _CAPTURED[-1].url.raw_path.decode()
    assert raw.startswith("/api/v1/label/we%2Fird%20name/values")


@pytest.mark.asyncio
async def test_label_values_rejects_a_non_list_payload() -> None:
    client = _client(_ok({"not": "a list"}))
    with pytest.raises(ValueError):
        await client.metric_label_values("x", "y", START, END)


@pytest.mark.asyncio
async def test_alert_rules_narrows_server_side_when_given_a_name() -> None:
    """D31: fetching one rule must not mean paging the whole catalogue."""

    client = _client(_ok({"groups": []}))
    await client.alert_rules(alertname="ESClusterHealth")

    params = _CAPTURED[-1].url.params
    assert params.get_list("rule_name[]") == ["ESClusterHealth"]
    assert params["exclude_alerts"] == "true"


@pytest.mark.asyncio
async def test_alert_rules_without_a_name_asks_for_everything() -> None:
    client = _client(_ok({"groups": []}))
    await client.alert_rules()
    assert "rule_name[]" not in _CAPTURED[-1].url.params


@pytest.mark.asyncio
async def test_alert_rules_flattens_groups_and_drops_recording_rules() -> None:
    client = _client(
        _ok(
            {
                "groups": [
                    {
                        "name": "g1",
                        "rules": [
                            {"type": "alerting", "name": "A", "query": "up == 0"},
                            {"type": "recording", "name": "r", "query": "sum(up)"},
                        ],
                    },
                    {
                        "name": "g2",
                        "rules": [{"type": "alerting", "name": "B", "query": "x > 1"}],
                    },
                ]
            }
        )
    )
    rules = await client.alert_rules()
    assert [rule["name"] for rule in rules] == ["A", "B"]
    assert _CAPTURED[-1].url.params["type"] == "alert"


@pytest.mark.asyncio
async def test_alert_rules_empty_on_a_deployment_without_a_ruler() -> None:
    """Thanos Query with no Ruler behind it answers with no groups.

    This is the whole reason the `generatorURL` fallback stays in the design: an
    empty list here is a normal deployment shape, not an error.
    """

    client = _client(_ok({"groups": []}))
    assert await client.alert_rules() == []


@pytest.mark.asyncio
async def test_alert_rules_tolerates_a_missing_groups_key() -> None:
    client = _client(_ok({}))
    assert await client.alert_rules() == []


# --------------------------------------------------------------------------
# shared guarantees
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evidence_reads_never_follow_redirects() -> None:
    def redirecting(request: httpx.Response) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://elsewhere.example/x"})

    client = _client(redirecting)
    # Named rather than generic — see the redirect test at the bottom.
    with pytest.raises(thanos_module.RedirectRejectedError):
        await client.query_range("up", START, END, 60)


@pytest.mark.asyncio
async def test_evidence_reads_stay_within_the_response_budget() -> None:
    oversized = "x" * (thanos_module.MAX_THANOS_RESPONSE_BYTES + 1024)
    client = _client(lambda r: httpx.Response(200, content=oversized.encode()))
    with pytest.raises(thanos_module.ResponseTooLargeError):
        await client.query_range("up", START, END, 60)


@pytest.mark.asyncio
async def test_evidence_read_failures_are_safe_to_surface() -> None:
    """`str(exc)` carries the address; only the classified code may leave."""

    client = _client(lambda r: httpx.Response(500, json={"status": "error"}))
    with pytest.raises(Exception) as caught:
        await client.query_range("up", START, END, 60)
    assert "thanos.internal.example" in str(caught.value)
    assert safe_error_code(caught.value) == "HTTP_500"


@pytest.mark.asyncio
async def test_all_evidence_reads_are_get_requests() -> None:
    client = _client(_ok({"resultType": "matrix", "result": []}))
    await client.query_range("up", START, END, 60)
    client = _client(_ok({"resultType": "vector", "result": []}))
    await client.query_instant("up", END)
    client = _client(_ok([]))
    await client.series(["up"], START, END, limit=5)
    client = _client(_ok([]))
    await client.metric_names(START, END)
    client = _client(_ok({}))
    await client.metric_metadata("up")
    client = _client(_ok([]))
    await client.metric_label_names("up", START, END)
    client = _client(_ok([]))
    await client.metric_label_values("up", "instance", START, END)
    client = _client(_ok({"groups": []}))
    await client.alert_rules()

    assert _CAPTURED, "the transport recorded nothing"
    assert {request.method for request in _CAPTURED} == {"GET"}


def test_client_exposes_no_arbitrary_url_method() -> None:
    """The read-only boundary must stay readable from this class alone.

    A method taking a caller-supplied URL or path would mean you can no longer
    tell what the backend is allowed to ask for by reading `ThanosClient`. The
    private `_get` takes a path, but only this module may call it — every public
    entry point names its own endpoint.
    """

    public = [
        name
        for name, member in inspect.getmembers(ThanosClient, inspect.isfunction)
        if not name.startswith("_")
    ]
    assert set(public) == {
        "probe",
        "query_alerts",
        "series_alerts",
        "query_range",
        "query_instant",
        "series",
        "metric_names",
        "metric_metadata",
        "metric_label_names",
        "metric_label_values",
        "alert_rules",
    }, "a new public read was added: give it its own endpoint and update this list"

    for name in public:
        params = set(inspect.signature(getattr(ThanosClient, name)).parameters)
        assert not params & {
            "url",
            "path",
            "endpoint",
        }, f"{name} accepts a caller-supplied address"


def test_existing_alert_reads_still_use_their_frozen_selectors() -> None:
    """F27 must not change what the pre-existing backfill reads ask for."""

    assert thanos_module.ALERTS_QUERY == 'ALERTS{alertstate=~"firing|pending"}'
    assert thanos_module.ALERTS_SERIES_MATCHER == 'ALERTS{alertname!=""}'


@pytest.mark.asyncio
async def test_a_redirect_is_named_rather_than_called_an_invalid_response() -> None:
    """An ingress sending http to https makes *every* read fail identically.

    Collapsing that into "invalid response" hides the one fix the reader can
    apply — configure the address with the scheme the ingress actually serves.
    """

    client = _client(
        lambda request: httpx.Response(301, headers={"location": "https://elsewhere/x"})
    )
    with pytest.raises(thanos_module.RedirectRejectedError):
        await client.query_range("up", START, END, 60)

    with pytest.raises(Exception) as caught:
        await client.metric_names(START, END)
    assert safe_error_code(caught.value) == "REDIRECT_REJECTED"


@pytest.mark.asyncio
async def test_a_route_prefix_in_the_address_is_preserved() -> None:
    """Thanos behind `--web.route-prefix=/thanos` is an ordinary deployment.

    The address is stored verbatim (no normalisation strips the path), and every
    read composes `base_url + /api/v1/...`. A real environment turned out to need
    exactly this, and the symptom of getting it wrong is a bare HTTP_404 on every
    endpoint at once — worth a test so nobody "tidies" the path away later.
    """

    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.url.path)
        return httpx.Response(
            200, json={"status": "success", "data": {"resultType": "matrix", "result": []}}
        )

    client = ThanosClient(
        base_url="https://thanos.example/thanos",
        transport=httpx.MockTransport(handler),
    )
    await client.query_range("up", START, END, 60)
    assert captured[-1] == "/thanos/api/v1/query_range"

    await client.probe()
    assert captured[-1] == "/thanos/api/v1/query"


@pytest.mark.asyncio
async def test_a_trailing_slash_does_not_double_up() -> None:
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.url.path)
        return httpx.Response(200, json={"status": "success", "data": []})

    client = ThanosClient(
        base_url="https://thanos.example/thanos/", transport=httpx.MockTransport(handler)
    )
    await client.metric_names(START, END)
    assert captured[-1] == "/thanos/api/v1/label/__name__/values"
