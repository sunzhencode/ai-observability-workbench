"""Exactly two bounded read-only Grafana methods for Incident Operations."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

MAX_GRAFANA_RESPONSE_BYTES = 8 * 1024 * 1024
_UID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class GrafanaReadError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class BoundedGrafanaReader:
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

    async def search_dashboards(
        self, query: str, *, limit: int = 50
    ) -> tuple[dict[str, Any], ...]:
        payload = await self._get(
            "/api/search",
            {"type": "dash-db", "query": query, "limit": max(1, min(limit, 500))},
        )
        if not isinstance(payload, list):
            raise GrafanaReadError("GRAFANA_RESPONSE_INVALID")
        return tuple(item for item in payload if isinstance(item, dict))

    async def get_dashboard(self, uid: str) -> dict[str, Any]:
        if not _UID.fullmatch(uid):
            raise GrafanaReadError("GRAFANA_UID_INVALID")
        payload = await self._get(f"/api/dashboards/uid/{uid}", {})
        dashboard = payload.get("dashboard") if isinstance(payload, dict) else None
        if not isinstance(dashboard, dict):
            raise GrafanaReadError("GRAFANA_RESPONSE_INVALID")
        return dashboard

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
                        raise GrafanaReadError("GRAFANA_REDIRECT_REJECTED")
                    if response.status_code in {401, 403}:
                        raise GrafanaReadError("GRAFANA_AUTH_FAILED")
                    if response.status_code == 404:
                        raise GrafanaReadError("GRAFANA_NOT_FOUND")
                    if response.status_code >= 400:
                        raise GrafanaReadError(f"GRAFANA_HTTP_{response.status_code}")
                    length = response.headers.get("content-length")
                    if length is not None and int(length) > MAX_GRAFANA_RESPONSE_BYTES:
                        raise GrafanaReadError("GRAFANA_RESPONSE_TOO_LARGE")
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > MAX_GRAFANA_RESPONSE_BYTES:
                            raise GrafanaReadError("GRAFANA_RESPONSE_TOO_LARGE")
        except httpx.TimeoutException:
            raise GrafanaReadError("GRAFANA_TIMEOUT") from None
        except httpx.HTTPError:
            raise GrafanaReadError("GRAFANA_UNREACHABLE") from None
        try:
            return json.loads(content)
        except ValueError:
            raise GrafanaReadError("GRAFANA_RESPONSE_INVALID") from None
