"""Incident Operations application factory used by the default product entrypoint."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response

from app.bootstrap.lifespan import create_lifespan
from app.bootstrap.wiring import PlatformWiring, default_platform_wiring
from app.platform.errors import (
    SafeApiError,
    safe_api_error_handler,
    safe_unexpected_error_handler,
    safe_validation_error_handler,
)
from app.platform.browser_security import (
    DEFAULT_TRUSTED_HOSTS,
    BrowserRequestGuardMiddleware,
    is_loopback_host,
    new_csrf_token,
)
from app.platform.frontend import mount_operator_interface
from app.platform.health import create_health_router
from app.platform.request_context import RequestContextMiddleware


def require_single_worker(worker_count: int) -> None:
    if type(worker_count) is not int or worker_count != 1:
        raise RuntimeError("Incident Operations requires a single Uvicorn worker (workers=1)")


def create_platform_app(
    *,
    wiring: PlatformWiring | None = None,
    worker_count: int = 1,
    frontend_dist: Path | None = None,
    trusted_hosts: tuple[str, ...] = (),
) -> FastAPI:
    """Build the platform shell without mounting legacy business routers."""
    require_single_worker(worker_count)
    resolved = wiring or default_platform_wiring()
    app = FastAPI(
        title="AI Incident Operations Platform",
        version="0.1.0-a3",
        lifespan=create_lifespan(resolved),
    )
    app.state.platform_wiring = resolved
    csrf_token: str | None = None
    if frontend_dist is not None:
        allowed_hosts = tuple(dict.fromkeys((*DEFAULT_TRUSTED_HOSTS, *trusted_hosts)))
        csrf_token = new_csrf_token()
        app.add_middleware(
            BrowserRequestGuardMiddleware,
            trusted_hosts=allowed_hosts,
            csrf_token=csrf_token,
        )
        remote_hosts = tuple(
            host for host in trusted_hosts if not is_loopback_host(host)
        )
        if remote_hosts:
            logging.getLogger("incident_operations.platform.security").warning(
                "Non-loopback access is enabled: every network-reachable client "
                "has full operator permissions"
            )
    app.add_middleware(RequestContextMiddleware, metrics=resolved.metrics)
    app.add_exception_handler(SafeApiError, safe_api_error_handler)
    app.add_exception_handler(RequestValidationError, safe_validation_error_handler)
    app.add_exception_handler(Exception, safe_unexpected_error_handler)
    app.include_router(
        create_health_router(
            runtime=resolved.runtime,
            probes=resolved.readiness_probes,
            metrics=resolved.metrics,
        )
    )
    for router in resolved.routers:
        app.include_router(router)

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(
            content=resolved.metrics.render(),
            media_type="text/plain; version=0.0.4",
        )

    if frontend_dist is not None:
        assert csrf_token is not None
        mount_operator_interface(app, frontend_dist, csrf_token=csrf_token)

    return app
