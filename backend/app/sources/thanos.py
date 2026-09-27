"""Read-only Thanos client used only for bounded ALERTS history queries."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx


# The two selectors this client ever sends. They used to be assembled by a
# helper that also accepted caller-supplied matchers and interpolated their
# label and operator into the PromQL unvalidated -- only the value was escaped.
# No caller ever passed any, so it was an injection site kept warm for whoever
# came next. Selectors are constants now; a future scoped query should build
# them from validated matcher parts, not from raw strings.
ALERTS_QUERY = 'ALERTS{alertstate=~"firing|pending"}'
ALERTS_SERIES_MATCHER = 'ALERTS{alertname!=""}'

# Bounded like the Alertmanager client, just with a bigger budget: a 24h matrix
# at 60s step is ~1440 samples per series, roughly 36 KB of JSON each, so this
# holds a few hundred concurrently firing series. Past that the read is refused
# rather than parsed into memory.
MAX_THANOS_RESPONSE_BYTES = 16 * 1024 * 1024


class ResponseTooLargeError(ValueError):
    pass


class RedirectRejectedError(ValueError):
    """The upstream answered with a redirect, which this client never follows.

    Named separately because the fix is specific and the generic
    "invalid response" hides it: an ingress that sends `http` to `https` makes
    **every** read fail identically, and the answer is to configure the address
    with the scheme the ingress actually serves.
    """


def safe_error_code(exc: BaseException) -> str:
    """Classify a failure without leaking the address or the remote body.

    `str(exc)` on an httpx error carries the full URL -- host, port and query --
    which must not reach an API response (SYSTEM_SPEC §7).
    """
    if isinstance(exc, httpx.TimeoutException):
        return "TIMEOUT"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP_{exc.response.status_code}"
    if isinstance(exc, httpx.HTTPError):
        return "NETWORK"
    if isinstance(exc, RedirectRejectedError):
        return "REDIRECT_REJECTED"
    if isinstance(exc, ResponseTooLargeError):
        return "RESPONSE_TOO_LARGE"
    if isinstance(exc, ValueError):
        return "INVALID_RESPONSE"
    return "UNAVAILABLE"


async def _read_bounded_json(
    client: httpx.AsyncClient, url: str, *, headers: dict[str, str], params: dict
) -> Any:
    """GET and parse JSON, refusing anything over the cap without buffering it."""
    async with client.stream("GET", url, headers=headers, params=params) as response:
        # `raise_for_status` only covers 4xx/5xx. A 3xx would otherwise fall
        # through to `json.loads` on an HTML body and surface as a vague
        # "invalid response", hiding a fix the reader could actually act on.
        if 300 <= response.status_code < 400:
            raise RedirectRejectedError(
                "upstream redirected; this client never follows redirects"
            )
        response.raise_for_status()
        length = response.headers.get("content-length")
        if length is not None and int(length) > MAX_THANOS_RESPONSE_BYTES:
            raise ResponseTooLargeError("Thanos response exceeds the read budget")
        content = bytearray()
        async for chunk in response.aiter_bytes():
            content.extend(chunk)
            if len(content) > MAX_THANOS_RESPONSE_BYTES:
                raise ResponseTooLargeError(
                    "Thanos response exceeds the read budget"
                )
    return json.loads(content)


class ThanosClient:
    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        timeout: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # F21: the connection is passed in from a HistoricalDataSource revision.
        # This used to fall back to the `.env` snapshot, which was the last place
        # a data source address could hide outside the registry. An omitted
        # base_url now means "unconfigured", not "read the environment".
        self.base_url = (base_url or "").rstrip("/")
        self.token = token or ""
        self.timeout = timeout if timeout is not None else 15.0
        self.transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def probe(self) -> tuple[bool, str]:
        """Read-only reachability check for explicit configuration testing.

        Uses an instant query of the constant 1: universally available on Thanos
        Query, cheap, and able to tell "reachable but unauthorized" (HTTP 401/403)
        apart from "unreachable". Returns a safe code only -- never response
        bodies, URLs or credentials.
        """
        if not self.configured:
            return False, "NOT_CONFIGURED"
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                transport=self.transport,
                follow_redirects=False,
            ) as client:
                response = await client.get(
                    f"{self.base_url}/api/v1/query",
                    headers=self._headers(),
                    params={"query": "1"},
                )
        except httpx.TimeoutException:
            return False, "TIMEOUT"
        except httpx.HTTPError:
            return False, "NETWORK"
        if response.status_code in (401, 403):
            return False, "UNAUTHORIZED"
        if 300 <= response.status_code < 400:
            return False, "REDIRECT_REJECTED"
        if response.status_code >= 400:
            return False, f"HTTP_{response.status_code}"
        try:
            payload = response.json()
        except ValueError:
            return False, "INVALID_RESPONSE"
        if not isinstance(payload, dict) or payload.get("status") != "success":
            return False, "INVALID_RESPONSE"
        return True, "OK"

    async def query_alerts(
        self,
        start: datetime,
        end: datetime,
        step_seconds: int,
    ) -> dict[str, Any]:
        """GET a bounded ALERTS matrix. No write method exists in this client."""
        async with httpx.AsyncClient(
            timeout=self.timeout,
            transport=self.transport,
            follow_redirects=False,
        ) as client:
            payload = await _read_bounded_json(
                client,
                f"{self.base_url}/api/v1/query_range",
                headers=self._headers(),
                params={
                    "query": ALERTS_QUERY,
                    "start": start.timestamp(),
                    "end": end.timestamp(),
                    "step": max(1, step_seconds),
                },
            )
        if not isinstance(payload, dict) or payload.get("status") != "success":
            raise ValueError("Thanos query_range returned a non-success response")
        return payload

    async def series_alerts(
        self,
        start: datetime,
        end: datetime,
        limit: int,
    ) -> list[dict[str, str]]:
        """GET bounded historical ALERTS label sets for configuration discovery."""
        bounded_limit = max(1, limit)
        async with httpx.AsyncClient(
            timeout=self.timeout,
            transport=self.transport,
            follow_redirects=False,
        ) as client:
            payload = await _read_bounded_json(
                client,
                f"{self.base_url}/api/v1/series",
                headers=self._headers(),
                params={
                    "match[]": ALERTS_SERIES_MATCHER,
                    "start": start.timestamp(),
                    "end": end.timestamp(),
                    "limit": bounded_limit,
                },
            )
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(payload, dict) or payload.get("status") != "success" or not isinstance(data, list):
            raise ValueError("Thanos series returned a non-success response")
        rows: list[dict[str, str]] = []
        for item in data[:bounded_limit]:
            if isinstance(item, dict):
                rows.append({str(key): str(value) for key, value in item.items()})
        return rows

    # ------------------------------------------------------------------
    # F27 evidence reads (ADR 0009). CAP-06.2a finally uses the read
    # authorisation that CAP-06.2 always granted.
    #
    # **One thin method per endpoint, and never a "pass any URL" method.**
    # A general-purpose method would make the read-only boundary impossible to
    # audit -- you could no longer tell by reading this class what the backend
    # is allowed to ask for. Adding an endpoint means adding a method here.
    # ------------------------------------------------------------------

    async def _get(self, path: str, params: dict) -> Any:
        async with httpx.AsyncClient(
            timeout=self.timeout,
            transport=self.transport,
            follow_redirects=False,
        ) as client:
            return await _read_bounded_json(
                client,
                f"{self.base_url}{path}",
                headers=self._headers(),
                params=params,
            )

    @staticmethod
    def _require_success(payload: Any, endpoint: str) -> Any:
        if not isinstance(payload, dict) or payload.get("status") != "success":
            raise ValueError(f"Thanos {endpoint} returned a non-success response")
        return payload.get("data")

    async def query_range(
        self, query: str, start: datetime, end: datetime, step_seconds: int
    ) -> dict[str, Any]:
        """GET a matrix for an arbitrary read-only PromQL expression.

        The caller must already have passed the window through
        `services.metric_budget`; this method does not invent its own bounds.
        """
        payload = await self._get(
            "/api/v1/query_range",
            {
                "query": query,
                "start": start.timestamp(),
                "end": end.timestamp(),
                "step": max(1, int(step_seconds)),
            },
        )
        data = self._require_success(payload, "query_range")
        if not isinstance(data, dict):
            raise ValueError("Thanos query_range returned an unexpected payload")
        return data

    async def query_instant(
        self, query: str, at: datetime, limit: int | None = None
    ) -> dict[str, Any]:
        """GET a single evaluation of a read-only expression.

        `limit` is a **hint to the upstream, not the enforcement boundary**:
        older Prometheus and Thanos builds ignore it entirely. So this method
        never slices the result -- the caller asks for one more than its budget
        and rejects the whole answer when that sentinel comes back. Trimming
        here would turn "over budget" into something that looks fine.
        """
        params: dict[str, Any] = {"query": query, "time": at.timestamp()}
        if limit is not None:
            params["limit"] = max(1, int(limit))
        payload = await self._get("/api/v1/query", params)
        data = self._require_success(payload, "query")
        if not isinstance(data, dict):
            raise ValueError("Thanos query returned an unexpected payload")
        return data

    async def series(
        self, matchers: list[str], start: datetime, end: datetime, limit: int
    ) -> list[dict[str, str]]:
        """GET the label sets a selector actually resolves to.

        Used before charting to tell "this metric does not exist here" apart from
        "this metric exists but not under these labels" -- two failures that lead
        the reader somewhere completely different (CAP-12.7).
        """
        bounded_limit = max(1, int(limit))
        payload = await self._get(
            "/api/v1/series",
            {
                "match[]": list(matchers),
                "start": start.timestamp(),
                "end": end.timestamp(),
                "limit": bounded_limit,
            },
        )
        data = self._require_success(payload, "series")
        if not isinstance(data, list):
            raise ValueError("Thanos series returned an unexpected payload")
        rows: list[dict[str, str]] = []
        for item in data[:bounded_limit]:
            if isinstance(item, dict):
                rows.append({str(key): str(value) for key, value in item.items()})
        return rows

    async def metric_names(self, start: datetime, end: datetime) -> list[str]:
        """GET every metric name the store knows about in the window."""
        payload = await self._get(
            "/api/v1/label/__name__/values",
            {"start": start.timestamp(), "end": end.timestamp()},
        )
        data = self._require_success(payload, "label values")
        if not isinstance(data, list):
            raise ValueError("Thanos label values returned an unexpected payload")
        return [str(item) for item in data if isinstance(item, (str, int, float))]

    async def metric_metadata(self, metric: str) -> dict[str, str] | None:
        """GET a metric's type and help text, or None when the store has neither.

        The type is what decides whether a curve needs `rate()`. Without it a
        counter is drawn as a line that only ever climbs -- a chart that looks
        entirely normal and says nothing (ADR 0009).
        """
        payload = await self._get("/api/v1/metadata", {"metric": metric, "limit": 1})
        data = self._require_success(payload, "metadata")
        if not isinstance(data, dict):
            raise ValueError("Thanos metadata returned an unexpected payload")
        entries = data.get(metric)
        if not isinstance(entries, list) or not entries:
            return None
        first = entries[0]
        if not isinstance(first, dict):
            return None
        return {
            "type": str(first.get("type") or "").lower(),
            "help": str(first.get("help") or ""),
            "unit": str(first.get("unit") or ""),
        }

    async def metric_label_names(
        self, metric: str, start: datetime, end: datetime
    ) -> list[str]:
        """GET the label names that actually exist on one metric.

        This is what lets a rebuilt selector use the metric's *real* schema
        instead of a fixed allowlist -- an allowlist drops exporter-specific
        labels, and dropping the one that says which thing fired turns a curve
        about this alert into a curve about everything (ADR 0009 revision).
        """
        payload = await self._get(
            "/api/v1/labels",
            {
                "match[]": [metric],
                "start": start.timestamp(),
                "end": end.timestamp(),
            },
        )
        data = self._require_success(payload, "labels")
        if not isinstance(data, list):
            raise ValueError("Thanos labels returned an unexpected payload")
        return [str(item) for item in data if isinstance(item, str)]

    async def metric_label_values(
        self, metric: str, label: str, start: datetime, end: datetime
    ) -> list[str]:
        """GET the values one label actually takes on one metric.

        Added for query authoring (ADR 0012): a model asked to write PromQL for
        this alert has to see what `namespace` or `pod` really contain here,
        otherwise it falls back to guessing from training data -- which is the
        exact failure the whole "read the catalogue first" rule exists to stop.

        Still one thin method per endpoint (D12): the label name goes in the
        path, so it is percent-encoded rather than interpolated, and there is
        still no public method that takes a caller-supplied URL.
        """
        payload = await self._get(
            f"/api/v1/label/{quote(label, safe='')}/values",
            {
                "match[]": [metric],
                "start": start.timestamp(),
                "end": end.timestamp(),
            },
        )
        data = self._require_success(payload, "label values")
        if not isinstance(data, list):
            raise ValueError("Thanos label values returned an unexpected payload")
        return [str(item) for item in data if isinstance(item, str)]

    async def alert_rules(self, alertname: str | None = None) -> list[dict[str, Any]]:
        """GET alerting rule definitions, flattened out of their groups.

        This is the authoritative source for an alert's expression; the
        `generatorURL` fallback exists because this endpoint is empty on
        deployments where Thanos Query has no Ruler behind it.

        `alertname` narrows the request server-side where the upstream supports
        it. Older builds ignore the parameter and answer with everything, which
        is why the response stays bounded by the shared read budget and why the
        caller filters again rather than trusting the narrowing.
        """
        params: dict[str, Any] = {"type": "alert"}
        if alertname:
            params["rule_name[]"] = [alertname]
            params["exclude_alerts"] = "true"
        payload = await self._get("/api/v1/rules", params)
        data = self._require_success(payload, "rules")
        if not isinstance(data, dict):
            raise ValueError("Thanos rules returned an unexpected payload")
        groups = data.get("groups")
        if not isinstance(groups, list):
            return []
        rules: list[dict[str, Any]] = []
        for group in groups:
            if not isinstance(group, dict):
                continue
            for rule in group.get("rules") or []:
                if isinstance(rule, dict) and rule.get("type") == "alerting":
                    rules.append(rule)
        return rules
