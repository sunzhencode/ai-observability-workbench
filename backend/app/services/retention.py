"""Deterministic cleanup of expired local SQLite runtime data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, inspect
from sqlmodel import Session, select

from app.registry_models import (
    AlertEndpointObservation,
    IncidentOccurrence,
    ModelCallLog,
)
from app.runtime_config import (
    MODEL_CALL_RETENTION_DAYS,
    OCCURRENCE_HISTORY_RETENTION_DAYS,
)
from app.models import (
    Alert,
    AlertTypeRule,
    ConfigAudit,
    GroupingPolicy,
    Incident,
    IncidentAudit,
    NotificationAttempt,
    NotificationDelivery,
    NotificationRoute,
    NotificationRouteTarget,
)


@dataclass(frozen=True)
class RetentionResult:
    alerts_deleted: int = 0
    endpoint_observations_deleted: int = 0
    audits_deleted: int = 0
    policies_deleted: int = 0
    alert_type_rules_deleted: int = 0
    incidents_deleted: int = 0
    notification_attempts_deleted: int = 0
    notification_deliveries_deleted: int = 0
    notification_route_targets_deleted: int = 0
    notification_routes_deleted: int = 0
    config_audits_deleted: int = 0
    occurrence_history_deleted: int = 0
    model_calls_deleted: int = 0


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _delete_orphan_endpoint_observations(session: Session) -> int:
    """Drop endpoint observations whose Alert is gone.

    `AlertEndpointObservation.alert_id` deliberately carries no foreign key --
    Alert lives in the other metadata -- so deleting an expired Alert leaves the
    row behind silently. They accumulate forever, and because SQLite reuses
    rowids a later Alert can inherit one and be credited with an endpoint
    observation it never had. Nothing else reclaims them: the per-source poll
    audit prune only covers PollRun/EndpointPollResult, and the endpoint-level
    delete only fires when a user removes an endpoint position.
    """
    if not inspect(session.connection()).has_table("alertendpointobservation"):
        return 0
    result = session.execute(
        delete(AlertEndpointObservation).where(
            AlertEndpointObservation.alert_id.not_in(select(Alert.id))
        )
    )
    session.flush()
    return int(result.rowcount or 0)


def _delete_expired_occurrence_history(
    session: Session, now: datetime, retention_days: int
) -> int:
    """Prune occurrence history on its *own* clock (F26 / CAP-09.6a).

    Deliberately not the 30-day cutoff used for everything else in this module:
    that one deletes terminal facts, while a history record is the only surviving
    evidence that an occurrence happened at all. Sharing the number would quietly
    declare that history is worth 30 days.

    `inspect(session.connection())` rather than `inspect(engine)`: the engine form
    checks out a *second* connection from the pool, which under `StaticPool` is the
    one this session is already using -- the subsequent rollback then discards
    whatever the caller had just flushed.
    """
    if not inspect(session.connection()).has_table("incidentoccurrence"):
        return 0
    cutoff = _utc(now) - timedelta(days=max(0, retention_days))
    result = session.execute(
        delete(IncidentOccurrence).where(IncidentOccurrence.recovered_at < cutoff)
    )
    session.flush()
    return int(result.rowcount or 0)


def _delete_expired_model_calls(
    session: Session, now: datetime, retention_days: int
) -> int:
    """Prune the model call log on its own clock (F27).

    It was added for audit and idempotency and then left out of cleanup, which
    in a local SQLite file is a slow leak with no upper bound. A third window
    rather than reusing either existing number: this is a **spending** record,
    so 30 days would delete the answer to "what did last quarter cost".

    `inspect(session.connection())`, not `inspect(engine)` -- the engine form
    checks a second connection out of the pool, which under `StaticPool` is the
    one this session is using, and the following rollback discards whatever the
    caller just flushed.
    """
    if not inspect(session.connection()).has_table("modelcalllog"):
        return 0
    cutoff = _utc(now) - timedelta(days=max(0, retention_days))
    result = session.execute(
        delete(ModelCallLog).where(ModelCallLog.created_at < cutoff)
    )
    session.flush()
    return int(result.rowcount or 0)


def cleanup_expired_data(
    session: Session,
    now: datetime,
    retention_days: int,
    occurrence_history_retention_days: int | None = None,
    model_call_retention_days: int | None = None,
) -> RetentionResult:
    cutoff = _utc(now) - timedelta(days=max(0, retention_days))
    alerts_deleted = audits_deleted = policies_deleted = 0
    alert_type_rules_deleted = incidents_deleted = 0
    notification_attempts_deleted = notification_deliveries_deleted = 0
    notification_route_targets_deleted = notification_routes_deleted = 0
    config_audits_deleted = endpoint_observations_deleted = 0

    for alert in session.exec(select(Alert)).all():
        if alert.source_state == "resolved" and _utc(alert.last_seen_at) < cutoff:
            session.delete(alert)
            alerts_deleted += 1
    session.flush()
    endpoint_observations_deleted = _delete_orphan_endpoint_observations(session)

    for audit in session.exec(select(IncidentAudit)).all():
        if _utc(audit.created_at) < cutoff:
            session.delete(audit)
            audits_deleted += 1

    for policy in session.exec(select(GroupingPolicy)).all():
        if not policy.enabled and _utc(policy.created_at) < cutoff:
            session.delete(policy)
            policies_deleted += 1

    for rule in session.exec(select(AlertTypeRule)).all():
        if not rule.enabled and _utc(rule.created_at) < cutoff:
            session.delete(rule)
            alert_type_rules_deleted += 1

    for audit in session.exec(select(ConfigAudit)).all():
        if _utc(audit.created_at) < cutoff:
            session.delete(audit)
            config_audits_deleted += 1

    terminal_states = {"SUCCEEDED", "PERMANENT_FAILED", "SUPPRESSED", "CANCELED"}
    for delivery in session.exec(select(NotificationDelivery)).all():
        if delivery.state not in terminal_states or _utc(delivery.updated_at) >= cutoff:
            continue
        attempts = session.exec(
            select(NotificationAttempt).where(
                NotificationAttempt.delivery_id == delivery.id
            )
        ).all()
        for attempt in attempts:
            session.delete(attempt)
            notification_attempts_deleted += 1
        session.flush()
        session.delete(delivery)
        notification_deliveries_deleted += 1

    session.flush()
    for route in session.exec(select(NotificationRoute)).all():
        if (
            route.status != "TERMINATED"
            or route.closed_at is None
            or _utc(route.closed_at) >= cutoff
        ):
            continue
        has_delivery = session.exec(
            select(NotificationDelivery.id).where(
                NotificationDelivery.route_id == route.id
            )
        ).first()
        if has_delivery is not None:
            continue
        targets = session.exec(
            select(NotificationRouteTarget).where(
                NotificationRouteTarget.route_id == route.id
            )
        ).all()
        for target in targets:
            session.delete(target)
            notification_route_targets_deleted += 1
        session.flush()
        session.delete(route)
        notification_routes_deleted += 1

    session.flush()
    for incident in session.exec(select(Incident)).all():
        if incident.superseded_by_incident_id is not None:
            # F20 collision history is deliberately retained as a read-only
            # audit object even after its Alert members move to the canonical.
            continue
        has_member = session.exec(
            select(Alert.id).where(Alert.incident_id == incident.id)
        ).first()
        has_audit = session.exec(
            select(IncidentAudit.id).where(IncidentAudit.incident_id == incident.id)
        ).first()
        has_route = session.exec(
            select(NotificationRoute.id).where(
                NotificationRoute.incident_id == incident.id
            )
        ).first()
        if has_member is None and has_audit is None and has_route is None:
            session.delete(incident)
            incidents_deleted += 1

    if occurrence_history_retention_days is None:
        occurrence_history_retention_days = OCCURRENCE_HISTORY_RETENTION_DAYS
    occurrence_history_deleted = _delete_expired_occurrence_history(
        session, now, occurrence_history_retention_days
    )

    if model_call_retention_days is None:
        model_call_retention_days = MODEL_CALL_RETENTION_DAYS
    model_calls_deleted = _delete_expired_model_calls(
        session, now, model_call_retention_days
    )

    session.commit()
    return RetentionResult(
        alerts_deleted=alerts_deleted,
        endpoint_observations_deleted=endpoint_observations_deleted,
        audits_deleted=audits_deleted,
        policies_deleted=policies_deleted,
        alert_type_rules_deleted=alert_type_rules_deleted,
        incidents_deleted=incidents_deleted,
        notification_attempts_deleted=notification_attempts_deleted,
        notification_deliveries_deleted=notification_deliveries_deleted,
        notification_route_targets_deleted=notification_route_targets_deleted,
        notification_routes_deleted=notification_routes_deleted,
        config_audits_deleted=config_audits_deleted,
        occurrence_history_deleted=occurrence_history_deleted,
        model_calls_deleted=model_calls_deleted,
    )
