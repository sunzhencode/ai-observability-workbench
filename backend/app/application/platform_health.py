"""One bounded management-health snapshot for the operator interface."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol


SourceHealthState = Literal[
    "HEALTHY", "DEGRADED", "UNAVAILABLE", "DISABLED", "ARCHIVED"
]
WatchdogState = Literal["healthy", "missing", "unknown"]
WatchdogMonitorState = Literal[
    "ENABLED", "DISABLED", "SOURCE_DISABLED", "SOURCE_ARCHIVED"
]
WatchdogOverallState = Literal["healthy", "missing", "unknown", "no_data"]
NotificationHealthState = Literal["disabled", "healthy", "degraded"]
ReadinessState = Literal["ready", "not_ready"]


@dataclass(frozen=True, slots=True)
class HealthCheck:
    name: str
    status: ReadinessState
    code: str


@dataclass(frozen=True, slots=True)
class ReadinessSummary:
    status: ReadinessState
    checked_at: datetime
    checks: tuple[HealthCheck, ...]


@dataclass(frozen=True, slots=True)
class JobRuntimeSummary:
    scheduler_running: bool
    scheduler_heartbeat_at: datetime | None
    runner_running: bool
    runner_heartbeat_at: datetime | None
    pending: int
    running: int
    expired_leases: int
    oldest_pending_seconds: int | None


@dataclass(frozen=True, slots=True)
class SourcePollSummary:
    source_id: str
    source_name: str
    lifecycle_state: str
    health: SourceHealthState
    endpoint_total: int
    endpoint_succeeded: int
    endpoint_failed: int
    last_poll_at: datetime | None
    last_complete_success_at: datetime | None
    last_any_success_at: datetime | None
    safe_error_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WatchdogCounts:
    total: int
    healthy: int
    missing: int
    unknown: int


@dataclass(frozen=True, slots=True)
class WatchdogClusterSummary:
    id: int | None
    source_id: str
    identity_value: str
    cluster: str
    inventory_state: str | None
    health_state: str | None
    status: WatchdogState
    first_discovered_at: datetime | None
    monitoring_started_at: datetime | None
    last_seen_at: datetime | None
    ignored_at: datetime | None
    ignored_reason: str | None
    still_emitting: bool
    freshness_seconds: int | None
    version: int | None


@dataclass(frozen=True, slots=True)
class WatchdogSourceSummary:
    source_id: str
    source_name: str
    source_health: SourceHealthState
    monitor_state: WatchdogMonitorState
    summary: WatchdogCounts
    clusters: tuple[WatchdogClusterSummary, ...]


@dataclass(frozen=True, slots=True)
class WatchdogHealthSummary:
    overall_status: WatchdogOverallState
    summary: WatchdogCounts
    clusters: tuple[WatchdogClusterSummary, ...]
    monitored_source_count: int
    unmonitored_source_count: int
    sources: tuple[WatchdogSourceSummary, ...]


@dataclass(frozen=True, slots=True)
class NotificationHealthSummary:
    status: NotificationHealthState
    worker_last_run_at: datetime | None
    pending: int
    retrying: int
    permanent_failed_24h: int
    oldest_pending_seconds: int | None
    active_channels: int
    active_policies: int
    last_error_code: str | None


@dataclass(frozen=True, slots=True)
class PlatformHealthSnapshot:
    status: str
    source_mode: Literal["REGISTRY", "UNCONFIGURED"]
    configuration_status: Literal["configured", "unconfigured", "degraded"]
    configuration_errors: tuple[str, ...]
    last_poll_at: datetime | None
    last_poll_ok: bool | None
    last_poll_error: str | None
    incident_count: int
    last_backfill_at: datetime | None
    last_backfill_ok: bool | None
    last_backfill_error: str | None
    backfill_skipped: bool
    backfill_effective_hours: int
    backfill_truncated_reason: str | None
    alerts_reconstructed: int
    sources: tuple[SourcePollSummary, ...]
    watchdog: WatchdogHealthSummary
    notifications: NotificationHealthSummary
    readiness: ReadinessSummary
    jobs: JobRuntimeSummary


class PlatformHealthReader(Protocol):
    async def snapshot(self, *, now: datetime) -> PlatformHealthSnapshot: ...
