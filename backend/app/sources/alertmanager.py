"""Read-only Alertmanager v2 client.

This client only performs GET requests. It must never create or modify
silences, routing, inhibitions, receivers, or notification state.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

ALERTMANAGER_ALERTS_PATH = "/api/v2/alerts"
ALERTMANAGER_ALERTS_PARAMS = {
    "active": "true",
    "silenced": "false",
    "inhibited": "false",
}
MAX_ALERTMANAGER_RESPONSE_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class AlertmanagerEndpointRequest:
    """One immutable Alertmanager endpoint request, including request-local auth."""

    base_url: str
    auth_kind: str = "NONE"
    username: str = ""
    secret: str = field(default="", repr=False)
    timeout_seconds: float = 10.0


@dataclass(frozen=True)
class AlertmanagerFetchResult:
    ok: bool
    status: str
    alerts: tuple[dict[str, Any], ...] = ()
    duration_ms: int = 0
    safe_error_code: str | None = None


class AlertmanagerEndpointClient:
    """Endpoint-scoped read-only Alertmanager v2 client."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.transport = transport

    async def fetch_alerts(
        self, endpoint: AlertmanagerEndpointRequest
    ) -> AlertmanagerFetchResult:
        """Fetch active alerts from exactly one endpoint using a bounded GET."""

        started = time.monotonic()
        headers = {"Accept": "application/json"}
        auth: httpx.BasicAuth | None = None
        if endpoint.auth_kind == "BEARER":
            headers["Authorization"] = f"Bearer {endpoint.secret}"
        elif endpoint.auth_kind == "BASIC":
            auth = httpx.BasicAuth(endpoint.username, endpoint.secret)
        try:
            async with httpx.AsyncClient(
                timeout=endpoint.timeout_seconds,
                follow_redirects=False,
                auth=auth,
                transport=self.transport,
            ) as client:
                async with client.stream(
                    "GET",
                    f"{endpoint.base_url.rstrip('/')}{ALERTMANAGER_ALERTS_PATH}",
                    headers=headers,
                    params=ALERTMANAGER_ALERTS_PARAMS,
                ) as response:
                    if response.status_code >= 500:
                        return _failed_result(
                            started, "HTTP", "ENDPOINT_HTTP_5XX"
                        )
                    if response.status_code >= 300:
                        return _failed_result(
                            started, "HTTP", "ENDPOINT_HTTP_4XX"
                        )
                    length = response.headers.get("content-length")
                    if (
                        length is not None
                        and int(length) > MAX_ALERTMANAGER_RESPONSE_BYTES
                    ):
                        return _failed_result(
                            started,
                            "PARSE",
                            "ENDPOINT_RESPONSE_TOO_LARGE",
                        )
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > MAX_ALERTMANAGER_RESPONSE_BYTES:
                            return _failed_result(
                                started,
                                "PARSE",
                                "ENDPOINT_RESPONSE_TOO_LARGE",
                            )
            payload = json.loads(content)
            if not isinstance(payload, list):
                return _failed_result(started, "PARSE", "ENDPOINT_PARSE")
            alerts = tuple(item for item in payload if isinstance(item, dict))
            return AlertmanagerFetchResult(
                ok=True,
                status="SUCCESS",
                alerts=alerts,
                duration_ms=_duration_ms(started),
            )
        except httpx.TimeoutException:
            return _failed_result(started, "TIMEOUT", "ENDPOINT_TIMEOUT")
        except httpx.HTTPError:
            return _failed_result(started, "NETWORK", "ENDPOINT_NETWORK")
        except (TypeError, ValueError, json.JSONDecodeError):
            return _failed_result(started, "PARSE", "ENDPOINT_PARSE")


def _duration_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def _failed_result(
    started: float, status: str, safe_error_code: str
) -> AlertmanagerFetchResult:
    return AlertmanagerFetchResult(
        ok=False,
        status=status,
        duration_ms=_duration_ms(started),
        safe_error_code=safe_error_code,
    )
