"""The read-only Thanos client must stay bounded and must not leak the address."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.sources.thanos import (
    ALERTS_QUERY,
    ALERTS_SERIES_MATCHER,
    MAX_THANOS_RESPONSE_BYTES,
    ThanosClient,
    safe_error_code,
)

ADDRESS = "https://thanos.internal.example:10902"
WINDOW_END = datetime(2026, 7, 27, tzinfo=timezone.utc)
WINDOW_START = WINDOW_END - timedelta(hours=24)


def _client(handler) -> ThanosClient:
    return ThanosClient(
        base_url=ADDRESS,
        token="history-token",
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.asyncio
async def test_safe_error_code_never_carries_the_address_or_query() -> None:
    """`str(exc)` on an httpx error embeds the full URL; the API must not see it."""
    client = _client(lambda request: httpx.Response(500, json={"status": "error"}))
    with pytest.raises(Exception) as caught:
        await client.series_alerts(start=WINDOW_START, end=WINDOW_END, limit=500)

    raw = str(caught.value)
    assert "thanos.internal.example" in raw  # the leak this guards against
    code = safe_error_code(caught.value)
    assert code == "HTTP_500"
    assert "thanos" not in code
    assert "10902" not in code
    assert "ALERTS" not in code


@pytest.mark.asyncio
@pytest.mark.parametrize("exc, expected", [
    (httpx.ConnectTimeout("slow"), "TIMEOUT"),
    (httpx.ConnectError("refused"), "NETWORK"),
    (ValueError("bad shape"), "INVALID_RESPONSE"),
    (RuntimeError("something else"), "UNAVAILABLE"),
])
async def test_safe_error_code_classifies_without_detail(exc, expected) -> None:
    assert safe_error_code(exc) == expected


@pytest.mark.asyncio
async def test_query_range_refuses_an_oversized_matrix() -> None:
    """A 24h matrix is unbounded in series count, so the read needs its own cap."""
    oversized = (
        b'{"status":"success","data":{"result":['
        + b'{"metric":{},"values":[]},' * 700_000
        + b"]}}"
    )
    assert len(oversized) > MAX_THANOS_RESPONSE_BYTES
    client = _client(lambda request: httpx.Response(200, content=oversized))

    with pytest.raises(Exception) as caught:
        await client.query_alerts(
            start=WINDOW_START, end=WINDOW_END, step_seconds=60
        )
    assert safe_error_code(caught.value) == "RESPONSE_TOO_LARGE"


@pytest.mark.asyncio
async def test_declared_content_length_is_refused_before_reading() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b'{"status":"success"}',
            headers={"content-length": str(MAX_THANOS_RESPONSE_BYTES + 1)},
        )

    client = _client(handler)
    with pytest.raises(Exception) as caught:
        await client.series_alerts(start=WINDOW_START, end=WINDOW_END, limit=10)
    assert safe_error_code(caught.value) == "RESPONSE_TOO_LARGE"


@pytest.mark.asyncio
async def test_selectors_are_constant_and_reads_stay_get_only() -> None:
    """No caller-supplied PromQL: the selectors are fixed strings."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json={"status": "success", "data": {"result": []}}
        )

    client = _client(handler)
    await client.query_alerts(start=WINDOW_START, end=WINDOW_END, step_seconds=60)
    assert seen[-1].method == "GET"
    assert seen[-1].url.params["query"] == ALERTS_QUERY

    def series_handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"status": "success", "data": []})

    await _client(series_handler).series_alerts(
        start=WINDOW_START, end=WINDOW_END, limit=10
    )
    assert seen[-1].method == "GET"
    assert seen[-1].url.params["match[]"] == ALERTS_SERIES_MATCHER
