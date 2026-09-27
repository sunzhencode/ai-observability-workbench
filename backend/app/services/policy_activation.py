"""Short-lived policy activation confirmation bound to the exact Incident set."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlmodel import Session, select

from app.models import (
    Alert,
    Incident,
    NotificationChannel,
    NotificationPolicyRevision,
    NotificationRoute,
)
from app.services.notification_planner import plan_explicit_activation
from app.services.notification_policies import (
    activate_policy,
    active_policy_candidates,
    choose_policy,
    incident_route_context,
    policy_candidate,
)


class ActivationTokenError(ValueError):
    pass


@dataclass(frozen=True)
class ActivationPreparation:
    token: str
    eligible_incident_count: int
    eligible_incident_ids: list[int]
    expires_at_epoch: int


def _ids_hash(ids: list[int]) -> str:
    raw = ",".join(str(item) for item in sorted(ids))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ActivationTokenService:
    def __init__(self, key: bytes | None = None, ttl_seconds: int = 300) -> None:
        self.key = key or secrets.token_bytes(32)
        self.ttl_seconds = min(max(int(ttl_seconds), 30), 900)

    def issue(
        self,
        *,
        revision: NotificationPolicyRevision,
        notify_existing: bool,
        incident_ids: list[int],
        now_epoch: int | None = None,
    ) -> tuple[str, int]:
        now = int(time.time()) if now_epoch is None else int(now_epoch)
        expires = now + self.ttl_seconds
        payload = {
            "revision_id": revision.id,
            "version": revision.version,
            "notify_existing": bool(notify_existing),
            "ids_hash": _ids_hash(incident_ids),
            "exp": expires,
        }
        encoded = base64.urlsafe_b64encode(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).decode("ascii").rstrip("=")
        signature = base64.urlsafe_b64encode(
            hmac.new(self.key, encoded.encode("ascii"), hashlib.sha256).digest()
        ).decode("ascii").rstrip("=")
        return f"{encoded}.{signature}", expires

    def verify(
        self,
        token: str,
        *,
        revision: NotificationPolicyRevision,
        notify_existing: bool,
        incident_ids: list[int],
        now_epoch: int | None = None,
    ) -> None:
        try:
            encoded, signature = token.split(".", 1)
            expected = base64.urlsafe_b64encode(
                hmac.new(self.key, encoded.encode("ascii"), hashlib.sha256).digest()
            ).decode("ascii").rstrip("=")
            if not hmac.compare_digest(signature, expected):
                raise ActivationTokenError("activation token is invalid")
            padded = encoded + "=" * (-len(encoded) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        except ActivationTokenError:
            raise
        except Exception as exc:
            raise ActivationTokenError("activation token is invalid") from exc
        now = int(time.time()) if now_epoch is None else int(now_epoch)
        if int(payload.get("exp", 0)) < now:
            raise ActivationTokenError("activation token has expired")
        expected_payload = {
            "revision_id": revision.id,
            "version": revision.version,
            "notify_existing": bool(notify_existing),
            "ids_hash": _ids_hash(incident_ids),
        }
        for key, value in expected_payload.items():
            if payload.get(key) != value:
                raise ActivationTokenError("activation set changed; prepare again")


activation_tokens = ActivationTokenService()


def eligible_existing_incidents(
    session: Session,
    revision: NotificationPolicyRevision,
    *,
    source_id: str | None = None,
) -> list[Incident]:
    candidate = policy_candidate(session, revision)
    active = [
        item
        for item in active_policy_candidates(session)
        if item.logical_id != candidate.logical_id
    ]
    combined = active + [candidate]
    enabled_channels = 0
    for channel_id in candidate.channel_ids:
        channel = session.get(NotificationChannel, channel_id)
        if (
            channel is not None
            and channel.state == "ENABLED"
            and channel.active_revision_id is not None
        ):
            enabled_channels += 1
    if enabled_channels == 0:
        return []
    eligible: list[Incident] = []
    statement = select(Incident).where(Incident.source_state == "firing")
    if source_id is not None:
        statement = statement.where(Incident.source_id == source_id)
    for incident in session.exec(statement).all():
        if incident.id is None or incident.handling_state in {
            "CLOSED",
            "FALSE_POSITIVE",
        }:
            continue
        if session.exec(
            select(NotificationRoute.id).where(
                NotificationRoute.incident_id == incident.id,
                NotificationRoute.occurrence_no == incident.occurrence_no,
            )
        ).first() is not None:
            continue
        has_live_member = session.exec(
            select(Alert.id).where(
                Alert.incident_id == incident.id,
                Alert.origin == "live",
            )
        ).first()
        if has_live_member is None:
            continue
        winner = choose_policy(incident_route_context(incident), combined)
        if winner is not None and winner.id == candidate.id:
            eligible.append(incident)
    return sorted(eligible, key=lambda item: int(item.id or 0))


def prepare_activation(
    session: Session,
    revision_id: int,
    *,
    expected_version: int,
    notify_existing: bool,
    source_id: str | None = None,
    token_service: ActivationTokenService = activation_tokens,
) -> ActivationPreparation:
    revision = session.get(NotificationPolicyRevision, revision_id)
    if revision is None or revision.state != "DRAFT":
        raise LookupError("draft policy revision not found")
    if revision.version != expected_version:
        raise FileExistsError("policy revision conflict")
    incidents = (
        eligible_existing_incidents(session, revision, source_id=source_id)
        if notify_existing
        else []
    )
    ids = [int(item.id) for item in incidents if item.id is not None]
    token, expires = token_service.issue(
        revision=revision,
        notify_existing=notify_existing,
        incident_ids=ids,
    )
    return ActivationPreparation(token, len(ids), ids, expires)


def confirm_activation(
    session: Session,
    revision_id: int,
    *,
    expected_version: int,
    notify_existing: bool,
    token: str,
    source_id: str | None = None,
    token_service: ActivationTokenService = activation_tokens,
    observed_at: datetime | None = None,
) -> NotificationPolicyRevision:
    revision = session.get(NotificationPolicyRevision, revision_id)
    if revision is None or revision.state != "DRAFT":
        raise LookupError("draft policy revision not found")
    if revision.version != expected_version:
        raise FileExistsError("policy revision conflict")
    incidents = (
        eligible_existing_incidents(session, revision, source_id=source_id)
        if notify_existing
        else []
    )
    ids = [int(item.id) for item in incidents if item.id is not None]
    token_service.verify(
        token,
        revision=revision,
        notify_existing=notify_existing,
        incident_ids=ids,
    )
    activated = activate_policy(
        session, revision.id, expected_version=expected_version
    )
    if notify_existing:
        now = observed_at or datetime.now(timezone.utc)
        for incident in incidents:
            plan_explicit_activation(session, incident, observed_at=now)
    session.flush()
    return activated
