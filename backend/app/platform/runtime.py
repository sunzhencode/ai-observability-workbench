"""In-process lifecycle state for liveness and readiness."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.platform.utc import utc_now


@dataclass(slots=True)
class PlatformRuntimeState:
    """Mutable state owned by one application factory instance."""

    started_at: datetime | None = None
    accepting_requests: bool = False
    shutdown_started_at: datetime | None = None

    def mark_started(self) -> None:
        self.started_at = utc_now()
        self.shutdown_started_at = None
        self.accepting_requests = True

    def mark_stopping(self) -> None:
        self.accepting_requests = False
        self.shutdown_started_at = utc_now()
