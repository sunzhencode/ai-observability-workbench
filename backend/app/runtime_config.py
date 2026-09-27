"""Immutable runtime configuration snapshots.

Business jobs capture one :class:`ActiveRuntimeConfig` at their boundary and
pass it through the whole call graph.  Since F22 the values in it are product
decisions rather than settings: they come from the constants below, not from
``.env`` -- that file only says where the database is and which ports to bind.
Addresses and credentials were never here; they live in the registry (ADR 0004).
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from typing import Protocol

# Bounds that the product decides, not the operator. The poll scheduler tick is
# owned by main.py; every source's own interval lives in its registry row.
DEFAULT_RESOLUTION_GRACE_SECONDS = 300
BACKFILL_HOURS = 24
BACKFILL_MAX_HOURS = 168
BACKFILL_STEP_SECONDS = 60
RETENTION_DAYS = 30
# Occurrence history is kept far longer than the runtime data, and deliberately
# not by the same number (F26 / CAP-09.6a). The 30 days above deletes *terminal
# facts* -- a resolved Alert nobody will look at again -- while a history record's
# value grows with age. 365 is the shortest window covering a full seasonal cycle
# (quarter-end, year-end batch jobs), which is exactly when someone asks "did this
# happen last time too?". One record is a dozen scalars, so a year of them costs
# local SQLite nothing.
OCCURRENCE_HISTORY_RETENTION_DAYS = 365

#: Outbound model calls (F27). A third knob rather than a reuse of either number
#: above, for the same reason those two are separate: this is a **spending**
#: record. Thirty days would delete the answer to "what did last quarter cost",
#: and a year of it is more than a local file should carry for an audit nobody
#: is reading day to day.
MODEL_CALL_RETENTION_DAYS = 90
# Legacy compatibility column only; no domain, API, grouping or UI reads it.
LEGACY_ENVIRONMENT = "prod"


@dataclass(frozen=True)
class ActiveRuntimeConfig:
    # Connections live in the registry, not here. Every job still captures one
    # immutable snapshot of the *runtime* values below; the addresses and
    # credentials it needs come from the source's own configuration.
    # See docs/adr/0004-registry-only-configuration.md.
    environment: str
    resolution_grace_seconds: int
    backfill_hours: int
    backfill_max_hours: int
    backfill_step_seconds: int
    retention_days: int
    occurrence_history_retention_days: int = OCCURRENCE_HISTORY_RETENTION_DAYS
    model_call_retention_days: int = MODEL_CALL_RETENTION_DAYS
    configuration_errors: tuple[str, ...] = ()


class RuntimeConfigSource(Protocol):
    def snapshot(self) -> ActiveRuntimeConfig: ...


class DefaultRuntimeConfig:
    """The product's own runtime values, identical on every install."""

    def snapshot(self) -> ActiveRuntimeConfig:
        return ActiveRuntimeConfig(
            environment=LEGACY_ENVIRONMENT,
            resolution_grace_seconds=DEFAULT_RESOLUTION_GRACE_SECONDS,
            backfill_hours=BACKFILL_HOURS,
            backfill_max_hours=BACKFILL_MAX_HOURS,
            backfill_step_seconds=BACKFILL_STEP_SECONDS,
            retention_days=RETENTION_DAYS,
            occurrence_history_retention_days=OCCURRENCE_HISTORY_RETENTION_DAYS,
            model_call_retention_days=MODEL_CALL_RETENTION_DAYS,
        )


class RuntimeConfigProvider:
    """Thread-safe source switch; each returned snapshot remains immutable."""

    def __init__(self, source: RuntimeConfigSource | None = None) -> None:
        self._lock = RLock()
        self._source = source or DefaultRuntimeConfig()

    def snapshot(self) -> ActiveRuntimeConfig:
        with self._lock:
            source = self._source
        return source.snapshot()

    def replace_source(self, source: RuntimeConfigSource) -> None:
        with self._lock:
            self._source = source


runtime_config_provider = RuntimeConfigProvider()
