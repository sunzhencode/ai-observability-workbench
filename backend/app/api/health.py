"""Health endpoint: source connectivity and data freshness."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, inspect
from sqlmodel import Session, select

from app.db import get_session
from app.crypto import SecretError
from app.registry_models import EventSource, SourcePollRun
from app.models import (
    Alert,
    Incident,
    NotificationChannel,
    NotificationDelivery,
    NotificationPolicyRevision,
)
from app.runtime_config import ActiveRuntimeConfig, runtime_config_provider
from app.schemas import (
    Health,
    NotificationHealth,
    SourcePollHealth,
    WatchdogClusterHealth,
    WatchdogHealth,
    WatchdogSourceClusterHealth,
    WatchdogSourceHealth,
    WatchdogSummary,
)
from app.state import backfill_status, notification_status, poll_status
from app.services.alert_types import is_watchdog_grouping
from app.services.notification_channels import resolve_active_channel_config
from app.services.source_identity import active_source_id
from app.services.source_scope import visible_source_ids
from app.services.watchdog import derive_watchdog_health

router = APIRouter()

# Health thresholds: what "the delivery worker looks unwell" means. Product
# judgement, not per-install tuning.
NOTIFICATION_WORKER_STALE_SECONDS = 60
NOTIFICATION_BACKLOG_DEGRADED_SECONDS = 300
NOTIFICATION_BACKLOG_DEGRADED_COUNT = 100


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _has_f20_sources_table(session: Session) -> bool:
    return bool(inspect(session.connection()).has_table("eventsource"))


def _event_sources(session: Session) -> list[EventSource]:
    """Managed sources only.

    Archived rows are excluded so that migration placeholders cannot make the
    registry look populated; see docs/superpowers/specs/archive/HISTORY.md §3.3.
    """
    if not _has_f20_sources_table(session):
        return []
    return list(
        session.exec(
            select(EventSource)
            .where(EventSource.lifecycle_state != "ARCHIVED")
            .order_by(EventSource.name, EventSource.id)
        ).all()
    )


def _latest_run(
    session: Session, source_id: str, *extra_conditions
) -> SourcePollRun | None:
    """Most recent poll run for a source, optionally narrowed.

    Health only ever needs three specific runs per source, so it asks for those
    rather than loading the whole retained audit (200 rows per source) and
    picking from it in Python -- on an endpoint the UI polls every 15s.
    """
    statement = select(SourcePollRun).where(SourcePollRun.source_id == source_id)
    for condition in extra_conditions:
        statement = statement.where(condition)
    return session.exec(
        statement.order_by(
            SourcePollRun.started_at.desc(), SourcePollRun.id.desc()
        ).limit(1)
    ).first()


def _source_health(session: Session, sources: list[EventSource]) -> list[SourcePollHealth]:
    result: list[SourcePollHealth] = []
    for source in sources:
        latest = _latest_run(session, source.id)
        if source.lifecycle_state == "DISABLED":
            health = "DISABLED"
        elif source.lifecycle_state == "ARCHIVED":
            health = "ARCHIVED"
        elif latest is None:
            health = "UNAVAILABLE"
        elif latest.completeness == "COMPLETE":
            health = "HEALTHY"
        elif latest.completeness == "PARTIAL":
            health = "DEGRADED"
        else:
            health = "UNAVAILABLE"
        last_complete = _latest_run(
            session, source.id, SourcePollRun.completeness == "COMPLETE"
        )
        last_any_success = _latest_run(
            session,
            source.id,
            SourcePollRun.completeness.in_(["COMPLETE", "PARTIAL"]),
            SourcePollRun.endpoint_succeeded > 0,
        )
        result.append(
            SourcePollHealth(
                source_id=source.id,
                source_name=source.name,
                lifecycle_state=source.lifecycle_state,
                health=health,
                endpoint_total=latest.endpoint_total if latest is not None else 0,
                endpoint_succeeded=(
                    latest.endpoint_succeeded if latest is not None else 0
                ),
                endpoint_failed=latest.endpoint_failed if latest is not None else 0,
                last_poll_at=(
                    latest.finished_at or latest.started_at
                    if latest is not None
                    else None
                ),
                last_complete_success_at=(
                    last_complete.finished_at or last_complete.started_at
                    if last_complete is not None
                    else None
                ),
                last_any_success_at=(
                    last_any_success.finished_at or last_any_success.started_at
                    if last_any_success is not None
                    else None
                ),
                safe_error_codes=list(latest.safe_error_codes or [])
                if latest is not None
                else [],
            )
        )
    return result


def _legacy_watchdog_health(
    session: Session, runtime: ActiveRuntimeConfig
) -> WatchdogHealth:
    source_id = active_source_id(runtime)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=runtime.retention_days)
    last_seen_by_cluster: dict[str, datetime] = {}
    stored = session.exec(
        select(Alert)
        .where(
            Alert.alertname == "Watchdog",
            Alert.source_id == source_id,
        )
        .order_by(Alert.last_seen_at.desc())
    ).all()
    for alert in stored:
        last_seen = _aware_utc(alert.last_seen_at)
        if last_seen < cutoff:
            continue
        cluster = alert.cluster or "<no-cluster>"
        previous = last_seen_by_cluster.get(cluster)
        if previous is None or last_seen > previous:
            last_seen_by_cluster[cluster] = last_seen

    if not last_seen_by_cluster:
        return WatchdogHealth(
            overall_status="no_data",
            summary=WatchdogSummary(total=0, healthy=0, missing=0, unknown=0),
            clusters=[],
        )

    clusters: list[WatchdogClusterHealth] = []
    counts = {"healthy": 0, "missing": 0, "unknown": 0}
    for cluster, last_seen in last_seen_by_cluster.items():
        if poll_status.source_id != source_id or poll_status.last_poll_ok is not True:
            status = "unknown"
        elif cluster in poll_status.watchdog_current_clusters:
            status = "healthy"
        else:
            status = "missing"
        counts[status] += 1
        clusters.append(
            WatchdogClusterHealth(
                cluster=cluster,
                status=status,
                last_seen_at=last_seen,
                freshness_seconds=max(0, int((now - last_seen).total_seconds())),
            )
        )

    order = {"missing": 0, "unknown": 1, "healthy": 2}
    clusters.sort(key=lambda item: (order[item.status], item.cluster))
    overall_status = (
        "unknown"
        if counts["unknown"]
        else "missing"
        if counts["missing"]
        else "healthy"
    )
    return WatchdogHealth(
        overall_status=overall_status,
        summary=WatchdogSummary(
            total=len(clusters),
            healthy=counts["healthy"],
            missing=counts["missing"],
            unknown=counts["unknown"],
        ),
        clusters=clusters,
    )


def _f20_watchdog_health(session: Session) -> WatchdogHealth:
    derived = derive_watchdog_health(session)
    return WatchdogHealth(
        overall_status=derived["overall_status"],
        summary=WatchdogSummary(**derived["summary"]),
        clusters=[
            WatchdogClusterHealth(
                cluster=item["cluster"],
                status=item["status"],
                last_seen_at=item["last_seen_at"],
                freshness_seconds=item["freshness_seconds"],
            )
            for item in derived["clusters"]
        ],
        monitored_source_count=derived["monitored_source_count"],
        unmonitored_source_count=derived["unmonitored_source_count"],
        sources=[
            WatchdogSourceHealth(
                source_id=source["source_id"],
                source_name=source["source_name"],
                source_health=source["source_health"],
                monitor_state=source["monitor_state"],
                summary=WatchdogSummary(**source["summary"]),
                clusters=[
                    WatchdogSourceClusterHealth.model_validate(item)
                    for item in source["clusters"]
                ],
            )
            for source in derived["sources"]
        ],
    )


def _watchdog_health(
    session: Session, runtime: ActiveRuntimeConfig, sources: list[EventSource]
) -> WatchdogHealth:
    if sources:
        return _f20_watchdog_health(session)
    return _legacy_watchdog_health(session, runtime)


def _notification_health(session: Session) -> NotificationHealth:
    """Report notification health without changing source/poll semantics."""
    now = datetime.now(timezone.utc)
    channels = session.exec(
        select(NotificationChannel).where(
            NotificationChannel.state == "ENABLED",
            NotificationChannel.active_revision_id.is_not(None),
        )
    ).all()
    policies = session.exec(
        select(NotificationPolicyRevision).where(
            NotificationPolicyRevision.state == "ACTIVE"
        )
    ).all()
    # Counted in SQL rather than by materialising the Outbox. Retention keeps
    # 30 days of terminal deliveries, and this endpoint is polled every 15s, so
    # loading the table grew the health check with the history.
    by_state = {
        str(state): int(count)
        for state, count in session.exec(
            select(NotificationDelivery.state, func.count(NotificationDelivery.id))
            .group_by(NotificationDelivery.state)
        ).all()
    }
    pending = by_state.get("PENDING", 0) + by_state.get("IN_FLIGHT", 0)
    retrying = by_state.get("RETRY_WAIT", 0)
    outstanding_count = pending + retrying
    oldest_scheduled_at = session.exec(
        select(func.min(NotificationDelivery.scheduled_at)).where(
            NotificationDelivery.state.in_(["PENDING", "IN_FLIGHT", "RETRY_WAIT"])
        )
    ).first()
    oldest_seconds = (
        max(0, int((now - _aware_utc(oldest_scheduled_at)).total_seconds()))
        if oldest_scheduled_at is not None
        else None
    )
    failed_cutoff = now - timedelta(hours=24)
    failed_24h = int(
        session.exec(
            select(func.count(NotificationDelivery.id)).where(
                NotificationDelivery.state == "PERMANENT_FAILED",
                NotificationDelivery.updated_at >= failed_cutoff,
            )
        ).first()
        or 0
    )

    error_code: str | None = None
    status = "disabled"
    if policies:
        status = "healthy"
        if not channels:
            error_code = "CHANNEL_UNAVAILABLE"
        else:
            try:
                for channel in channels:
                    resolve_active_channel_config(session, int(channel.id))
            except (SecretError, ValueError):
                error_code = "SECRET_UNAVAILABLE"
        if error_code is None and notification_status.last_error_code:
            error_code = notification_status.last_error_code
        if error_code is None and notification_status.last_run_at is None:
            error_code = "WORKER_NOT_STARTED"
        if error_code is None and notification_status.last_run_at is not None:
            age = (now - _aware_utc(notification_status.last_run_at)).total_seconds()
            if age > max(1, NOTIFICATION_WORKER_STALE_SECONDS):
                error_code = "WORKER_STALE"
        if error_code is None and outstanding_count >= max(
            1, NOTIFICATION_BACKLOG_DEGRADED_COUNT
        ):
            error_code = "BACKLOG_COUNT"
        if error_code is None and oldest_seconds is not None and oldest_seconds > max(
            1, NOTIFICATION_BACKLOG_DEGRADED_SECONDS
        ):
            error_code = "BACKLOG_OLD"
        if error_code is not None:
            status = "degraded"

    return NotificationHealth(
        status=status,
        worker_last_run_at=notification_status.last_run_at,
        pending=pending,
        retrying=retrying,
        permanent_failed_24h=failed_24h,
        oldest_pending_seconds=oldest_seconds,
        active_channels=len(channels),
        active_policies=len(policies),
        last_error_code=error_code,
    )
@router.get("/health", response_model=Health)
def health(session: Session = Depends(get_session)) -> Health:
    runtime = runtime_config_provider.snapshot()
    sources = _event_sources(session)
    source_id = active_source_id(runtime)
    # One rule for "what is visible", shared with /api/incidents so the count and
    # the list can never disagree. F21: the registry decides, with no `.env`
    # fallback; a pre-F20 database without registry tables is not filtered.
    visible_ids = visible_source_ids(session)
    # Only the two fields the watchdog predicate reads. It matches on group_key
    # and title, so the filter cannot move into SQL, but materialising whole
    # Incident rows just to count them can be avoided.
    incident_statement = select(Incident.group_key, Incident.title).where(
        Incident.superseded_by_incident_id.is_(None)
    )
    if visible_ids:
        incident_statement = incident_statement.where(
            Incident.source_id.in_(visible_ids)
        )
    else:
        incident_statement = incident_statement.where(Incident.id == -1)
    incident_count = sum(
        1
        for group_key, title in session.exec(incident_statement).all()
        if not is_watchdog_grouping(group_key, title)
    )
    # F21: the registry is the only source of truth, so there are two states.
    source_mode = "REGISTRY" if sources else "UNCONFIGURED"

    return Health(
        status="ok",
        source_mode=source_mode,
        configuration_status=(
            "degraded"
            if runtime.configuration_errors
            else "unconfigured"
            if not sources
            else "configured"
        ),
        configuration_errors=list(runtime.configuration_errors),
        last_poll_at=poll_status.last_poll_at,
        last_poll_ok=poll_status.last_poll_ok,
        last_poll_error=poll_status.last_poll_error,
        incident_count=incident_count,
        last_backfill_at=backfill_status.last_backfill_at,
        last_backfill_ok=backfill_status.last_backfill_ok,
        last_backfill_error=backfill_status.last_backfill_error,
        backfill_skipped=backfill_status.skipped,
        backfill_effective_hours=backfill_status.effective_hours,
        backfill_truncated_reason=backfill_status.truncated_reason,
        alerts_reconstructed=backfill_status.alerts_reconstructed,
        sources=_source_health(session, sources),
        watchdog=_watchdog_health(session, runtime, sources),
        notifications=_notification_health(session),
    )
