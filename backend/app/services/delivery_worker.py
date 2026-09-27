"""Lease-based, bounded notification Outbox worker."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol
from uuid import uuid4

from sqlalchemy import Engine
from sqlmodel import Session, select

from app.crypto import SecretError
from app.models import (
    Incident,
    NotificationAttempt,
    NotificationDelivery,
    NotificationRoute,
    NotificationRouteTarget,
    RuntimeSetting,
)
from app.providers.feishu import FeishuConfig, ProviderResult
from app.providers.registry import (
    FEISHU_CUSTOM_BOT,
    UnsupportedProviderKind,
    get_provider,
)
from app.services.notification_channels import resolve_active_channel_config
from app.services.notification_renderer import (
    PayloadTooLargeError,
    render_feishu_payload,
)

RETRY_DELAYS = (60, 300, 900, 3600)
MAX_ATTEMPTS = 5


class NotificationProvider(Protocol):
    async def send(
        self, payload: dict[str, Any], config: FeishuConfig, **kwargs: Any
    ) -> ProviderResult: ...


@dataclass(frozen=True)
class ClaimedDelivery:
    delivery_id: int
    lease_token: str


@dataclass(frozen=True)
class PreparedSend:
    delivery_id: int
    lease_token: str
    attempt_id: int
    payload: dict[str, Any]
    config: Any
    # Which implementation sends this. Read from the route snapshot rather than
    # the channel's current revision: a route locks its destination when the
    # occurrence is first routed, and that includes how it gets there.
    provider: str = FEISHU_CUSTOM_BOT


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def retry_delay_seconds(event_key: str, attempt_count: int) -> int:
    index = min(max(attempt_count - 1, 0), len(RETRY_DELAYS) - 1)
    base = RETRY_DELAYS[index]
    jitter_window = max(1, base // 10)
    jitter = int(hashlib.sha256(event_key.encode("utf-8")).hexdigest()[:8], 16)
    return base + jitter % jitter_window


def _expire_stale_leases(session: Session, now: datetime) -> None:
    expired = session.exec(
        select(NotificationDelivery).where(
            NotificationDelivery.state == "IN_FLIGHT",
            NotificationDelivery.lease_expires_at.is_not(None),
            NotificationDelivery.lease_expires_at <= now,
        )
    ).all()
    for delivery in expired:
        attempt = session.exec(
            select(NotificationAttempt)
            .where(
                NotificationAttempt.delivery_id == delivery.id,
                NotificationAttempt.finished_at.is_(None),
            )
            .order_by(NotificationAttempt.attempt_no.desc())
        ).first()
        if attempt is not None:
            attempt.finished_at = now
            attempt.outcome = "AMBIGUOUS"
            attempt.error_code = "LEASE_EXPIRED"
            attempt.error_summary = "worker lease expired before result persistence"
            session.add(attempt)
        delivery.state = "RETRY_WAIT"
        delivery.next_attempt_at = now
        delivery.lease_token = None
        delivery.lease_expires_at = None
        delivery.updated_at = now
        session.add(delivery)
    session.flush()


def claim_deliveries(
    session: Session,
    *,
    now: datetime,
    batch_size: int = 20,
    lease_seconds: int = 60,
) -> list[ClaimedDelivery]:
    _expire_stale_leases(session, now)
    limit = min(max(int(batch_size), 1), 100)
    candidates = session.exec(
        select(NotificationDelivery)
        .where(
            NotificationDelivery.state.in_(["PENDING", "RETRY_WAIT"]),
            NotificationDelivery.next_attempt_at <= now,
        )
        .order_by(
            NotificationDelivery.next_attempt_at,
            NotificationDelivery.id,
        )
        .limit(limit)
    ).all()
    claimed: list[ClaimedDelivery] = []
    for delivery in candidates:
        token = str(uuid4())
        delivery.state = "IN_FLIGHT"
        delivery.lease_token = token
        delivery.lease_expires_at = now + timedelta(
            seconds=min(max(int(lease_seconds), 10), 300)
        )
        delivery.updated_at = now
        session.add(delivery)
        session.flush()
        claimed.append(ClaimedDelivery(int(delivery.id), token))
    return claimed


def _attempt_times_for_channel(
    session: Session, channel_id: int, since: datetime
) -> list[datetime]:
    times: list[datetime] = []
    attempts = session.exec(
        select(NotificationAttempt).where(NotificationAttempt.started_at >= since)
    ).all()
    for attempt in attempts:
        delivery = session.get(NotificationDelivery, attempt.delivery_id)
        target = (
            session.get(NotificationRouteTarget, delivery.route_target_id)
            if delivery is not None
            else None
        )
        if target is not None and target.channel_id == channel_id:
            times.append(_aware(attempt.started_at))
    return sorted(times)


def next_rate_limit_permit(
    session: Session, *, channel_id: int, now: datetime
) -> datetime:
    minute_times = _attempt_times_for_channel(
        session, channel_id, now - timedelta(seconds=60)
    )
    second_times = [item for item in minute_times if item > now - timedelta(seconds=1)]
    candidates = [now]
    if len(second_times) >= 4:
        candidates.append(second_times[-4] + timedelta(seconds=1))
    if len(minute_times) >= 90:
        candidates.append(minute_times[-90] + timedelta(seconds=60))
    return max(candidates)


def _cancel_or_suppress_before_send(
    session: Session,
    delivery: NotificationDelivery,
    route: NotificationRoute,
    target: NotificationRouteTarget,
    incident: Incident,
    now: datetime,
) -> bool:
    if route.status == "TERMINATED":
        delivery.state = "CANCELED"
        delivery.suppression_reason = route.termination_reason or "ROUTE_TERMINATED"
    elif incident.superseded_by_incident_id is not None:
        delivery.state = "CANCELED"
        delivery.suppression_reason = "INCIDENT_SUPERSEDED"
    elif incident.handling_state in {"CLOSED", "FALSE_POSITIVE"}:
        delivery.state = "CANCELED"
        delivery.suppression_reason = f"HANDLING_{incident.handling_state}"
    elif delivery.event_type != "RECOVERED" and incident.freshness_state == "STALE":
        delivery.state = "CANCELED"
        delivery.suppression_reason = "SOURCE_STALE"
    elif delivery.event_type != "RECOVERED" and incident.source_state != "firing":
        delivery.state = "CANCELED"
        delivery.suppression_reason = "INCIDENT_NOT_FIRING"
    else:
        return False
    delivery.lease_token = None
    delivery.lease_expires_at = None
    delivery.updated_at = now
    session.add(delivery)
    return True


def _workbench_url(session: Session) -> str:
    row = session.get(RuntimeSetting, "workbench_url")
    return str(row.value_json or "") if row else ""


def prepare_send(
    session: Session,
    claim: ClaimedDelivery,
    *,
    now: datetime,
) -> PreparedSend | None:
    delivery = session.get(NotificationDelivery, claim.delivery_id)
    if (
        delivery is None
        or delivery.state != "IN_FLIGHT"
        or delivery.lease_token != claim.lease_token
    ):
        return None
    route = session.get(NotificationRoute, delivery.route_id)
    target = session.get(NotificationRouteTarget, delivery.route_target_id)
    incident = session.get(Incident, delivery.incident_id)
    if route is None or target is None or incident is None:
        delivery.state = "PERMANENT_FAILED"
        delivery.suppression_reason = "BROKEN_REFERENCE"
        delivery.lease_token = None
        delivery.lease_expires_at = None
        session.add(delivery)
        session.flush()
        return None
    if _cancel_or_suppress_before_send(
        session, delivery, route, target, incident, now
    ):
        session.flush()
        return None
    permit = next_rate_limit_permit(
        session, channel_id=target.channel_id, now=now
    )
    if permit > now:
        delivery.state = "PENDING"
        delivery.next_attempt_at = permit
        delivery.lease_token = None
        delivery.lease_expires_at = None
        delivery.updated_at = now
        session.add(delivery)
        session.flush()
        return None
    try:
        _revision, config = resolve_active_channel_config(
            session, target.channel_id
        )
    except SecretError:
        delivery.state = "RETRY_WAIT"
        delivery.next_attempt_at = now + timedelta(seconds=60)
        delivery.lease_token = None
        delivery.lease_expires_at = None
        delivery.suppression_reason = "SECRET_UNAVAILABLE"
        delivery.updated_at = now
        session.add(delivery)
        session.flush()
        return None
    except ValueError:
        delivery.state = "SUPPRESSED"
        delivery.suppression_reason = "CHANNEL_DISABLED"
        delivery.lease_token = None
        delivery.lease_expires_at = None
        delivery.updated_at = now
        session.add(delivery)
        session.flush()
        return None
    try:
        payload = render_feishu_payload(
            delivery,
            target,
            required_keyword=config.required_keyword,
            workbench_url=_workbench_url(session),
        )
    except PayloadTooLargeError:
        delivery.attempt_count += 1
        attempt = NotificationAttempt(
            delivery_id=delivery.id,
            attempt_no=delivery.attempt_count,
            trigger=delivery.next_attempt_trigger,
            started_at=now,
            finished_at=now,
            outcome="PERMANENT_FAILURE",
            error_code="PAYLOAD_TOO_LARGE",
            error_summary="rendered notification exceeds application limit",
        )
        delivery.state = "PERMANENT_FAILED"
        delivery.next_attempt_trigger = "AUTO"
        delivery.lease_token = None
        delivery.lease_expires_at = None
        delivery.updated_at = now
        session.add(attempt)
        session.add(delivery)
        session.flush()
        return None
    delivery.attempt_count += 1
    attempt = NotificationAttempt(
        delivery_id=delivery.id,
        attempt_no=delivery.attempt_count,
        trigger=delivery.next_attempt_trigger,
        started_at=now,
    )
    delivery.next_attempt_trigger = "AUTO"
    delivery.updated_at = now
    session.add(attempt)
    session.add(delivery)
    session.flush()
    session.refresh(attempt)
    return PreparedSend(
        delivery_id=delivery.id,
        lease_token=claim.lease_token,
        attempt_id=attempt.id,
        payload=payload,
        config=config,
        provider=target.provider or FEISHU_CUSTOM_BOT,
    )


def finish_send(
    session: Session,
    prepared: PreparedSend,
    result: ProviderResult,
    *,
    now: datetime,
) -> bool:
    delivery = session.get(NotificationDelivery, prepared.delivery_id)
    attempt = session.get(NotificationAttempt, prepared.attempt_id)
    if (
        delivery is None
        or attempt is None
        or delivery.state != "IN_FLIGHT"
        or delivery.lease_token != prepared.lease_token
    ):
        return False
    attempt.finished_at = now
    attempt.http_status = result.http_status
    attempt.provider_request_id = result.request_id
    attempt.error_code = None if result.ok else result.code[:128]
    attempt.error_summary = (
        None if result.ok else (result.error_summary or "provider send failed")[:512]
    )
    route = session.get(NotificationRoute, delivery.route_id)
    target = session.get(NotificationRouteTarget, delivery.route_target_id)
    incident = session.get(Incident, delivery.incident_id)
    if result.ok:
        attempt.outcome = "SUCCESS"
        delivery.state = "SUCCEEDED"
        delivery.succeeded_at = now
        if target is not None:
            if delivery.event_type == "FIRING_OPENED" and target.opened_success_at is None:
                target.opened_success_at = now
            target.last_success_at = now
            session.add(target)
        if route is not None:
            route.last_successful_event_at = now
            route.last_notified_severity = str(
                delivery.payload_snapshot_json.get("severity") or "unknown"
            )
            if (
                delivery.event_type != "RECOVERED"
                and route.repeat_interval_seconds > 0
                and incident is not None
                and incident.source_state == "firing"
                and incident.handling_state == "NEW"
            ):
                route.next_reminder_at = now + timedelta(
                    seconds=route.repeat_interval_seconds
                )
            elif delivery.event_type == "RECOVERED":
                route.next_reminder_at = None
            session.add(route)
    else:
        attempt.outcome = (
            "AMBIGUOUS"
            if result.code in {"TIMEOUT", "NETWORK"}
            else "TRANSIENT_FAILURE"
            if result.transient
            else "PERMANENT_FAILURE"
        )
        if result.transient and delivery.attempt_count < MAX_ATTEMPTS:
            delivery.state = "RETRY_WAIT"
            delivery.next_attempt_at = now + timedelta(
                seconds=retry_delay_seconds(
                    delivery.event_key, delivery.attempt_count
                )
            )
        else:
            delivery.state = "PERMANENT_FAILED"
    delivery.lease_token = None
    delivery.lease_expires_at = None
    delivery.updated_at = now
    session.add(attempt)
    session.add(delivery)
    session.flush()
    return True


class DeliveryWorker:
    def __init__(
        self,
        engine: Engine,
        provider: NotificationProvider | None = None,
        *,
        batch_size: int = 20,
        lease_seconds: int = 60,
        resolve_provider: Callable[[str], NotificationProvider] | None = None,
    ) -> None:
        self.engine = engine
        # `provider` pins one implementation for every delivery. Tests use it to
        # inject a fake; production leaves it unset so each delivery goes to the
        # implementation its route recorded.
        self.provider = provider
        self.resolve_provider = resolve_provider or get_provider
        self.batch_size = batch_size
        self.lease_seconds = lease_seconds

    def _provider_for(self, kind: str) -> NotificationProvider:
        if self.provider is not None:
            return self.provider
        return self.resolve_provider(kind)

    async def run_once(self, *, now: datetime | None = None) -> int:
        current = now or datetime.now(timezone.utc)
        with Session(self.engine) as session:
            claimed = claim_deliveries(
                session,
                now=current,
                batch_size=self.batch_size,
                lease_seconds=self.lease_seconds,
            )
            session.commit()
        sent = 0
        for claim in claimed:
            with Session(self.engine) as session:
                prepared = prepare_send(session, claim, now=current)
                session.commit()
            if prepared is None:
                continue
            try:
                provider = self._provider_for(prepared.provider)
            except UnsupportedProviderKind:
                # A channel naming a kind this build cannot send with is a
                # permanent failure, not something to retry forever.
                result = ProviderResult(
                    False,
                    "UNSUPPORTED_PROVIDER",
                    error_summary="channel provider is not available in this build",
                )
            else:
                result = await provider.send(
                    prepared.payload, prepared.config, purpose="DELIVERY"
                )
            with Session(self.engine) as session:
                if finish_send(session, prepared, result, now=current):
                    sent += 1
                session.commit()
        return sent
