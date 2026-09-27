"""One thin, bounded GET adapter for Alertmanager alerts."""

from __future__ import annotations

from collections.abc import Mapping
import json
import time
from typing import Any, cast

import httpx

from app.domains.sources.models import EndpointObservation, EndpointSnapshot, RawAlert

MAX_RESPONSE_BYTES = 5 * 1024 * 1024


class BoundedAlertmanagerReader:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def fetch(self, endpoint: EndpointSnapshot) -> EndpointObservation:
        started = time.perf_counter()
        headers: dict[str, str] = {"Accept": "application/json"}
        auth: httpx.BasicAuth | None = None
        if endpoint.auth_kind == "BEARER" and endpoint.secret:
            headers["Authorization"] = f"Bearer {endpoint.secret}"
        elif endpoint.auth_kind == "BASIC":
            auth = httpx.BasicAuth(endpoint.username, endpoint.secret)
        elif endpoint.auth_kind != "NONE":
            return EndpointObservation(
                endpoint,
                "CONFIGURATION",
                (),
                0,
                safe_error_code="ENDPOINT_AUTH_UNSUPPORTED",
            )
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                follow_redirects=False,
                timeout=endpoint.timeout_seconds,
            ) as client:
                async with client.stream(
                    "GET",
                    f"{endpoint.canonical_url.rstrip('/')}/api/v2/alerts",
                    headers=headers,
                    auth=auth,
                ) as response:
                    response.raise_for_status()
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_RESPONSE_BYTES:
                            raise ValueError("response too large")
                        chunks.append(chunk)
            raw: Any = json.loads(b"".join(chunks))
            if not isinstance(raw, list):
                raise ValueError("response is not an alert list")
            alerts: list[RawAlert] = []
            for item in raw:
                if not isinstance(item, Mapping):
                    raise ValueError("response contains a non-object alert")
                alerts.append(cast(RawAlert, item))
            return EndpointObservation(
                endpoint=endpoint,
                status="SUCCESS",
                alerts=tuple(alerts),
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        except httpx.TimeoutException:
            code = "ENDPOINT_TIMEOUT"
            status = "TIMEOUT"
        except httpx.HTTPStatusError:
            code = "ENDPOINT_HTTP_STATUS"
            status = "HTTP"
        except (httpx.HTTPError, ValueError, json.JSONDecodeError):
            code = "ENDPOINT_RESPONSE_INVALID"
            status = "NETWORK"
        return EndpointObservation(
            endpoint=endpoint,
            status=status,
            alerts=(),
            duration_ms=int((time.perf_counter() - started) * 1000),
            safe_error_code=code,
        )
