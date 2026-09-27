"""The read-only Grafana client. Entirely offline, driven by MockTransport.

The point of this file is the *shape* of the client as much as its behaviour.
Grafana is the third read-only monitoring source, alongside Alertmanager and
Thanos, and its boundary is: GET only, one thin method per endpoint, no
"pass any URL" method, bounded response, timeout, no redirect following.

It is **not** an outbound target, so the egress guard that rejects loopback and
private addresses must not be anywhere near it — the user's Grafana is on the
internal network, and applying that guard would make this unusable on day one
(ADR 0013). `test_a_private_address_is_a_normal_address` pins exactly that.
"""

from __future__ import annotations

import inspect
import json

import httpx
import pytest

from app.sources.grafana_dashboards import (
    MAX_GRAFANA_RESPONSE_BYTES,
    GrafanaDashboardClient,
    GrafanaError,
)

SEARCH_RESULT = [
    {"uid": "mysql-overview", "title": "MySQL Overview", "type": "dash-db"},
    {"uid": "node-exporter", "title": "Node Exporter", "type": "dash-db"},
]

DASHBOARD = {
    "dashboard": {"uid": "mysql-overview", "title": "MySQL Overview", "panels": []},
    "meta": {"folderTitle": "Databases"},
}


def _client(handler, **kwargs) -> GrafanaDashboardClient:
    return GrafanaDashboardClient(
        base_url=kwargs.pop("base_url", "http://grafana.test"),
        token=kwargs.pop("token", "svc-token"),
        timeout_seconds=kwargs.pop("timeout_seconds", 15),
        transport=httpx.MockTransport(handler),
    )


class TestSearchDashboards:
    @pytest.mark.asyncio
    async def test_it_returns_the_dashboard_summaries(self) -> None:
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["method"] = request.method
            return httpx.Response(200, json=SEARCH_RESULT)

        rows = await _client(handler).search_dashboards("mysql", limit=10)

        assert [row["uid"] for row in rows] == ["mysql-overview", "node-exporter"]
        assert seen["method"] == "GET"
        assert "/api/search" in str(seen["url"])
        assert "type=dash-db" in str(seen["url"])
        assert "query=mysql" in str(seen["url"])
        assert "limit=10" in str(seen["url"])

    @pytest.mark.asyncio
    async def test_an_empty_query_lists_what_is_there(self) -> None:
        """Also the connectivity test's request: cheap, read-only, universal."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=SEARCH_RESULT)

        rows = await _client(handler).search_dashboards("", limit=1)

        assert len(rows) == 2

    @pytest.mark.asyncio
    async def test_a_non_list_payload_is_a_readable_failure(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"message": "nope"})

        with pytest.raises(GrafanaError) as excinfo:
            await _client(handler).search_dashboards("x")

        assert excinfo.value.code == "GRAFANA_NOT_JSON"


class TestGetDashboard:
    @pytest.mark.asyncio
    async def test_it_unwraps_the_dashboard_key(self) -> None:
        """Grafana wraps the definition in `{dashboard, meta}`; callers want the
        definition. Unwrapping here keeps every parser downstream from having to
        know the envelope."""

        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, json=DASHBOARD)

        result = await _client(handler).get_dashboard("mysql-overview")

        assert result["title"] == "MySQL Overview"
        assert str(seen["url"]).endswith("/api/dashboards/uid/mysql-overview")

    @pytest.mark.asyncio
    async def test_a_missing_dashboard_key_is_a_readable_failure(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"meta": {}})

        with pytest.raises(GrafanaError) as excinfo:
            await _client(handler).get_dashboard("abc")

        assert excinfo.value.code == "GRAFANA_NOT_JSON"

    @pytest.mark.parametrize(
        "uid",
        ["", "../../etc/passwd", "abc/def", "a b", "x" * 65, "uid?query=1", "a%2f"],
    )
    @pytest.mark.asyncio
    async def test_a_uid_that_is_not_a_uid_never_reaches_the_network(
        self, uid: str
    ) -> None:
        """The uid is interpolated into the URL path, so it is validated first.

        Rejecting before the request rather than escaping during it: escaping is
        a property of one call site, a whitelist is a property of the client.
        """

        called = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            return httpx.Response(200, json=DASHBOARD)

        with pytest.raises(GrafanaError) as excinfo:
            await _client(handler).get_dashboard(uid)

        assert excinfo.value.code == "GRAFANA_INVALID_UID"
        assert called is False


class TestFailureClassification:
    @pytest.mark.asyncio
    async def test_a_redirect_is_refused_rather_than_followed(self) -> None:
        """An ingress redirecting http to https makes *every* read fail the same
        way, and the fix is to configure the address with the scheme actually
        served. A generic "invalid response" hides that."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": "https://elsewhere.test"})

        with pytest.raises(GrafanaError) as excinfo:
            await _client(handler).search_dashboards("x")

        assert excinfo.value.code == "GRAFANA_REDIRECT"

    @pytest.mark.parametrize("status", [401, 403])
    @pytest.mark.asyncio
    async def test_an_unauthorized_answer_says_how_to_get_a_token(
        self, status: int
    ) -> None:
        """Design §4: anonymous read is normal on internal Grafana, so a 401 is
        not "you configured it wrong" — it is "this one needs a token", and the
        message has to say which kind."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, json={"message": "Unauthorized"})

        with pytest.raises(GrafanaError) as excinfo:
            await _client(handler).search_dashboards("x")

        assert excinfo.value.code == "GRAFANA_AUTH_REQUIRED"
        assert "service account" in excinfo.value.message
        assert "Viewer" in excinfo.value.message

    @pytest.mark.asyncio
    async def test_a_missing_uid_is_its_own_failure(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"message": "Dashboard not found"})

        with pytest.raises(GrafanaError) as excinfo:
            await _client(handler).get_dashboard("gone")

        assert excinfo.value.code == "GRAFANA_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_a_timeout_is_its_own_failure(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("too slow", request=request)

        with pytest.raises(GrafanaError) as excinfo:
            await _client(handler).search_dashboards("x")

        assert excinfo.value.code == "GRAFANA_TIMEOUT"

    @pytest.mark.asyncio
    async def test_an_unreachable_host_is_its_own_failure(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route", request=request)

        with pytest.raises(GrafanaError) as excinfo:
            await _client(handler).search_dashboards("x")

        assert excinfo.value.code == "GRAFANA_UNREACHABLE"

    @pytest.mark.asyncio
    async def test_a_body_over_the_cap_fails_instead_of_yielding_half(self) -> None:
        """Dashboard JSON can be very large; an unbounded read is a hole in local
        memory. The refusal must be total — half a dashboard parses into a
        plausible-looking set of candidates that is simply missing panels."""

        oversized = json.dumps(
            [{"uid": f"d{i}", "title": "x" * 512} for i in range(40_000)]
        ).encode()
        assert len(oversized) > MAX_GRAFANA_RESPONSE_BYTES

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=oversized)

        with pytest.raises(GrafanaError) as excinfo:
            await _client(handler).search_dashboards("x")

        assert excinfo.value.code == "GRAFANA_RESPONSE_TOO_LARGE"

    @pytest.mark.asyncio
    async def test_an_html_answer_is_not_reported_as_a_syntax_problem(self) -> None:
        """A login page served with 200 is the classic shape of this failure."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"<html><body>Login</body></html>")

        with pytest.raises(GrafanaError) as excinfo:
            await _client(handler).search_dashboards("x")

        assert excinfo.value.code == "GRAFANA_NOT_JSON"

    @pytest.mark.asyncio
    async def test_a_failure_never_carries_the_address_back_out(self) -> None:
        """`str(exc)` on an httpx error contains the full URL — host, port and
        query. That must not reach an API response (SYSTEM_SPEC §7)."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connect to 10.9.9.9:3000 failed", request=request)

        client = _client(handler, base_url="http://grafana-internal.corp:3000")
        with pytest.raises(GrafanaError) as excinfo:
            await client.search_dashboards("x")

        rendered = f"{excinfo.value.code} {excinfo.value.message} {excinfo.value}"
        assert "grafana-internal.corp" not in rendered
        assert "10.9.9.9" not in rendered


class TestBoundary:
    @pytest.mark.asyncio
    async def test_a_private_address_is_a_normal_address(self) -> None:
        """**The one that matters most.** Grafana is a read-only monitoring
        source, not an outbound target: it lives on the internal network, so a
        private address is its normal form, not a threat to reject.

        If someone ever wires `providers/egress.py` into this client, this test
        goes red — which is the whole reason it exists.
        """

        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["host"] = request.url.host
            return httpx.Response(200, json=SEARCH_RESULT)

        for address in (
            "http://10.0.0.5:3000",
            "http://192.168.1.20:3000",
            "http://172.16.4.4:3000",
            "http://127.0.0.1:3000",
            "http://grafana.monitoring.svc.cluster.local:3000",
        ):
            client = _client(handler, base_url=address)
            rows = await client.search_dashboards("mysql")
            assert len(rows) == 2

    @pytest.mark.asyncio
    async def test_plain_http_is_accepted(self) -> None:
        """Internal Grafana routinely has no TLS. Requiring HTTPS here would be
        importing the outbound rule into the monitoring-source side."""

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.scheme == "http"
            return httpx.Response(200, json=SEARCH_RESULT)

        assert await _client(handler, base_url="http://10.0.0.5:3000").search_dashboards(
            ""
        )

    def test_the_client_does_not_reach_for_either_egress_guard(self) -> None:
        """Checked in the source text, because an import is how it would arrive.

        The two guard modules protect outbound targets from being pointed at
        internal addresses. Applying either one here inverts its purpose.
        """

        import app.sources.grafana_dashboards as module

        source = inspect.getsource(module)
        assert "providers.egress" not in source
        assert "providers.model.egress" not in source
        assert "from app.providers" not in source
        assert "import app.providers" not in source

    def test_the_public_surface_is_exactly_two_methods(self) -> None:
        """No third method, and above all no "pass any path" method.

        A general-purpose method makes the read-only boundary unauditable: you
        could no longer tell by reading the class what the backend may ask for.
        Adding an endpoint means adding a method here — deliberately a visible
        act (same rule as `ThanosClient`, CAP-06.2a).
        """

        public = {
            name
            for name, value in vars(GrafanaDashboardClient).items()
            if not name.startswith("_") and callable(value)
        }
        assert public == {"search_dashboards", "get_dashboard"}

    def test_no_method_accepts_a_url_or_path_argument(self) -> None:
        for name in ("search_dashboards", "get_dashboard"):
            params = set(
                inspect.signature(getattr(GrafanaDashboardClient, name)).parameters
            )
            assert not params & {"url", "path", "endpoint", "route"}

    @pytest.mark.asyncio
    async def test_redirects_are_disabled_on_the_transport_itself(self) -> None:
        """Not just detected after the fact: the client never follows one."""

        hops = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal hops
            hops += 1
            return httpx.Response(301, headers={"location": "http://grafana.test/x"})

        with pytest.raises(GrafanaError):
            await _client(handler).search_dashboards("x")

        assert hops == 1

    @pytest.mark.asyncio
    async def test_a_token_is_sent_as_a_bearer_credential(self) -> None:
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json=SEARCH_RESULT)

        await _client(handler, token="svc-token").search_dashboards("x")

        assert seen["auth"] == "Bearer svc-token"

    @pytest.mark.asyncio
    async def test_no_token_means_no_authorization_header(self) -> None:
        """Anonymous read is a supported configuration, not a broken one: an
        empty credential must produce a bare request, not `Bearer `."""

        seen: dict[str, object] = {"auth": "unset"}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json=SEARCH_RESULT)

        await _client(handler, token=None).search_dashboards("x")

        assert seen["auth"] is None

    def test_an_address_without_a_base_url_is_unconfigured(self) -> None:
        assert GrafanaDashboardClient(base_url="").configured is False
        assert GrafanaDashboardClient(base_url="http://10.0.0.5:3000").configured

    @pytest.mark.asyncio
    async def test_an_unconfigured_client_refuses_before_any_request(self) -> None:
        client = GrafanaDashboardClient(base_url="")

        with pytest.raises(GrafanaError) as excinfo:
            await client.search_dashboards("x")

        assert excinfo.value.code == "GRAFANA_NOT_CONFIGURED"
