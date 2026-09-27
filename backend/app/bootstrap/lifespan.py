"""Ordered startup and shutdown for injected platform capabilities."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from collections.abc import Callable

from fastapi import FastAPI

from app.bootstrap.wiring import PlatformWiring


def create_lifespan(
    wiring: PlatformWiring,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            for hook in wiring.startup_hooks:
                await hook()
            wiring.runtime.mark_started()
            wiring.metrics.set_ready(True)
            yield
        finally:
            wiring.runtime.mark_stopping()
            wiring.metrics.set_ready(False)
            for hook in reversed(wiring.shutdown_hooks):
                await hook()

    return lifespan
