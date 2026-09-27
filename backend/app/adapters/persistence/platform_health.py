"""Bounded SQL projection for the operator management-health page."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from typing import cast

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session

from app.adapters.persistence.jobs import JobRecord
from app.adapters.persistence.notifications import (
    NotificationChannelRecord,
    NotificationAttemptRecord,
    NotificationDeliveryRecord,
    NotificationPolicyRevisionRecord,
)
from app.adapters.persistence.observability import (
    MetricBackfillRunRecord,
    MonitoringConnectionRecord,
)
from app.adapters.persistence.sources import (
    IncidentRecord,
    PollRunRecord,
    SourceRecord,
    WatchdogClusterRecord,
)
from app.application.jobs import EnqueueOnlySchedulerState, JobRunnerState
from app.application.platform_health import (
    HealthCheck,
    JobRuntimeSummary,
    NotificationHealthSummary,
    NotificationHealthState,
    PlatformHealthSnapshot,
    ReadinessSummary,
    ReadinessState,
    SourceHealthState,
    SourcePollSummary,
    WatchdogClusterSummary,
    WatchdogCounts,
    WatchdogHealthSummary,
    WatchdogMonitorState,
    WatchdogOverallState,
    WatchdogState,
    WatchdogSourceSummary,
)
from app.platform.persistence.codecs import aware_utc as _aware, stored_utc as _stored
from app.domains.notifications.models import DeliveryState
from app.domains.operations.jobs import JobState
from app.domains.sources.models import PollCompleteness, SourceState
from app.platform.health import ReadinessProbe, evaluate_readiness
from app.platform.persistence.database import SessionFactory
from app.platform.runtime import PlatformRuntimeState


UTC = timezone.utc
NOTIFICATION_WORKER_STALE_SECONDS = 60
NOTIFICATION_BACKLOG_DEGRADED_SECONDS = 300
NOTIFICATION_BACKLOG_DEGRADED_COUNT = 100


def _seconds_since(now: datetime, value: datetime | None) -> int | None:
    aware = _aware(value)
    if aware is None:
        return None
    return max(0, int((now.astimezone(UTC) - aware).total_seconds()))


def _source_health(source: SourceRecord, latest: PollRunRecord | None) -> SourceHealthState:
    if source.state == SourceState.ARCHIVED.value:
        return "ARCHIVED"
    if source.state != SourceState.ENABLED.value:
        return "DISABLED"
    if latest is None or latest.completeness == PollCompleteness.FAILED.value:
        return "UNAVAILABLE"
    if latest.completeness == PollCompleteness.PARTIAL.value:
        return "DEGRADED"
    return "HEALTHY"


class SqlAlchemyPlatformHealthReader:
    def __init__(
        self,
        sessions: SessionFactory,
        *,
        runtime: PlatformRuntimeState,
        probes: tuple[ReadinessProbe, ...],
        scheduler: EnqueueOnlySchedulerState,
        runner: JobRunnerState,
    ) -> None:
        self._sessions = sessions
        self._runtime = runtime
        self._probes = probes
        self._scheduler = scheduler
        self._runner = runner

    @staticmethod
    def _latest_poll(
        session: Session, source_id: str, *conditions: ColumnElement[bool]
    ) -> PollRunRecord | None:
        statement = select(PollRunRecord).where(PollRunRecord.source_id == source_id)
        for condition in conditions:
            statement = statement.where(condition)
        return session.scalar(
            statement.order_by(PollRunRecord.started_at.desc(), PollRunRecord.id.desc()).limit(1)
        )

    def _sources(self, session: Session) -> tuple[SourcePollSummary, ...]:
        result: list[SourcePollSummary] = []
        rows = tuple(
            session.scalars(
                select(SourceRecord)
                .where(SourceRecord.state != SourceState.ARCHIVED.value)
                .order_by(SourceRecord.name, SourceRecord.id)
            )
        )
        for source in rows:
            latest = self._latest_poll(session, source.id)
            complete = self._latest_poll(
                session,
                source.id,
                PollRunRecord.completeness == PollCompleteness.COMPLETE.value,
            )
            any_success = self._latest_poll(
                session,
                source.id,
                PollRunRecord.completeness.in_(
                    (PollCompleteness.COMPLETE.value, PollCompleteness.PARTIAL.value)
                ),
                PollRunRecord.endpoint_succeeded > 0,
            )
            codes = () if latest is None else tuple(
                str(item) for item in cast(list[object], json.loads(latest.safe_error_codes_json))
            )
            result.append(
                SourcePollSummary(
                    source_id=source.id,
                    source_name=source.name,
                    lifecycle_state=source.state,
                    health=_source_health(source, latest),
                    endpoint_total=0 if latest is None else latest.endpoint_total,
                    endpoint_succeeded=0 if latest is None else latest.endpoint_succeeded,
                    endpoint_failed=(
                        0 if latest is None else latest.endpoint_total - latest.endpoint_succeeded
                    ),
                    last_poll_at=None if latest is None else _aware(latest.finished_at),
                    last_complete_success_at=(
                        None if complete is None else _aware(complete.finished_at)
                    ),
                    last_any_success_at=(
                        None if any_success is None else _aware(any_success.finished_at)
                    ),
                    safe_error_codes=codes,
                )
            )
        return tuple(result)

    def _watchdog(
        self,
        session: Session,
        sources: tuple[SourcePollSummary, ...],
        *,
        now: datetime,
    ) -> WatchdogHealthSummary:
        source_rows = {
            item.id: item
            for item in session.scalars(
                select(SourceRecord)
                .where(SourceRecord.state != SourceState.ARCHIVED.value)
                .order_by(SourceRecord.name, SourceRecord.id)
            )
        }
        source_health = {item.source_id: item.health for item in sources}
        source_summaries: list[WatchdogSourceSummary] = []
        all_clusters: list[WatchdogClusterSummary] = []
        monitored = 0
        unmonitored = 0
        for source_id, source in source_rows.items():
            latest = self._latest_poll(session, source_id)
            enabled = source.state == SourceState.ENABLED.value and source.watchdog_enabled
            if enabled:
                monitored += 1
                monitor_state = "ENABLED"
            else:
                unmonitored += 1
                monitor_state = (
                    "SOURCE_DISABLED"
                    if source.state != SourceState.ENABLED.value
                    else "DISABLED"
                )
            clusters: list[WatchdogClusterSummary] = []
            for row in session.scalars(
                select(WatchdogClusterRecord)
                .where(
                    WatchdogClusterRecord.source_id == source_id,
                    WatchdogClusterRecord.inventory_state != "IGNORED",
                )
                .order_by(WatchdogClusterRecord.identity_value)
            ):
                observed = _aware(row.last_observed_at)
                if not enabled or latest is None:
                    status = "unknown"
                    health_state = None if not enabled else "UNKNOWN"
                elif (
                    latest.completeness == PollCompleteness.PARTIAL.value
                    and observed is not None
                    and observed >= _aware(latest.started_at)
                ):
                    status = "healthy"
                    health_state = "HEALTHY"
                elif latest.completeness != PollCompleteness.COMPLETE.value:
                    status = "unknown"
                    health_state = "UNKNOWN"
                elif (
                    (freshness := _seconds_since(now, observed)) is not None
                    and freshness <= source.watchdog_missing_after_seconds
                ):
                    status = "healthy"
                    health_state = "HEALTHY"
                else:
                    status = "missing"
                    health_state = "MISSING"
                item = WatchdogClusterSummary(
                    id=row.id,
                    source_id=source_id,
                    identity_value=row.identity_value,
                    cluster=row.identity_value,
                    inventory_state=row.inventory_state,
                    health_state=health_state,
                    status=cast(WatchdogState, status),
                    first_discovered_at=_aware(row.created_at),
                    # The source creation time is not evidence that Watchdog
                    # monitoring was enabled at that instant.  The current
                    # schema does not retain that transition timestamp, so
                    # keep the preserved field explicitly unknown.
                    monitoring_started_at=None,
                    last_seen_at=observed,
                    ignored_at=None,
                    ignored_reason=None,
                    still_emitting=status == "healthy",
                    freshness_seconds=_seconds_since(now, observed),
                    version=None,
                )
                clusters.append(item)
                if enabled:
                    all_clusters.append(item)
            counts = WatchdogCounts(
                total=len(clusters) if enabled else 0,
                healthy=sum(item.status == "healthy" for item in clusters) if enabled else 0,
                missing=sum(item.status == "missing" for item in clusters) if enabled else 0,
                unknown=sum(item.status == "unknown" for item in clusters) if enabled else 0,
            )
            source_summaries.append(
                WatchdogSourceSummary(
                    source_id=source_id,
                    source_name=source.name,
                    source_health=source_health[source_id],
                    monitor_state=cast(WatchdogMonitorState, monitor_state),
                    summary=counts,
                    clusters=tuple(clusters),
                )
            )
        counts = WatchdogCounts(
            total=len(all_clusters),
            healthy=sum(item.status == "healthy" for item in all_clusters),
            missing=sum(item.status == "missing" for item in all_clusters),
            unknown=sum(item.status == "unknown" for item in all_clusters),
        )
        overall = (
            "missing"
            if counts.missing
            else "unknown"
            if counts.unknown
            else "healthy"
            if counts.total
            else "no_data"
        )
        return WatchdogHealthSummary(
            overall_status=cast(WatchdogOverallState, overall),
            summary=counts,
            clusters=tuple(all_clusters),
            monitored_source_count=monitored,
            unmonitored_source_count=unmonitored,
            sources=tuple(source_summaries),
        )

    def _notifications(
        self, session: Session, *, now: datetime
    ) -> NotificationHealthSummary:
        active_channels = int(
            session.scalar(
                select(func.count(NotificationChannelRecord.id)).where(
                    NotificationChannelRecord.enabled.is_(True),
                    NotificationChannelRecord.active_revision_id.is_not(None),
                )
            )
            or 0
        )
        active_policies = int(
            session.scalar(
                select(func.count(NotificationPolicyRevisionRecord.id)).where(
                    NotificationPolicyRevisionRecord.state == "ACTIVE"
                )
            )
            or 0
        )
        by_state = {
            str(state): int(count)
            for state, count in session.execute(
                select(NotificationDeliveryRecord.state, func.count(NotificationDeliveryRecord.id))
                .group_by(NotificationDeliveryRecord.state)
            )
        }
        pending = by_state.get(DeliveryState.PENDING.value, 0) + by_state.get(
            DeliveryState.IN_FLIGHT.value, 0
        )
        retrying = by_state.get(DeliveryState.RETRY_WAIT.value, 0)
        oldest = session.scalar(
            select(func.min(NotificationDeliveryRecord.scheduled_at)).where(
                NotificationDeliveryRecord.state.in_(
                    (
                        DeliveryState.PENDING.value,
                        DeliveryState.IN_FLIGHT.value,
                        DeliveryState.RETRY_WAIT.value,
                    )
                )
            )
        )
        permanent_failed = int(
            session.scalar(
                select(func.count(NotificationDeliveryRecord.id)).where(
                    NotificationDeliveryRecord.state == DeliveryState.PERMANENTLY_FAILED.value,
                    NotificationDeliveryRecord.updated_at
                    >= _stored(now - timedelta(hours=24)),
                )
            )
            or 0
        )
        latest_job = session.scalar(
            select(JobRecord)
            .where(JobRecord.kind == "notification.dispatch")
            .order_by(JobRecord.updated_at.desc(), JobRecord.id.desc())
            .limit(1)
        )
        latest_attempt_error = session.scalar(
            select(NotificationAttemptRecord.error_code)
            .order_by(
                NotificationAttemptRecord.started_at.desc(),
                NotificationAttemptRecord.id.desc(),
            )
            .limit(1)
        )
        worker_at = _aware(self._runner.heartbeat_at)
        error: str | None = None
        status = "disabled"
        oldest_seconds = _seconds_since(now, oldest)
        if active_policies:
            status = "healthy"
            if not active_channels:
                error = "CHANNEL_UNAVAILABLE"
            elif not self._runner.running or worker_at is None:
                error = "WORKER_NOT_STARTED"
            elif (
                (worker_age := _seconds_since(now, worker_at)) is not None
                and worker_age > NOTIFICATION_WORKER_STALE_SECONDS
            ):
                error = "WORKER_STALE"
            elif latest_job is not None and latest_job.state == JobState.FAILED.value:
                error = latest_job.safe_error_code or "NOTIFICATION_DISPATCH_FAILED"
            elif latest_attempt_error is not None:
                error = str(latest_attempt_error)
            elif pending + retrying >= NOTIFICATION_BACKLOG_DEGRADED_COUNT:
                error = "BACKLOG_COUNT"
            elif oldest_seconds is not None and oldest_seconds > NOTIFICATION_BACKLOG_DEGRADED_SECONDS:
                error = "BACKLOG_OLD"
            if error is not None:
                status = "degraded"
        return NotificationHealthSummary(
            status=cast(NotificationHealthState, status),
            worker_last_run_at=worker_at,
            pending=pending,
            retrying=retrying,
            permanent_failed_24h=permanent_failed,
            oldest_pending_seconds=oldest_seconds,
            active_channels=active_channels,
            active_policies=active_policies,
            last_error_code=error,
        )

    def _jobs(self, session: Session, *, now: datetime) -> JobRuntimeSummary:
        by_state = {
            str(state): int(count)
            for state, count in session.execute(
                select(JobRecord.state, func.count(JobRecord.id)).group_by(JobRecord.state)
            )
        }
        expired = int(
            session.scalar(
                select(func.count(JobRecord.id)).where(
                    JobRecord.state == JobState.RUNNING.value,
                    JobRecord.lease_expires_at.is_not(None),
                    JobRecord.lease_expires_at < _stored(now),
                )
            )
            or 0
        )
        oldest = session.scalar(
            select(func.min(JobRecord.created_at)).where(
                JobRecord.state == JobState.PENDING.value
            )
        )
        return JobRuntimeSummary(
            scheduler_running=self._scheduler.running,
            scheduler_heartbeat_at=_aware(self._scheduler.heartbeat_at),
            runner_running=self._runner.running,
            runner_heartbeat_at=_aware(self._runner.heartbeat_at),
            pending=by_state.get(JobState.PENDING.value, 0),
            running=by_state.get(JobState.RUNNING.value, 0),
            expired_leases=expired,
            oldest_pending_seconds=_seconds_since(now, oldest),
        )

    async def snapshot(self, *, now: datetime) -> PlatformHealthSnapshot:
        ready, raw_checks = await evaluate_readiness(
            runtime=self._runtime,
            probes=self._probes,
        )
        with self._sessions() as session:
            sources = self._sources(session)
            latest_poll = session.scalar(
                select(PollRunRecord)
                .join(SourceRecord, SourceRecord.id == PollRunRecord.source_id)
                .where(SourceRecord.state != SourceState.ARCHIVED.value)
                .order_by(PollRunRecord.finished_at.desc(), PollRunRecord.id.desc())
                .limit(1)
            )
            active_thanos = int(
                session.scalar(
                    select(func.count(MonitoringConnectionRecord.source_id))
                    .join(SourceRecord, SourceRecord.id == MonitoringConnectionRecord.source_id)
                    .where(
                        MonitoringConnectionRecord.kind == "THANOS",
                        MonitoringConnectionRecord.state == "ACTIVE",
                        SourceRecord.state == SourceState.ENABLED.value,
                    )
                )
                or 0
            )
            latest_backfill_job = session.scalar(
                select(JobRecord)
                .where(JobRecord.kind == "metrics.backfill")
                .order_by(JobRecord.updated_at.desc(), JobRecord.id.desc())
                .limit(1)
            )
            backfill_ok = None
            backfill_error = None
            latest_backfill_result = None
            if latest_backfill_job is not None:
                if latest_backfill_job.state == JobState.SUCCEEDED.value:
                    backfill_ok = True
                    latest_backfill_result = session.scalar(
                        select(MetricBackfillRunRecord)
                        .where(
                            MetricBackfillRunRecord.source_id
                            == latest_backfill_job.subject_id
                        )
                        .order_by(
                            MetricBackfillRunRecord.completed_at.desc(),
                            MetricBackfillRunRecord.id.desc(),
                        )
                        .limit(1)
                    )
                elif latest_backfill_job.state in {
                    JobState.FAILED.value,
                    JobState.CANCELED.value,
                }:
                    backfill_ok = False
                    backfill_error = (
                        latest_backfill_job.safe_error_code or "BACKFILL_JOB_FAILED"
                    )
            codes = () if latest_poll is None else tuple(
                str(item)
                for item in cast(
                    list[object], json.loads(latest_poll.safe_error_codes_json)
                )
            )
            incident_count = int(session.scalar(select(func.count(IncidentRecord.id))) or 0)
            return PlatformHealthSnapshot(
                status="ok",
                source_mode="REGISTRY" if sources else "UNCONFIGURED",
                configuration_status="configured" if sources else "unconfigured",
                configuration_errors=(),
                last_poll_at=None if latest_poll is None else _aware(latest_poll.finished_at),
                last_poll_ok=(
                    None
                    if latest_poll is None
                    else latest_poll.completeness == PollCompleteness.COMPLETE.value
                ),
                last_poll_error=codes[0] if codes else None,
                incident_count=incident_count,
                last_backfill_at=(
                    None
                    if latest_backfill_job is None
                    else _aware(
                        latest_backfill_job.finished_at
                        or latest_backfill_job.updated_at
                    )
                ),
                last_backfill_ok=backfill_ok,
                last_backfill_error=backfill_error,
                backfill_skipped=active_thanos == 0,
                backfill_effective_hours=(
                    0
                    if latest_backfill_result is None
                    else latest_backfill_result.effective_hours
                ),
                backfill_truncated_reason=(
                    None
                    if latest_backfill_result is None
                    else latest_backfill_result.truncated_reason
                ),
                alerts_reconstructed=(
                    0
                    if latest_backfill_result is None
                    else latest_backfill_result.alerts_reconstructed
                ),
                sources=sources,
                watchdog=self._watchdog(session, sources, now=now),
                notifications=self._notifications(session, now=now),
                readiness=ReadinessSummary(
                    status="ready" if ready else "not_ready",
                    checked_at=now,
                    checks=tuple(
                        HealthCheck(
                            name=item["name"],
                            status=cast(ReadinessState, item["status"]),
                            code=item["code"],
                        )
                        for item in raw_checks
                    ),
                ),
                jobs=self._jobs(session, now=now),
            )
