"""In-memory runtime state shared across the app (poll health, etc.)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class PollStatus:
    last_poll_at: Optional[datetime] = None
    last_poll_ok: Optional[bool] = None
    last_poll_error: Optional[str] = None
    source_id: Optional[str] = None
    # Exact clusters observed in the latest successful live poll. Known
    # clusters and their last-seen times are derived from retained Alert rows.
    watchdog_current_clusters: set[str] = field(default_factory=set)


poll_status = PollStatus()


@dataclass
class BackfillStatus:
    last_backfill_at: Optional[datetime] = None
    last_backfill_ok: Optional[bool] = None
    last_backfill_error: Optional[str] = None
    skipped: bool = False
    effective_hours: int = 0
    truncated_reason: Optional[str] = None
    alerts_reconstructed: int = 0


backfill_status = BackfillStatus()


@dataclass
class NotificationStatus:
    """Process-local worker liveness; durable queue facts stay in SQLite."""

    last_run_at: Optional[datetime] = None
    last_error_code: Optional[str] = None


notification_status = NotificationStatus()
