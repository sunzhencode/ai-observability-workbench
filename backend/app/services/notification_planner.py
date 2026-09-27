"""Purely local Incident change planner; it never performs network I/O."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlmodel import Session, select

from app.models import (
    Alert,
    Incident,
    IncidentAudit,
    NotificationChannel,
    NotificationChannelRevision,
    NotificationDelivery,
    NotificationPolicyRevision,
    NotificationRoute,
    NotificationRouteTarget,
)
from app.services.grouping import severity_rank
from app.services.notification_policies import (
    active_policy_candidates,
    choose_policy,
    incident_route_context,
)
from app.services.source_scope import source_name_map


@dataclass(frozen=True)
class IncidentSnapshot:
    source_state: str
    severity: str
    handling_state: str
    aggregation_rule_id: int | None
    group_labels: dict[str, str]
    occurrence_no: int


@dataclass(frozen=True)
class IncidentChange:
    incident_id: int
    occurrence_no: int
    previous_source_state: str | None
    current_source_state: str
    previous_severity: str | None
    current_severity: str
    previous_handling_state: str | None
    current_handling_state: str
    change_version: int
    origin: str
    observed_at: datetime


def snapshot_incidents(
    session: Session, *, source_id: str
) -> dict[int, IncidentSnapshot]:
    result: dict[int, IncidentSnapshot] = {}
    for incident in session.exec(
        select(Incident).where(
            Incident.source_id == source_id,
            Incident.superseded_by_incident_id.is_(None),
        )
    ).all():
        if incident.id is None:
            continue
        result[incident.id] = IncidentSnapshot(
            source_state=incident.source_state,
            severity=incident.severity,
            handling_state=incident.handling_state,
            aggregation_rule_id=incident.aggregation_rule_id,
            group_labels=dict(incident.group_labels),
            occurrence_no=incident.occurrence_no,
        )
    return result


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _occurrence_start(
    session: Session, incident: Incident, fallback: datetime
) -> datetime:
    values: list[datetime] = []
    members = session.exec(
        select(Alert).where(
            Alert.incident_id == incident.id,
            Alert.source_state != "resolved",
        )
    ).all()
    for alert in members:
        value = alert.starts_at or alert.first_seen_at
        if value is not None:
            values.append(_aware(value))
    return min(values) if values else fallback


def _meaningful_change(
    previous: IncidentSnapshot | None, incident: Incident
) -> bool:
    if previous is None:
        return True
    return any(
        (
            previous.source_state != incident.source_state,
            previous.severity != incident.severity,
            previous.handling_state != incident.handling_state,
            previous.aggregation_rule_id != incident.aggregation_rule_id,
            previous.group_labels != incident.group_labels,
        )
    )


def reconcile_incident_changes(
    session: Session,
    before: dict[int, IncidentSnapshot],
    *,
    source_id: str,
    observed_at: datetime,
    origin: str,
) -> list[IncidentChange]:
    changes: list[IncidentChange] = []
    incidents = session.exec(
        select(Incident).where(
            Incident.source_id == source_id,
            Incident.superseded_by_incident_id.is_(None),
        )
    ).all()
    for incident in incidents:
        if incident.id is None:
            continue
        previous = before.get(incident.id)
        if not _meaningful_change(previous, incident):
            continue
        recurrence = (
            origin == "LIVE_POLL"
            and previous is not None
            and previous.source_state == "recovered"
            and incident.source_state == "firing"
        )
        previous_handling = previous.handling_state if previous else None
        if recurrence:
            incident.occurrence_no = previous.occurrence_no + 1
            incident.occurrence_started_at = _occurrence_start(
                session, incident, observed_at
            )
            if incident.handling_state != "NEW":
                session.add(
                    IncidentAudit(
                        incident_id=incident.id,
                        actor="system",
                        from_state=incident.handling_state,
                        to_state="NEW",
                        reason="source_recurrence",
                        created_at=observed_at,
                    )
                )
                incident.handling_state = "NEW"
        incident.change_version += 1
        incident.change_origin = origin
        incident.updated_at = observed_at
        session.add(incident)
        session.flush()
        change = IncidentChange(
            incident_id=incident.id,
            occurrence_no=incident.occurrence_no,
            previous_source_state=previous.source_state if previous else None,
            current_source_state=incident.source_state,
            previous_severity=previous.severity if previous else None,
            current_severity=incident.severity,
            previous_handling_state=previous_handling,
            current_handling_state=incident.handling_state,
            change_version=incident.change_version,
            origin=origin,
            observed_at=observed_at,
        )
        plan_incident_change(session, incident, change)
        changes.append(change)
    session.flush()
    return changes


def _route_for_occurrence(
    session: Session, incident: Incident
) -> NotificationRoute | None:
    return session.exec(
        select(NotificationRoute).where(
            NotificationRoute.incident_id == incident.id,
            NotificationRoute.occurrence_no == incident.occurrence_no,
        )
    ).first()


def _payload_snapshot(
    session: Session, incident: Incident, change: IncidentChange
) -> dict[str, Any]:
    names = source_name_map(session)
    members = session.exec(
        select(Alert).where(Alert.incident_id == incident.id)
    ).all()
    members.sort(
        key=lambda item: (
            0 if item.source_state != "resolved" else 1,
            -severity_rank(item.severity),
            item.alertname,
            item.upstream_fingerprint or item.fingerprint,
        )
    )
    safe_members: list[dict[str, str]] = []
    for alert in members[:5]:
        summary = ""
        for key in ("summary", "description", "message"):
            if key in alert.annotations and not any(
                part in key.lower()
                for part in ("token", "password", "secret", "webhook", "authorization")
            ):
                summary = str(alert.annotations.get(key) or "")[:240]
                if summary:
                    break
        safe_members.append(
            {
                "alertname": alert.alertname[:160],
                "severity": alert.severity[:32],
                "source_state": alert.source_state[:32],
                "cluster": str(alert.labels.get("cluster") or alert.cluster or "")[:160],
                "namespace": str(alert.labels.get("namespace") or "")[:160],
                "service": str(alert.labels.get("service") or "")[:160],
                "job": str(alert.labels.get("job") or "")[:160],
                "workload": str(alert.labels.get("workload") or "")[:160],
                "summary": summary,
            }
        )
    return {
        "incident_id": incident.id,
        "occurrence_no": incident.occurrence_no,
        "title": incident.title[:512],
        "severity": incident.severity,
        "source_state": incident.source_state,
        "change_version": change.change_version,
        "source_id": incident.source_id,
        "source_name": names.get(incident.source_id) or incident.source_id,
        "group_labels": {
            str(key)[:128]: str(value)[:256]
            for key, value in sorted(incident.group_labels.items())
        },
        "member_count": len(members),
        "members": safe_members,
        "members_truncated": max(0, len(members) - len(safe_members)),
    }


def _event_key(
    *,
    incident_id: int,
    route_id: int,
    target_id: int,
    event_type: str,
    change_version: int,
    repeat_slot: int | None = None,
) -> str:
    raw = "|".join(
        str(item)
        for item in (
            incident_id,
            route_id,
            target_id,
            event_type,
            change_version,
            "" if repeat_slot is None else repeat_slot,
        )
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _create_delivery(
    session: Session,
    *,
    incident: Incident,
    route: NotificationRoute,
    target: NotificationRouteTarget,
    event_type: str,
    change: IncidentChange,
    repeat_slot: int | None = None,
) -> NotificationDelivery:
    key = _event_key(
        incident_id=incident.id,
        route_id=route.id,
        target_id=target.id,
        event_type=event_type,
        change_version=change.change_version,
        repeat_slot=repeat_slot,
    )
    existing = session.exec(
        select(NotificationDelivery).where(NotificationDelivery.event_key == key)
    ).first()
    if existing is not None:
        return existing
    delivery = NotificationDelivery(
        event_key=key,
        incident_id=incident.id,
        route_id=route.id,
        route_target_id=target.id,
        event_type=event_type,
        incident_change_version=change.change_version,
        repeat_slot=repeat_slot,
        payload_snapshot_json=_payload_snapshot(session, incident, change),
        scheduled_at=change.observed_at,
        next_attempt_at=change.observed_at,
    )
    channel = session.get(NotificationChannel, target.channel_id)
    if channel is None or channel.state != "ENABLED":
        delivery.state = "SUPPRESSED"
        delivery.suppression_reason = "CHANNEL_DISABLED"
    session.add(delivery)
    session.flush()
    session.refresh(delivery)
    return delivery


def _cancel_open_deliveries(
    session: Session, target: NotificationRouteTarget
) -> None:
    deliveries = session.exec(
        select(NotificationDelivery).where(
            NotificationDelivery.route_target_id == target.id,
            NotificationDelivery.event_type == "FIRING_OPENED",
            NotificationDelivery.state.in_(["PENDING", "RETRY_WAIT"]),
        )
    ).all()
    for delivery in deliveries:
        delivery.state = "CANCELED"
        delivery.suppression_reason = "SUPERSEDED_BEFORE_OPEN"
        session.add(delivery)


def _cancel_pending_route_deliveries(
    session: Session, route: NotificationRoute, reason: str
) -> None:
    deliveries = session.exec(
        select(NotificationDelivery).where(
            NotificationDelivery.route_id == route.id,
            NotificationDelivery.state.in_(["PENDING", "RETRY_WAIT"]),
        )
    ).all()
    for delivery in deliveries:
        delivery.state = "CANCELED"
        delivery.suppression_reason = reason
        session.add(delivery)


def _create_route(
    session: Session, incident: Incident, change: IncidentChange
) -> NotificationRoute | None:
    context = incident_route_context(incident)
    winner = choose_policy(context, active_policy_candidates(session))
    if winner is None:
        return None
    policy = session.get(NotificationPolicyRevision, winner.id)
    if policy is None:
        return None
    target_rows: list[tuple[NotificationChannel, NotificationChannelRevision]] = []
    for channel_id in winner.channel_ids:
        channel = session.get(NotificationChannel, channel_id)
        if (
            channel is None
            or channel.state != "ENABLED"
            or channel.active_revision_id is None
        ):
            continue
        revision = session.get(
            NotificationChannelRevision, channel.active_revision_id
        )
        if revision is not None and revision.state == "ACTIVE":
            target_rows.append((channel, revision))
    if not target_rows:
        return None
    route = NotificationRoute(
        incident_id=incident.id,
        occurrence_no=incident.occurrence_no,
        policy_revision_id=policy.id,
        policy_name=policy.name,
        policy_version=policy.version,
        policy_priority=policy.priority,
        match_context_json=context,
        repeat_interval_seconds=policy.repeat_interval_seconds,
        created_at=change.observed_at,
    )
    session.add(route)
    session.flush()
    session.refresh(route)
    for channel, revision in target_rows:
        target = NotificationRouteTarget(
            route_id=route.id,
            channel_id=channel.id,
            routed_channel_revision_id=revision.id,
            channel_name=channel.name,
            provider=revision.provider,
            channel_version=revision.version,
            mention_mode=revision.mention_mode,
            mention_users=list(revision.mention_users),
            mention_on=dict(revision.mention_on),
        )
        session.add(target)
        session.flush()
        session.refresh(target)
        _create_delivery(
            session,
            incident=incident,
            route=route,
            target=target,
            event_type="FIRING_OPENED",
            change=change,
        )
    return route


def plan_incident_change(
    session: Session, incident: Incident, change: IncidentChange
) -> NotificationRoute | None:
    route = _route_for_occurrence(session, incident)
    if route is None:
        if (
            change.current_source_state == "firing"
            and change.origin in {"LIVE_POLL", "EXPLICIT_ACTIVATION"}
            and change.current_handling_state not in {"CLOSED", "FALSE_POSITIVE"}
            and incident.freshness_state != "STALE"
        ):
            return _create_route(session, incident, change)
        return None

    if change.current_handling_state in {"CLOSED", "FALSE_POSITIVE"}:
        route.status = "TERMINATED"
        route.termination_reason = change.current_handling_state
        route.closed_at = change.observed_at
        _cancel_pending_route_deliveries(
            session, route, f"HANDLING_{change.current_handling_state}"
        )
        session.add(route)
        session.flush()
        return route

    targets = session.exec(
        select(NotificationRouteTarget).where(
            NotificationRouteTarget.route_id == route.id
        )
    ).all()
    if change.current_source_state == "recovered":
        for target in targets:
            if target.opened_success_at is None:
                _cancel_open_deliveries(session, target)
                continue
            _create_delivery(
                session,
                incident=incident,
                route=route,
                target=target,
                event_type="RECOVERED",
                change=change,
            )
        route.status = "RECOVERED"
        route.closed_at = change.observed_at
        session.add(route)
        session.flush()
        return route

    escalated = (
        change.previous_severity is not None
        and severity_rank(change.current_severity)
        > severity_rank(change.previous_severity)
    )
    if change.current_source_state == "firing" and escalated:
        for target in targets:
            if target.opened_success_at is None:
                _cancel_open_deliveries(session, target)
                _create_delivery(
                    session,
                    incident=incident,
                    route=route,
                    target=target,
                    event_type="FIRING_OPENED",
                    change=change,
                )
            else:
                _create_delivery(
                    session,
                    incident=incident,
                    route=route,
                    target=target,
                    event_type="SEVERITY_ESCALATED",
                    change=change,
                )
    session.flush()
    return route


def plan_handling_change(
    session: Session,
    incident: Incident,
    *,
    previous_handling_state: str,
    observed_at: datetime,
) -> IncidentChange:
    incident.change_version += 1
    incident.change_origin = "HANDLING"
    session.add(incident)
    session.flush()
    change = IncidentChange(
        incident_id=incident.id,
        occurrence_no=incident.occurrence_no,
        previous_source_state=incident.source_state,
        current_source_state=incident.source_state,
        previous_severity=incident.severity,
        current_severity=incident.severity,
        previous_handling_state=previous_handling_state,
        current_handling_state=incident.handling_state,
        change_version=incident.change_version,
        origin="HANDLING",
        observed_at=observed_at,
    )
    plan_incident_change(session, incident, change)
    session.flush()
    return change


def plan_explicit_activation(
    session: Session, incident: Incident, *, observed_at: datetime
) -> NotificationRoute | None:
    incident.change_version += 1
    incident.change_origin = "EXPLICIT_ACTIVATION"
    session.add(incident)
    session.flush()
    change = IncidentChange(
        incident_id=incident.id,
        occurrence_no=incident.occurrence_no,
        previous_source_state=incident.source_state,
        current_source_state=incident.source_state,
        previous_severity=incident.severity,
        current_severity=incident.severity,
        previous_handling_state=incident.handling_state,
        current_handling_state=incident.handling_state,
        change_version=incident.change_version,
        origin="EXPLICIT_ACTIVATION",
        observed_at=observed_at,
    )
    return plan_incident_change(session, incident, change)


def terminate_routes_for_regroup(
    session: Session, incident: Incident, *, observed_at: datetime
) -> None:
    routes = session.exec(
        select(NotificationRoute).where(
            NotificationRoute.incident_id == incident.id,
            NotificationRoute.status == "ACTIVE",
        )
    ).all()
    for route in routes:
        route.status = "TERMINATED"
        route.termination_reason = "REGROUP"
        route.closed_at = observed_at
        _cancel_pending_route_deliveries(session, route, "REGROUP")
        session.add(route)
    session.flush()


def plan_due_reminders(session: Session, *, now: datetime) -> list[NotificationDelivery]:
    """Create at most one reminder slot per due route; offline slots are not replayed."""
    created: list[NotificationDelivery] = []
    routes = session.exec(
        select(NotificationRoute).where(
            NotificationRoute.status == "ACTIVE",
            NotificationRoute.repeat_interval_seconds > 0,
            NotificationRoute.next_reminder_at.is_not(None),
            NotificationRoute.next_reminder_at <= now,
        )
    ).all()
    for route in routes:
        incident = session.get(Incident, route.incident_id)
        if (
            incident is None
            or incident.superseded_by_incident_id is not None
            or incident.source_state != "firing"
            or incident.freshness_state == "STALE"
            or incident.handling_state != "NEW"
        ):
            continue
        outstanding = session.exec(
            select(NotificationDelivery.id).where(
                NotificationDelivery.route_id == route.id,
                NotificationDelivery.event_type == "REMINDER",
                NotificationDelivery.state.in_(["PENDING", "IN_FLIGHT", "RETRY_WAIT"]),
            )
        ).first()
        if outstanding is not None:
            continue
        targets = session.exec(
            select(NotificationRouteTarget).where(
                NotificationRouteTarget.route_id == route.id,
                NotificationRouteTarget.opened_success_at.is_not(None),
            )
        ).all()
        if not targets:
            continue
        route.repeat_slot += 1
        # Arm the next slot at planning time, not at delivery time. Waiting for a
        # successful send left a route whose reminder reached a terminal state
        # still due, so the next worker tick opened another slot immediately --
        # a permanently failing reminder became a tick-rate resend loop that
        # ignored both the repeat interval and the per-delivery attempt cap.
        # Counting from `now` is also what keeps an offline gap to one catch-up.
        route.next_reminder_at = now + timedelta(
            seconds=route.repeat_interval_seconds
        )
        session.add(route)
        session.flush()
        change = IncidentChange(
            incident_id=incident.id,
            occurrence_no=incident.occurrence_no,
            previous_source_state=incident.source_state,
            current_source_state=incident.source_state,
            previous_severity=incident.severity,
            current_severity=incident.severity,
            previous_handling_state=incident.handling_state,
            current_handling_state=incident.handling_state,
            change_version=incident.change_version,
            origin="REMINDER",
            observed_at=now,
        )
        for target in targets:
            created.append(
                _create_delivery(
                    session,
                    incident=incident,
                    route=route,
                    target=target,
                    event_type="REMINDER",
                    change=change,
                    repeat_slot=route.repeat_slot,
                )
            )
    session.flush()
    return created
