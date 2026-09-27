"""Single server-side management-health snapshot."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter

from app.api.v1.schemas import (
    ErrorEnvelope,
    PlatformHealthCheckResponse,
    PlatformHealthResponse,
    PlatformJobRuntimeResponse,
    PlatformNotificationHealthResponse,
    PlatformReadinessResponse,
    PlatformSourcePollResponse,
    PlatformWatchdogClusterResponse,
    PlatformWatchdogCountsResponse,
    PlatformWatchdogResponse,
    PlatformWatchdogSourceClusterResponse,
    PlatformWatchdogSourceResponse,
)
from app.application.platform_health import PlatformHealthReader, WatchdogCounts
from app.platform.utc import to_utc_iso


UTC = timezone.utc


def _time(value: datetime | None) -> str | None:
    return None if value is None else to_utc_iso(value)


def _counts(value: WatchdogCounts) -> PlatformWatchdogCountsResponse:
    return PlatformWatchdogCountsResponse(
        total=value.total,
        healthy=value.healthy,
        missing=value.missing,
        unknown=value.unknown,
    )


def create_platform_health_router(
    *,
    reader: PlatformHealthReader,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    router = APIRouter(prefix="/api/v1")
    errors: dict[int | str, dict[str, Any]] = {
        500: {"model": ErrorEnvelope},
    }

    @router.get(
        "/platform-health",
        response_model=PlatformHealthResponse,
        responses=errors,
    )
    async def platform_health() -> PlatformHealthResponse:
        value = await reader.snapshot(now=now())
        return PlatformHealthResponse(
            status=value.status,
            source_mode=value.source_mode,
            configuration_status=value.configuration_status,
            configuration_errors=list(value.configuration_errors),
            last_poll_at=_time(value.last_poll_at),
            last_poll_ok=value.last_poll_ok,
            last_poll_error=value.last_poll_error,
            incident_count=value.incident_count,
            last_backfill_at=_time(value.last_backfill_at),
            last_backfill_ok=value.last_backfill_ok,
            last_backfill_error=value.last_backfill_error,
            backfill_skipped=value.backfill_skipped,
            backfill_effective_hours=value.backfill_effective_hours,
            backfill_truncated_reason=value.backfill_truncated_reason,
            alerts_reconstructed=value.alerts_reconstructed,
            sources=[
                PlatformSourcePollResponse(
                    source_id=item.source_id,
                    source_name=item.source_name,
                    lifecycle_state=item.lifecycle_state,
                    health=item.health,
                    endpoint_total=item.endpoint_total,
                    endpoint_succeeded=item.endpoint_succeeded,
                    endpoint_failed=item.endpoint_failed,
                    last_poll_at=_time(item.last_poll_at),
                    last_complete_success_at=_time(item.last_complete_success_at),
                    last_any_success_at=_time(item.last_any_success_at),
                    safe_error_codes=list(item.safe_error_codes),
                )
                for item in value.sources
            ],
            watchdog=PlatformWatchdogResponse(
                overall_status=value.watchdog.overall_status,
                summary=_counts(value.watchdog.summary),
                clusters=[
                    PlatformWatchdogClusterResponse(
                        cluster=item.cluster,
                        status=item.status,
                        last_seen_at=_time(item.last_seen_at),
                        freshness_seconds=item.freshness_seconds,
                    )
                    for item in value.watchdog.clusters
                ],
                monitored_source_count=value.watchdog.monitored_source_count,
                unmonitored_source_count=value.watchdog.unmonitored_source_count,
                sources=[
                    PlatformWatchdogSourceResponse(
                        source_id=source.source_id,
                        source_name=source.source_name,
                        source_health=source.source_health,
                        monitor_state=source.monitor_state,
                        summary=_counts(source.summary),
                        clusters=[
                            PlatformWatchdogSourceClusterResponse(
                                id=item.id,
                                source_id=item.source_id,
                                identity_value=item.identity_value,
                                cluster=item.cluster,
                                inventory_state=item.inventory_state,
                                health_state=item.health_state,
                                status=item.status,
                                first_discovered_at=_time(item.first_discovered_at),
                                monitoring_started_at=_time(
                                    item.monitoring_started_at
                                ),
                                last_seen_at=_time(item.last_seen_at),
                                ignored_at=_time(item.ignored_at),
                                ignored_reason=item.ignored_reason,
                                still_emitting=item.still_emitting,
                                freshness_seconds=item.freshness_seconds,
                                version=item.version,
                            )
                            for item in source.clusters
                        ],
                    )
                    for source in value.watchdog.sources
                ],
            ),
            notifications=PlatformNotificationHealthResponse(
                status=value.notifications.status,
                worker_last_run_at=_time(value.notifications.worker_last_run_at),
                pending=value.notifications.pending,
                retrying=value.notifications.retrying,
                permanent_failed_24h=value.notifications.permanent_failed_24h,
                oldest_pending_seconds=value.notifications.oldest_pending_seconds,
                active_channels=value.notifications.active_channels,
                active_policies=value.notifications.active_policies,
                last_error_code=value.notifications.last_error_code,
            ),
            readiness=PlatformReadinessResponse(
                status=value.readiness.status,
                checked_at=to_utc_iso(value.readiness.checked_at),
                checks=[
                    PlatformHealthCheckResponse(
                        name=item.name,
                        status=item.status,
                        code=item.code,
                    )
                    for item in value.readiness.checks
                ],
            ),
            jobs=PlatformJobRuntimeResponse(
                scheduler_running=value.jobs.scheduler_running,
                scheduler_heartbeat_at=_time(value.jobs.scheduler_heartbeat_at),
                runner_running=value.jobs.runner_running,
                runner_heartbeat_at=_time(value.jobs.runner_heartbeat_at),
                pending=value.jobs.pending,
                running=value.jobs.running,
                expired_leases=value.jobs.expired_leases,
                oldest_pending_seconds=value.jobs.oldest_pending_seconds,
            ),
        )

    return router
