"""Incident endpoints: list and detail."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlmodel import Session, select

from app.db import get_session
from app.models import AggregationRule, Alert, Incident, IncidentAudit
from app.schemas import (
    AlertOut,
    HandlingAuditOut,
    HandlingUpdate,
    IncidentDetail,
    IncidentSummary,
)
from app.services.grouping import severity_rank
from app.services.handling import InvalidHandlingTransition, change_handling_state
from app.services.notification_planner import plan_handling_change
from app.services.occurrence_history import sync_open_conclusion
from app.services.alert_types import is_watchdog_incident
from app.services.source_scope import (
    has_event_sources,
    source_name_map,
    visible_source_ids,
)

router = APIRouter()


def _alert_out(alert: Alert) -> AlertOut:
    payload = alert.model_dump()
    payload["fingerprint"] = alert.upstream_fingerprint or alert.fingerprint
    return AlertOut.model_validate(payload)


def _aggregation_metadata(
    incident: Incident, rules: dict[int, AggregationRule]
) -> tuple[int | None, str | None, str]:
    """Read the stored grouping decision instead of re-deriving it.

    This used to parse the rule id back out of `group_key` and sniff the
    human-readable `grouping_explanation` for "missing_labels=". Both facts have
    their own columns -- `aggregation_rule_id` is even indexed -- and migrations
    5/7 build `group_key` *from* those columns, so the columns are the source of
    truth. Re-deriving made the key format and an explanation sentence into
    implicit API contract: rewording either would have flipped the status.
    """
    if incident.aggregation_rule_id is None:
        return None, None, "unmatched"
    rule = rules.get(incident.aggregation_rule_id)
    status = "missing_labels" if incident.missing_group_labels else "matched"
    return incident.aggregation_rule_id, rule.name if rule is not None else None, status


def _member_counts(session: Session, source_ids: list[str]) -> dict[int, int]:
    """Members per Incident, counted in SQL.

    This list is polled every 15s, so materialising every Alert row just to
    count them made the endpoint scale with the 30-day retention window.
    """
    rows = session.exec(
        select(Alert.incident_id, func.count(Alert.id))
        .where(Alert.source_id.in_(source_ids), Alert.incident_id.is_not(None))
        .group_by(Alert.incident_id)
    ).all()
    return {int(incident_id): int(count) for incident_id, count in rows}


@router.get("/incidents", response_model=list[IncidentSummary])
def list_incidents(
    session: Session = Depends(get_session),
    state: str | None = Query(default=None, description="Filter by source_state"),
    severity: str | None = Query(default=None, description="Filter by severity"),
    source_ids: list[str] | None = Query(default=None, description="Comma separated or repeated source IDs"),
    include_archived: bool = Query(default=False),
) -> list[IncidentSummary]:
    visible_ids = visible_source_ids(
        session,
        requested=source_ids,
        include_archived=include_archived,
    )
    incidents = [
        incident
        for incident in session.exec(
            select(Incident).where(Incident.source_id.in_(visible_ids))
        ).all()
        if not is_watchdog_incident(incident)
    ]
    counts = _member_counts(session, visible_ids)
    names = source_name_map(session)
    rules = {
        rule.id: rule
        for rule in session.exec(select(AggregationRule)).all()
        if rule.id is not None
    }

    if state:
        incidents = [i for i in incidents if i.source_state == state]
    if severity:
        incidents = [i for i in incidents if i.severity == severity]

    # Sort: most severe first, then most recently updated.
    incidents.sort(key=lambda i: (severity_rank(i.severity), i.updated_at), reverse=True)

    result: list[IncidentSummary] = []
    for i in incidents:
        if i.id is None:
            continue
        rule_id, rule_name, aggregation_status = _aggregation_metadata(i, rules)
        result.append(IncidentSummary(
            id=i.id,
            source_id=i.source_id,
            source_name=names.get(i.source_id),
            title=i.title,
            severity=i.severity,
            source_state=i.source_state,
            freshness_state=i.freshness_state,
            handling_state=i.handling_state,
            member_count=counts.get(i.id, 0),
            updated_at=i.updated_at,
            aggregation_rule_id=rule_id,
            aggregation_rule_name=rule_name,
            aggregation_status=aggregation_status,
        ))
    return result


@router.get("/incidents/{incident_id}", response_model=IncidentDetail)
def get_incident(
    incident_id: int, session: Session = Depends(get_session)
) -> IncidentDetail:
    incident = session.get(Incident, incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    if has_event_sources(session):
        if incident.source_id not in visible_source_ids(
            session, requested=[incident.source_id], include_archived=True
        ):
            raise HTTPException(status_code=404, detail="Incident not found")
    elif incident.source_id not in visible_source_ids(session):
        raise HTTPException(status_code=404, detail="Incident not found")

    members = session.exec(
        select(Alert).where(Alert.incident_id == incident_id)
    ).all()
    members = sorted(
        members,
        key=lambda a: (severity_rank(a.severity), a.last_seen_at),
        reverse=True,
    )
    handling_history = session.exec(
        select(IncidentAudit)
        .where(IncidentAudit.incident_id == incident_id)
        .order_by(IncidentAudit.created_at, IncidentAudit.id)
    ).all()
    rules = {
        rule.id: rule
        for rule in session.exec(select(AggregationRule)).all()
        if rule.id is not None
    }
    rule_id, rule_name, aggregation_status = _aggregation_metadata(incident, rules)
    names = source_name_map(session)

    return IncidentDetail(
        id=incident.id,
        source_id=incident.source_id,
        source_name=names.get(incident.source_id),
        title=incident.title,
        severity=incident.severity,
        source_state=incident.source_state,
        freshness_state=incident.freshness_state,
        handling_state=incident.handling_state,
        member_count=len(members),
        updated_at=incident.updated_at,
        aggregation_rule_id=rule_id,
        aggregation_rule_name=rule_name,
        aggregation_status=aggregation_status,
        group_key=incident.group_key,
        grouping_explanation=incident.grouping_explanation,
        policy_version=incident.policy_version,
        created_at=incident.created_at,
        members=[_alert_out(a) for a in members],
        handling_history=[
            HandlingAuditOut.model_validate(item.model_dump()) for item in handling_history
        ],
    )


@router.patch("/incidents/{incident_id}/handling", response_model=HandlingAuditOut)
def update_incident_handling(
    incident_id: int,
    payload: HandlingUpdate,
    session: Session = Depends(get_session),
) -> HandlingAuditOut:
    incident = session.get(Incident, incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    if has_event_sources(session):
        if incident.source_id not in visible_source_ids(
            session, requested=[incident.source_id], include_archived=True
        ):
            raise HTTPException(status_code=404, detail="Incident not found")
    elif incident.source_id not in visible_source_ids(session):
        raise HTTPException(status_code=404, detail="Incident not found")
    try:
        previous_handling_state = incident.handling_state
        audit = change_handling_state(
            session,
            incident,
            to_state=payload.state,
            reason=payload.reason,
            actor=payload.actor,
        )
        plan_handling_change(
            session,
            incident,
            previous_handling_state=previous_handling_state,
            observed_at=audit.created_at,
        )
        # F26 / D16: if this group's current occurrence has already been sealed,
        # the verdict the user just gave belongs on that record. Recovery never
        # settles handling (CAP-04.9), so without this the conclusion filter on
        # the history page would answer "unsettled" for every record forever.
        # Only the record matching the Incident's *current* occurrence_no can be
        # reached, so an occurrence that has already recurred stays closed.
        sync_open_conclusion(session, incident)
    except InvalidHandlingTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    try:
        session.commit()
        session.refresh(audit)
    except Exception:
        session.rollback()
        raise
    return HandlingAuditOut.model_validate(audit.model_dump())
