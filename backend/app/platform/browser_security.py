"""Browser cross-site request guards for the single-origin operator UI."""

from __future__ import annotations

import ipaddress
import secrets
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from app.platform.request_context import current_request_id

CSRF_HEADER = "X-CSRF-Token"
DEFAULT_TRUSTED_HOSTS = ("127.0.0.1", "localhost", "::1", "testserver")
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def normalize_host(value: str) -> str:
    candidate = value.strip()
    if candidate.count(":") >= 2 and not candidate.startswith("["):
        try:
            return ipaddress.ip_address(candidate).compressed.lower()
        except ValueError:
            pass
    try:
        host = urlsplit(f"//{candidate}").hostname
    except ValueError as exc:
        raise ValueError("trusted host is invalid") from exc
    if host is None or not host.strip() or "*" in host:
        raise ValueError("trusted host must be explicit")
    return host.lower()


def is_loopback_host(value: str) -> bool:
    host = normalize_host(value)
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def _failure(status_code: int, code: str, message: str) -> JSONResponse:
    request_id = current_request_id()
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "request_id": request_id,
            }
        },
        headers={"X-Request-ID": request_id},
    )


class BrowserRequestGuardMiddleware(BaseHTTPMiddleware):
    """Reject cross-host/origin requests and tokenless browser mutations."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        trusted_hosts: tuple[str, ...],
        csrf_token: str,
    ) -> None:
        super().__init__(app)
        self._trusted_hosts = frozenset(normalize_host(item) for item in trusted_hosts)
        if not self._trusted_hosts:
            raise ValueError("at least one trusted host is required")
        if len(csrf_token) < 32:
            raise ValueError("CSRF token is too short")
        self._csrf_token = csrf_token

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        host_header = request.headers.get("host", "")
        try:
            host = normalize_host(host_header)
        except ValueError:
            return _failure(400, "HOST_INVALID", "请求 Host 无法识别")
        if host not in self._trusted_hosts:
            return _failure(400, "HOST_NOT_ALLOWED", "请求 Host 不在本机允许范围")

        origin = request.headers.get("origin")
        if origin is not None:
            try:
                parsed = urlsplit(origin)
            except ValueError:
                parsed = None
            if (
                parsed is None
                or parsed.scheme not in {"http", "https"}
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
                or parsed.netloc.lower() != host_header.lower()
            ):
                return _failure(403, "ORIGIN_NOT_ALLOWED", "浏览器请求不是同源请求")

        if request.method in _MUTATING_METHODS and request.url.path.startswith(
            "/api/v1/"
        ):
            supplied = request.headers.get(CSRF_HEADER, "")
            if not supplied or not secrets.compare_digest(supplied, self._csrf_token):
                return _failure(
                    403,
                    "CSRF_TOKEN_INVALID",
                    "写操作缺少当前页面的 CSRF 令牌，请刷新后重试",
                )
        return await call_next(request)
