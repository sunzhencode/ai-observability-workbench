"""Typed liveness/readiness probes for the Incident Operations platform shell."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import re
from typing import Literal

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.platform.metrics import MetricRegistry
from app.platform.runtime import PlatformRuntimeState
from app.platform.utc import to_utc_iso, utc_now

ReadinessStatus = Literal["ready", "not_ready"]
ReadinessCheck = Callable[[], Awaitable["ReadinessProbeResult"]]
PROBE_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
SAFE_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{1,95}$")


@dataclass(frozen=True, slots=True)
class ReadinessProbeResult:
    status: ReadinessStatus = "ready"
    code: str = "OK"

    def __post_init__(self) -> None:
        if not SAFE_CODE_PATTERN.fullmatch(self.code):
            raise ValueError("readiness result code must be a stable safe code")


@dataclass(frozen=True, slots=True)
class ReadinessProbe:
    name: str
    check: ReadinessCheck
    failure_code: str

    def __post_init__(self) -> None:
        if not PROBE_NAME_PATTERN.fullmatch(self.name):
            raise ValueError("readiness probe name must be a stable lower-case key")
        if not SAFE_CODE_PATTERN.fullmatch(self.failure_code):
            raise ValueError("readiness failure code must be a stable safe code")


async def evaluate_readiness(
    *,
    runtime: PlatformRuntimeState,
    probes: tuple[ReadinessProbe, ...],
) -> tuple[bool, list[dict[str, str]]]:
    checks: list[dict[str, str]] = []
    if not runtime.accepting_requests:
        checks.append(
            {"name": "lifespan", "status": "not_ready", "code": "APP_NOT_STARTED"}
        )
    for probe in probes:
        try:
            result = await probe.check()
        except Exception:  # noqa: BLE001 - raw dependency errors never leave health
            result = ReadinessProbeResult(
                status="not_ready", code=probe.failure_code
            )
        checks.append(
            {"name": probe.name, "status": result.status, "code": result.code}
        )
    ready = all(item["status"] == "ready" for item in checks)
    return ready, checks


def create_health_router(
    *,
    runtime: PlatformRuntimeState,
    probes: tuple[ReadinessProbe, ...],
    metrics: MetricRegistry,
) -> APIRouter:
    router = APIRouter(include_in_schema=False)

    @router.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "alive", "checked_at": to_utc_iso(utc_now())}

    @router.get("/health/ready")
    async def ready() -> JSONResponse:
        is_ready, checks = await evaluate_readiness(
            runtime=runtime, probes=probes
        )
        metrics.set_ready(is_ready)
        return JSONResponse(
            status_code=200 if is_ready else 503,
            content={
                "status": "ready" if is_ready else "not_ready",
                "checked_at": to_utc_iso(utc_now()),
                "checks": checks,
            },
        )

    return router
