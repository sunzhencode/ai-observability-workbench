"""Request correlation, bounded HTTP metrics and structured access logs."""

from __future__ import annotations

import json
import logging
import re
from contextvars import ContextVar, Token
from time import perf_counter
from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from app.platform.metrics import MetricRegistry

REQUEST_ID_HEADER = "X-Request-ID"
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_request_id: ContextVar[str | None] = ContextVar("incident_operations_request_id", default=None)
logger = logging.getLogger("incident_operations.platform.http")


def current_request_id() -> str:
    return _request_id.get() or "unavailable"


def _choose_request_id(value: str | None) -> str:
    if value and REQUEST_ID_PATTERN.fullmatch(value):
        return value
    return uuid4().hex


class RequestContextMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, *, metrics: MetricRegistry) -> None:
        super().__init__(app)
        self._metrics = metrics

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        request_id = _choose_request_id(request.headers.get(REQUEST_ID_HEADER))
        request.state.request_id = request_id
        token: Token[str | None] = _request_id.set(request_id)
        started = perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers[REQUEST_ID_HEADER] = request_id
            return response
        finally:
            duration = perf_counter() - started
            route = request.scope.get("route")
            route_path = getattr(route, "path", None) or "<unmatched>"
            self._metrics.observe_http(
                method=request.method,
                route=route_path,
                status_code=status_code,
                duration_seconds=duration,
            )
            logger.info(
                json.dumps(
                    {
                        "event": "http_request_completed",
                        "request_id": request_id,
                        "method": request.method,
                        "route": route_path,
                        "status_code": status_code,
                        "duration_ms": round(duration * 1000, 3),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            _request_id.reset(token)
