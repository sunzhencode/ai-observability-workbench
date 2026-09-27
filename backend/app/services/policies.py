"""Grouping-policy validation, versioning, and atomic full regrouping."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session, func, select

from app.models import Alert, GroupingPolicy, Incident, IncidentAudit
from app.services.grouping import (
    DEFAULT_GROUP_BY,
    build_explanation,
    build_title,
    compute_group_key,
)
from app.services.lifecycle import recompute_incident_lifecycle
from app.services.alert_types import WATCHDOG_ALERTNAME

ALLOWED_GROUP_FIELDS = {"alertname", "cluster", "severity"}


def validate_group_by(group_by: list[str]) -> list[str]:
    if not group_by:
        raise ValueError("group_by must contain at least one field")
    if len(group_by) != len(set(group_by)):
        raise ValueError("group_by must not contain duplicate fields")
    unknown = set(group_by) - ALLOWED_GROUP_FIELDS
    if unknown:
        raise ValueError(f"unsupported group_by fields: {', '.join(sorted(unknown))}")
    return list(group_by)


def get_active_policy(session: Session) -> GroupingPolicy:
    policy = session.exec(
        select(GroupingPolicy)
        .where(GroupingPolicy.enabled == True)  # noqa: E712 - SQL expression
        .order_by(GroupingPolicy.version.desc())
    ).first()
    if policy is not None:
        return policy

    max_version = session.exec(select(func.max(GroupingPolicy.version))).one() or 0
    policy = GroupingPolicy(
        version=int(max_version) + 1,
        group_by=list(DEFAULT_GROUP_BY),
        enabled=True,
    )
    session.add(policy)
    session.commit()
    session.refresh(policy)
    return policy


def _alert_fields(alert: Alert) -> dict[str, Any]:
    return {
        "source_id": alert.source_id,
        "environment": alert.environment,
        "alertname": alert.alertname,
        "cluster": alert.cluster,
        "severity": alert.severity,
    }


def update_policy_and_regroup(
    session: Session,
    group_by: list[str],
    changed_at: datetime | None = None,
) -> GroupingPolicy:
    """Create a version and atomically reassign every stored Alert."""
    validated = validate_group_by(group_by)
    changed_at = changed_at or datetime.now(timezone.utc)
    current = get_active_policy(session)
    next_version = current.version + 1

    try:
        current.enabled = False
        session.add(current)
        policy = GroupingPolicy(
            version=next_version,
            group_by=validated,
            enabled=True,
            created_at=changed_at,
        )
        session.add(policy)
        session.flush()

        incidents_by_key = {
            incident.group_key: incident for incident in session.exec(select(Incident)).all()
        }
        representatives: dict[int, dict[str, Any]] = {}
        used_ids: set[int] = set()

        alerts = session.exec(select(Alert).order_by(Alert.fingerprint)).all()
        for alert in alerts:
            if alert.alertname == WATCHDOG_ALERTNAME:
                alert.incident_id = None
                session.add(alert)
                continue
            fields = _alert_fields(alert)
            group_key = compute_group_key(fields, validated)
            incident = incidents_by_key.get(group_key)
            if incident is None:
                incident = Incident(
                    source_id=alert.source_id,
                    environment=alert.environment,
                    group_key=group_key,
                    title=build_title(fields),
                    severity=alert.severity,
                    policy_version=policy.version,
                    grouping_explanation=build_explanation(fields, validated),
                )
                session.add(incident)
                session.flush()
                incidents_by_key[group_key] = incident
            incident.policy_version = policy.version
            incident.grouping_explanation = build_explanation(fields, validated)
            session.add(incident)
            alert.incident_id = incident.id
            session.add(alert)
            if incident.id is not None:
                used_ids.add(incident.id)
                representatives.setdefault(incident.id, fields)

        session.flush()
        recompute_incident_lifecycle(session, changed_at)
        session.flush()

        for incident in session.exec(select(Incident)).all():
            if incident.id in used_ids:
                representative = representatives[incident.id]
                incident.title = build_title(representative)
                incident.policy_version = policy.version
                session.add(incident)
                continue
            audit = session.exec(
                select(IncidentAudit).where(IncidentAudit.incident_id == incident.id)
            ).first()
            if audit is None:
                session.delete(incident)
            else:
                incident.source_state = "recovered"
                incident.updated_at = changed_at
                session.add(incident)

        session.commit()
        session.refresh(policy)
        return policy
    except Exception:
        session.rollback()
        raise
