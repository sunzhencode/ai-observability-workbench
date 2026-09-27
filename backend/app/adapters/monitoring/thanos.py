"""Thin bounded read-only Thanos methods for Incident Operations."""

from __future__ import annotations

from datetime import datetime
import json
from typing import Any
from urllib.parse import quote

import httpx

MAX_THANOS_RESPONSE_BYTES = 16 * 1024 * 1024
ALERTS_QUERY = 'ALERTS{alertstate=~"firing|pending"}'
ALERTS_SERIES_MATCHER = 'ALERTS{alertname!=""}'


class MonitoringReadError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class BoundedThanosReader:
    def __init__(
        self,
        base_url: str,
        *,
        token: str = "",
        timeout_seconds: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout_seconds
        self._transport = transport

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
                follow_redirects=False,
            ) as client:
                async with client.stream(
                    "GET", f"{self._base_url}{path}", headers=headers, params=params
                ) as response:
                    if 300 <= response.status_code < 400:
                        raise MonitoringReadError("THANOS_REDIRECT_REJECTED")
                    if response.status_code in {401, 403}:
                        raise MonitoringReadError("THANOS_AUTH_FAILED")
                    if response.status_code >= 400:
                        raise MonitoringReadError(f"THANOS_HTTP_{response.status_code}")
                    length = response.headers.get("content-length")
                    if length is not None and int(length) > MAX_THANOS_RESPONSE_BYTES:
                        raise MonitoringReadError("THANOS_RESPONSE_TOO_LARGE")
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > MAX_THANOS_RESPONSE_BYTES:
                            raise MonitoringReadError("THANOS_RESPONSE_TOO_LARGE")
        except httpx.TimeoutException:
            raise MonitoringReadError("THANOS_TIMEOUT") from None
        except httpx.HTTPError:
            raise MonitoringReadError("THANOS_UNREACHABLE") from None
        try:
            payload = json.loads(content)
        except ValueError:
            raise MonitoringReadError("THANOS_RESPONSE_INVALID") from None
        if not isinstance(payload, dict) or payload.get("status") != "success":
            raise MonitoringReadError("THANOS_RESPONSE_INVALID")
        return payload.get("data")

    async def probe(self) -> tuple[bool, str]:
        try:
            await self.query_instant("1", datetime.now().astimezone(), limit=1)
        except MonitoringReadError as exc:
            return False, exc.code
        return True, "OK"

    async def query_range(
        self, query: str, start: datetime, end: datetime, step_seconds: int
    ) -> dict[str, Any]:
        data = await self._get(
            "/api/v1/query_range",
            {
                "query": query,
                "start": start.timestamp(),
                "end": end.timestamp(),
                "step": max(1, step_seconds),
            },
        )
        if not isinstance(data, dict):
            raise MonitoringReadError("THANOS_RESPONSE_INVALID")
        return data

    async def query_alerts(
        self, start: datetime, end: datetime, step_seconds: int
    ) -> dict[str, Any]:
        """Historical ALERTS backfill through the same bounded range endpoint."""
        return await self.query_range(ALERTS_QUERY, start, end, step_seconds)

    async def series_alerts(
        self, start: datetime, end: datetime, *, limit: int
    ) -> tuple[dict[str, str], ...]:
        return await self.series((ALERTS_SERIES_MATCHER,), start, end, limit=limit)

    async def query_instant(
        self, query: str, at: datetime, *, limit: int | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"query": query, "time": at.timestamp()}
        if limit is not None:
            params["limit"] = max(1, limit)
        data = await self._get("/api/v1/query", params)
        if not isinstance(data, dict):
            raise MonitoringReadError("THANOS_RESPONSE_INVALID")
        return data

    async def series(
        self, matchers: tuple[str, ...], start: datetime, end: datetime, *, limit: int
    ) -> tuple[dict[str, str], ...]:
        data = await self._get(
            "/api/v1/series",
            {"match[]": list(matchers), "start": start.timestamp(), "end": end.timestamp(), "limit": limit},
        )
        if not isinstance(data, list):
            raise MonitoringReadError("THANOS_RESPONSE_INVALID")
        return tuple(
            {str(key): str(value) for key, value in item.items()}
            for item in data[:limit]
            if isinstance(item, dict)
        )

    async def metric_names(self, start: datetime, end: datetime) -> tuple[str, ...]:
        data = await self._get(
            "/api/v1/label/__name__/values",
            {"start": start.timestamp(), "end": end.timestamp()},
        )
        if not isinstance(data, list):
            raise MonitoringReadError("THANOS_RESPONSE_INVALID")
        return tuple(str(item) for item in data if isinstance(item, str))

    async def metric_metadata(self, metric: str) -> dict[str, str] | None:
        data = await self._get("/api/v1/metadata", {"metric": metric, "limit": 1})
        if not isinstance(data, dict):
            raise MonitoringReadError("THANOS_RESPONSE_INVALID")
        entries = data.get(metric)
        if not isinstance(entries, list) or not entries or not isinstance(entries[0], dict):
            return None
        return {str(key): str(value) for key, value in entries[0].items()}

    async def metric_label_names(
        self, metric: str, start: datetime, end: datetime
    ) -> tuple[str, ...]:
        data = await self._get(
            "/api/v1/labels",
            {"match[]": [metric], "start": start.timestamp(), "end": end.timestamp()},
        )
        if not isinstance(data, list):
            raise MonitoringReadError("THANOS_RESPONSE_INVALID")
        return tuple(str(item) for item in data if isinstance(item, str))

    async def metric_label_values(
        self, metric: str, label: str, start: datetime, end: datetime
    ) -> tuple[str, ...]:
        data = await self._get(
            f"/api/v1/label/{quote(label, safe='')}/values",
            {"match[]": [metric], "start": start.timestamp(), "end": end.timestamp()},
        )
        if not isinstance(data, list):
            raise MonitoringReadError("THANOS_RESPONSE_INVALID")
        return tuple(str(item) for item in data if isinstance(item, str))

    async def alert_rules(self, alertname: str) -> tuple[dict[str, Any], ...]:
        data = await self._get(
            "/api/v1/rules",
            {"type": "alert", "rule_name[]": [alertname], "exclude_alerts": "true"},
        )
        if not isinstance(data, dict):
            raise MonitoringReadError("THANOS_RESPONSE_INVALID")
        rules: list[dict[str, Any]] = []
        for group in data.get("groups") or ():
            if isinstance(group, dict):
                rules.extend(
                    item
                    for item in group.get("rules") or ()
                    if isinstance(item, dict) and item.get("type") == "alerting"
                )
        return tuple(rules)
