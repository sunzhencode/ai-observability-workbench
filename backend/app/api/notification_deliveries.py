"""Bounded notification delivery history and explicit manual retry APIs."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select

from app.config_schemas import (
    AttemptOut,
    DeliveryDetailOut,
    DeliveryOut,
    IncidentNotificationOut,
    ManualRetryOut,
)
from app.db import get_session
from app.models import (
    Incident,
    NotificationAttempt,
    NotificationDelivery,
    NotificationRoute,
    NotificationRouteTarget,
)
from app.services.source_identity import active_source_id
from app.services.source_scope import has_event_sources, visible_source_ids

router = APIRouter()


def _delivery_out(
    session: Session, delivery: NotificationDelivery
) -> DeliveryOut:
    target = session.get(NotificationRouteTarget, delivery.route_target_id)
    incident = session.get(Incident, delivery.incident_id)
    snapshot = dict(delivery.payload_snapshot_json or {})
    source_id = str(snapshot.get("source_id") or (incident.source_id if incident else "legacy"))
    raw_source_name = snapshot.get("source_name")
    source_name = str(raw_source_name) if raw_source_name else None
    return DeliveryOut(
        id=delivery.id,
        event_key=delivery.event_key,
        incident_id=delivery.incident_id,
        route_id=delivery.route_id,
        route_target_id=delivery.route_target_id,
        channel_id=target.channel_id if target else 0,
        channel_name=target.channel_name if target else "<missing>",
        source_id=source_id,
        source_name=source_name,
        event_type=delivery.event_type,
        state=delivery.state,
        attempt_count=delivery.attempt_count,
        scheduled_at=delivery.scheduled_at,
        next_attempt_at=delivery.next_attempt_at,
        suppression_reason=delivery.suppression_reason,
        created_at=delivery.created_at,
        updated_at=delivery.updated_at,
        succeeded_at=delivery.succeeded_at,
    )


@router.get("/notification-deliveries", response_model=list[DeliveryOut])
def list_deliveries(
    state: str | None = Query(default=None, max_length=64),
    event_type: str | None = Query(default=None, max_length=64),
    channel_id: int | None = Query(default=None, ge=1),
    limit: int = Query(default=100, ge=1, le=500),
    session: Session = Depends(get_session),
) -> list[DeliveryOut]:
    statement = select(NotificationDelivery).order_by(
        NotificationDelivery.created_at.desc(), NotificationDelivery.id.desc()
    )
    if state:
        statement = statement.where(NotificationDelivery.state == state)
    if event_type:
        statement = statement.where(NotificationDelivery.event_type == event_type)
    deliveries = session.exec(statement.limit(limit * 2 if channel_id else limit)).all()
    if channel_id is not None:
        deliveries = [
            item
            for item in deliveries
            if (
                (target := session.get(NotificationRouteTarget, item.route_target_id))
                is not None
                and target.channel_id == channel_id
            )
        ][:limit]
    return [_delivery_out(session, item) for item in deliveries]


@router.get(
    "/notification-deliveries/{delivery_id}", response_model=DeliveryDetailOut
)
def get_delivery(
    delivery_id: int, session: Session = Depends(get_session)
) -> DeliveryDetailOut:
    delivery = session.get(NotificationDelivery, delivery_id)
    if delivery is None:
        raise HTTPException(status_code=404, detail="delivery not found")
    base = _delivery_out(session, delivery).model_dump()
    attempts = session.exec(
        select(NotificationAttempt)
        .where(NotificationAttempt.delivery_id == delivery.id)
        .order_by(NotificationAttempt.attempt_no)
    ).all()
    return DeliveryDetailOut(
        **base,
        payload_snapshot=dict(delivery.payload_snapshot_json),
        attempts=[
            AttemptOut.model_validate(item.model_dump(exclude={"delivery_id"}))
            for item in attempts
        ],
    )


@router.post(
    "/notification-deliveries/{delivery_id}/retry", response_model=ManualRetryOut
)
def retry_delivery(
    delivery_id: int, session: Session = Depends(get_session)
) -> ManualRetryOut:
    delivery = session.get(NotificationDelivery, delivery_id)
    if delivery is None:
        raise HTTPException(status_code=404, detail="delivery not found")
    if delivery.state == "SUCCEEDED":
        raise HTTPException(status_code=409, detail="successful delivery cannot be retried")
    if delivery.state != "PERMANENT_FAILED":
        raise HTTPException(status_code=409, detail="delivery is not eligible for manual retry")
    route = session.get(NotificationRoute, delivery.route_id)
    incident = session.get(Incident, delivery.incident_id)
    if (
        route is None
        or incident is None
        or route.status == "TERMINATED"
        or incident.handling_state in {"CLOSED", "FALSE_POSITIVE"}
    ):
        raise HTTPException(status_code=409, detail="delivery lifecycle is no longer active")
    delivery.state = "PENDING"
    delivery.next_attempt_at = datetime.now(timezone.utc)
    delivery.next_attempt_trigger = "MANUAL"
    delivery.suppression_reason = None
    delivery.updated_at = datetime.now(timezone.utc)
    session.add(delivery)
    session.commit()
    session.refresh(delivery)
    return ManualRetryOut(
        delivery=_delivery_out(session, delivery),
        warning="manual retry may duplicate a message if the previous outcome was ambiguous",
    )


@router.get(
    "/incidents/{incident_id}/notification", response_model=IncidentNotificationOut
)
def get_incident_notification(
    incident_id: int, session: Session = Depends(get_session)
) -> IncidentNotificationOut:
    incident = session.get(Incident, incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="incident not found")
    if has_event_sources(session):
        visible_ids = visible_source_ids(
            session,
            requested=[incident.source_id],
            include_archived=True,
        )
        if incident.source_id not in visible_ids:
            raise HTTPException(status_code=404, detail="incident not found")
    elif incident.source_id != active_source_id():
        raise HTTPException(status_code=404, detail="incident not found")
    route = session.exec(
        select(NotificationRoute).where(
            NotificationRoute.incident_id == incident.id,
            NotificationRoute.occurrence_no == incident.occurrence_no,
        )
    ).first()
    if route is None:
        return IncidentNotificationOut(
            incident_id=incident.id,
            occurrence_no=incident.occurrence_no,
            route=None,
            targets=[],
            deliveries=[],
        )
    targets = session.exec(
        select(NotificationRouteTarget).where(
            NotificationRouteTarget.route_id == route.id
        )
    ).all()
    deliveries = session.exec(
        select(NotificationDelivery)
        .where(NotificationDelivery.route_id == route.id)
        .order_by(NotificationDelivery.created_at, NotificationDelivery.id)
    ).all()
    return IncidentNotificationOut(
        incident_id=incident.id,
        occurrence_no=incident.occurrence_no,
        route={
            "id": route.id,
            "status": route.status,
            "policy_name": route.policy_name,
            "policy_version": route.policy_version,
            "repeat_interval_seconds": route.repeat_interval_seconds,
            "next_reminder_at": route.next_reminder_at,
            "termination_reason": route.termination_reason,
        },
        targets=[
            {
                "id": item.id,
                "channel_id": item.channel_id,
                "channel_name": item.channel_name,
                "channel_version": item.channel_version,
                "opened_success_at": item.opened_success_at,
                "last_success_at": item.last_success_at,
            }
            for item in targets
        ],
        deliveries=[_delivery_out(session, item) for item in deliveries],
    )
