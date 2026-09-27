"""Composition data for one Incident Operations application instance."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from fastapi import APIRouter

from app.platform.health import ReadinessProbe
from app.platform.metrics import MetricRegistry
from app.platform.runtime import PlatformRuntimeState

LifecycleHook = Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class PlatformWiring:
    runtime: PlatformRuntimeState = field(default_factory=PlatformRuntimeState)
    metrics: MetricRegistry = field(default_factory=MetricRegistry)
    readiness_probes: tuple[ReadinessProbe, ...] = ()
    routers: tuple[APIRouter, ...] = ()
    startup_hooks: tuple[LifecycleHook, ...] = ()
    shutdown_hooks: tuple[LifecycleHook, ...] = ()


def default_platform_wiring(
    *,
    runtime: PlatformRuntimeState | None = None,
    metrics: MetricRegistry | None = None,
    readiness_probes: tuple[ReadinessProbe, ...] = (),
    routers: tuple[APIRouter, ...] = (),
    startup_hooks: tuple[LifecycleHook, ...] = (),
    shutdown_hooks: tuple[LifecycleHook, ...] = (),
) -> PlatformWiring:
    return PlatformWiring(
        runtime=runtime or PlatformRuntimeState(),
        metrics=metrics or MetricRegistry(),
        readiness_probes=readiness_probes,
        routers=routers,
        startup_hooks=startup_hooks,
        shutdown_hooks=shutdown_hooks,
    )
