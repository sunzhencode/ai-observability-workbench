"""Notification configuration, routing, Outbox and delivery use cases."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import hmac
import json
import secrets
import time
from typing import Mapping, Protocol

from app.application.commands import ExternalOutcomeUnknown
from app.domains.notifications.models import (
    DeliveryState,
    IncidentNotificationFact,
    Matcher,
    NotificationChange,
    PolicyCandidate,
)
from app.domains.operations.jobs import JobPool, JobSpec


@dataclass(frozen=True, slots=True)
class SecretChange:
    action: str = "KEEP"
    value: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ChannelDraft:
    name: str
    provider: str
    config: Mapping[str, object]
    secrets: Mapping[str, SecretChange]


@dataclass(frozen=True, slots=True)
class ChannelView:
    id: str
    name: str
    provider: str
    enabled: bool
    revision_no: int
    revision_state: str
    config: Mapping[str, object]
    secret_configured: Mapping[str, bool]
    tested_at: datetime | None
    last_test_code: str | None


@dataclass(frozen=True, slots=True)
class ResolvedChannel:
    view: ChannelView
    secrets: Mapping[str, str] = field(repr=False)


@dataclass(frozen=True, slots=True)
class PolicyDraft:
    name: str
    priority: int
    matchers: tuple[Matcher, ...]
    repeat_interval_seconds: int
    channel_ids: tuple[str, ...]
    scope_mode: str = "ALL"
    source_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PolicyView:
    revision_id: int
    logical_id: str
    version: int
    name: str
    state: str
    priority: int
    matchers: tuple[Matcher, ...]
    repeat_interval_seconds: int
    channel_ids: tuple[str, ...]
    scope_mode: str
    source_ids: tuple[str, ...]

    def candidate(self) -> PolicyCandidate:
        return PolicyCandidate(
            revision_id=self.revision_id,
            logical_id=self.logical_id,
            name=self.name,
            version=self.version,
            priority=self.priority,
            matchers=self.matchers,
            scope_mode=self.scope_mode,
            source_ids=self.source_ids,
            channel_ids=self.channel_ids,
            repeat_interval_seconds=self.repeat_interval_seconds,
        )


@dataclass(frozen=True, slots=True)
class PolicyPreviewSample:
    incident_id: int
    winner_policy_id: int | None
    winner_policy_name: str | None


@dataclass(frozen=True, slots=True)
class PolicyPreview:
    direct_match_count: int
    shadowed_count: int
    final_match_count: int
    firing_match_count: int
    unrouted_count: int
    disabled_channel_count: int
    samples: tuple[PolicyPreviewSample, ...]


@dataclass(frozen=True, slots=True)
class ActivationPreparation:
    confirm_token: str
    eligible_incident_count: int
    expires_at_epoch: int


@dataclass(frozen=True, slots=True)
class AttemptView:
    id: int
    attempt_no: int
    trigger: str
    started_at: datetime
    finished_at: datetime | None
    outcome: str | None
    http_status: int | None
    provider_request_id: str | None
    error_code: str | None


@dataclass(frozen=True, slots=True)
class DeliveryView:
    id: int
    event_key: str
    incident_id: int
    occurrence_no: int
    route_id: int
    route_target_id: int
    channel_id: str
    channel_name: str
    provider: str
    event_type: str
    state: str
    attempt_count: int
    scheduled_at: datetime
    next_attempt_at: datetime
    suppression_reason: str | None
    payload_snapshot: Mapping[str, object]
    succeeded_at: datetime | None
    attempts: tuple[AttemptView, ...] = ()


@dataclass(frozen=True, slots=True)
class RouteView:
    id: int
    status: str
    policy_name: str
    policy_version: int
    repeat_interval_seconds: int
    next_reminder_at: datetime | None
    termination_reason: str | None


@dataclass(frozen=True, slots=True)
class RouteTargetView:
    id: int
    channel_id: str
    channel_name: str
    provider: str
    channel_revision_no: int
    opened_success_at: datetime | None
    last_success_at: datetime | None


@dataclass(frozen=True, slots=True)
class IncidentNotificationView:
    incident_id: int
    occurrence_no: int
    route: RouteView | None
    targets: tuple[RouteTargetView, ...]
    deliveries: tuple[DeliveryView, ...]


@dataclass(frozen=True, slots=True)
class ClaimedDelivery:
    delivery_id: int
    lease_token: str


@dataclass(frozen=True, slots=True)
class PreparedDelivery:
    delivery_id: int
    lease_token: str
    attempt_id: int
    provider: str
    payload: Mapping[str, object]
    config: Mapping[str, object]
    secrets: Mapping[str, str] = field(repr=False)


@dataclass(frozen=True, slots=True)
class ProviderResult:
    ok: bool
    code: str
    transient: bool = False
    http_status: int | None = None
    request_id: str | None = None


class NotificationStore(Protocol):
    def get_workbench_url(self) -> str: ...
    def save_workbench_url(self, url: str, *, now: datetime) -> str: ...
    def create_channel(self, draft: ChannelDraft, *, now: datetime) -> ChannelView: ...
    def update_channel(self, channel_id: str, draft: ChannelDraft, *, expected_revision: int, now: datetime) -> ChannelView: ...
    def list_channels(self) -> tuple[ChannelView, ...]: ...
    def load_channel(self, channel_id: str, *, require_draft: bool = False) -> ResolvedChannel: ...
    def record_channel_test(self, channel_id: str, *, expected_revision: int, result: ProviderResult, now: datetime) -> ChannelView: ...
    def activate_channel(self, channel_id: str, *, expected_revision: int, now: datetime) -> ChannelView: ...
    def set_channel_enabled(self, channel_id: str, *, expected_revision: int, enabled: bool, now: datetime) -> ChannelView: ...
    def create_policy(self, draft: PolicyDraft, *, now: datetime) -> PolicyView: ...
    def update_policy(self, logical_id: str, draft: PolicyDraft, *, expected_version: int, now: datetime) -> PolicyView: ...
    def list_policies(self) -> tuple[PolicyView, ...]: ...
    def preview_policy(self, candidate: PolicyCandidate) -> PolicyPreview: ...
    def eligible_existing_incident_ids(self, revision_id: int) -> tuple[int, ...]: ...
    def get_policy(self, revision_id: int) -> PolicyView: ...
    def activate_policy(self, revision_id: int, *, expected_version: int, notify_existing: bool, now: datetime) -> PolicyView: ...
    def disable_policy(self, revision_id: int, *, expected_version: int, now: datetime) -> PolicyView: ...
    def plan_change(self, fact: IncidentNotificationFact, change: NotificationChange) -> int | None: ...
    def plan_due_reminders(self, *, now: datetime) -> int: ...
    def has_due_deliveries(self, *, now: datetime) -> bool: ...
    def has_due_reminders(self, *, now: datetime) -> bool: ...
    def claim_deliveries(self, *, now: datetime, batch_size: int, lease_seconds: int) -> tuple[ClaimedDelivery, ...]: ...
    def prepare_delivery(self, claim: ClaimedDelivery, *, now: datetime) -> PreparedDelivery | None: ...
    def finish_delivery(self, prepared: PreparedDelivery, result: ProviderResult, *, now: datetime) -> bool: ...
    def list_deliveries(self, *, state: str | None = None, event_type: str | None = None, channel_id: str | None = None, incident_id: int | None = None, before_id: int | None = None, limit: int = 100) -> tuple[DeliveryView, ...]: ...
    def get_delivery(self, delivery_id: int) -> DeliveryView: ...
    def retry_delivery(self, delivery_id: int, *, now: datetime) -> DeliveryView: ...
    def get_incident_notification(self, incident_id: int) -> IncidentNotificationView: ...


class NotificationProvider(Protocol):
    async def send(
        self,
        payload: Mapping[str, object],
        config: Mapping[str, object],
        secrets: Mapping[str, str],
        *,
        purpose: str,
    ) -> ProviderResult: ...


class NotificationProviderFactory(Protocol):
    def __call__(self, provider: str) -> NotificationProvider: ...


class TestNotificationChannel:
    __test__ = False

    def __init__(self, store: NotificationStore, providers: NotificationProviderFactory) -> None:
        self._store = store
        self._providers = providers

    async def execute(
        self, channel_id: str, *, expected_revision: int, now: datetime
    ) -> ChannelView:
        resolved = self._store.load_channel(channel_id, require_draft=True)
        if resolved.view.revision_no != expected_revision:
            raise FileExistsError("NOTIFICATION_CHANNEL_REVISION_CONFLICT")
        payload: dict[str, object] = {
            "schema_version": 1,
            "event_type": "CHANNEL_TEST",
            "subject": "通知通道测试",
            "text": "这是一条由操作者显式发起的通知通道测试消息。",
        }
        result = await self._providers(resolved.view.provider).send(
            payload,
            resolved.view.config,
            resolved.secrets,
            purpose="CHANNEL_TEST",
        )
        if result.code in {"TIMEOUT", "NETWORK"}:
            raise ExternalOutcomeUnknown(result.code)
        return self._store.record_channel_test(
            channel_id,
            expected_revision=expected_revision,
            result=result,
            now=now,
        )


class PlanNotification:
    def __init__(self, store: NotificationStore) -> None:
        self._store = store

    def execute(
        self, fact: IncidentNotificationFact, change: NotificationChange
    ) -> int | None:
        return self._store.plan_change(fact, change)


class DeliverNotifications:
    def __init__(
        self,
        store: NotificationStore,
        providers: NotificationProviderFactory,
        *,
        batch_size: int = 20,
        lease_seconds: int = 60,
    ) -> None:
        self._store = store
        self._providers = providers
        self._batch_size = min(max(batch_size, 1), 100)
        self._lease_seconds = min(max(lease_seconds, 10), 300)

    async def execute(self, *, now: datetime) -> int:
        claims = self._store.claim_deliveries(
            now=now,
            batch_size=self._batch_size,
            lease_seconds=self._lease_seconds,
        )
        sent = 0
        for claim in claims:
            prepared = self._store.prepare_delivery(claim, now=now)
            if prepared is None:
                continue
            try:
                provider = self._providers(prepared.provider)
                result = await provider.send(
                    prepared.payload,
                    prepared.config,
                    prepared.secrets,
                    purpose="DELIVERY",
                )
            except Exception:
                result = ProviderResult(False, "PROVIDER_ADAPTER_UNAVAILABLE")
            if self._store.finish_delivery(prepared, result, now=now):
                sent += 1
        return sent


class NotificationJobPlanner:
    def __init__(self, store: NotificationStore) -> None:
        self._store = store

    def plan(self, *, now: datetime) -> tuple[JobSpec, ...]:
        if not (
            self._store.has_due_deliveries(now=now)
            or self._store.has_due_reminders(now=now)
        ):
            return ()
        slot = int(now.timestamp()) // 5
        return (
            JobSpec(
                kind="notification.dispatch",
                pool=JobPool.NOTIFICATION,
                subject_type="notification_outbox",
                subject_id="due",
                payload={},
                payload_revision=1,
                idempotency_key=f"notification-dispatch:{slot}",
            ),
        )


class ActivationTokenError(ValueError):
    pass


def _ids_hash(ids: tuple[int, ...]) -> str:
    return hashlib.sha256(",".join(str(item) for item in ids).encode("utf-8")).hexdigest()


class ActivationTokenService:
    def __init__(self, key: bytes | None = None, ttl_seconds: int = 300) -> None:
        self._key = key or secrets.token_bytes(32)
        self._ttl = min(max(ttl_seconds, 30), 900)

    def issue(
        self,
        *,
        revision_id: int,
        version: int,
        notify_existing: bool,
        incident_ids: tuple[int, ...],
        now_epoch: int | None = None,
    ) -> tuple[str, int]:
        now = int(time.time()) if now_epoch is None else now_epoch
        expires = now + self._ttl
        payload = {
            "revision_id": revision_id,
            "version": version,
            "notify_existing": notify_existing,
            "ids_hash": _ids_hash(incident_ids),
            "exp": expires,
        }
        encoded = base64.urlsafe_b64encode(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).decode("ascii").rstrip("=")
        signature = base64.urlsafe_b64encode(
            hmac.new(self._key, encoded.encode("ascii"), hashlib.sha256).digest()
        ).decode("ascii").rstrip("=")
        return f"{encoded}.{signature}", expires

    def verify(
        self,
        token: str,
        *,
        revision_id: int,
        version: int,
        notify_existing: bool,
        incident_ids: tuple[int, ...],
        now_epoch: int | None = None,
    ) -> None:
        try:
            encoded, signature = token.split(".", 1)
            expected = base64.urlsafe_b64encode(
                hmac.new(self._key, encoded.encode("ascii"), hashlib.sha256).digest()
            ).decode("ascii").rstrip("=")
            if not hmac.compare_digest(signature, expected):
                raise ActivationTokenError("NOTIFICATION_ACTIVATION_TOKEN_INVALID")
            raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            payload = json.loads(raw.decode("utf-8"))
        except ActivationTokenError:
            raise
        except Exception as exc:
            raise ActivationTokenError("NOTIFICATION_ACTIVATION_TOKEN_INVALID") from exc
        now = int(time.time()) if now_epoch is None else now_epoch
        if int(payload.get("exp", 0)) < now:
            raise ActivationTokenError("NOTIFICATION_ACTIVATION_TOKEN_EXPIRED")
        expected_payload = {
            "revision_id": revision_id,
            "version": version,
            "notify_existing": notify_existing,
            "ids_hash": _ids_hash(incident_ids),
        }
        if any(payload.get(key) != value for key, value in expected_payload.items()):
            raise ActivationTokenError("NOTIFICATION_ACTIVATION_SET_CHANGED")


class PreparePolicyActivation:
    def __init__(self, store: NotificationStore, tokens: ActivationTokenService) -> None:
        self._store = store
        self._tokens = tokens

    def execute(
        self, revision_id: int, *, expected_version: int, notify_existing: bool
    ) -> ActivationPreparation:
        revision = self._store.get_policy(revision_id)
        if revision.state != "DRAFT":
            raise RuntimeError("NOTIFICATION_POLICY_NOT_DRAFT")
        if revision.version != expected_version:
            raise FileExistsError("NOTIFICATION_POLICY_VERSION_CONFLICT")
        incident_ids = (
            self._store.eligible_existing_incident_ids(revision_id)
            if notify_existing
            else ()
        )
        token, expires = self._tokens.issue(
            revision_id=revision_id,
            version=expected_version,
            notify_existing=notify_existing,
            incident_ids=incident_ids,
        )
        return ActivationPreparation(token, len(incident_ids), expires)


class ConfirmPolicyActivation:
    def __init__(self, store: NotificationStore, tokens: ActivationTokenService) -> None:
        self._store = store
        self._tokens = tokens

    def execute(
        self,
        revision_id: int,
        *,
        expected_version: int,
        notify_existing: bool,
        confirm_token: str,
        now: datetime,
    ) -> PolicyView:
        revision = self._store.get_policy(revision_id)
        incident_ids = (
            self._store.eligible_existing_incident_ids(revision_id)
            if notify_existing
            else ()
        )
        self._tokens.verify(
            confirm_token,
            revision_id=revision_id,
            version=expected_version,
            notify_existing=notify_existing,
            incident_ids=incident_ids,
        )
        return self._store.activate_policy(
            revision_id,
            expected_version=expected_version,
            notify_existing=notify_existing,
            now=now,
        )


def delivery_is_terminal(state: str) -> bool:
    return state in {
        DeliveryState.SUCCEEDED.value,
        DeliveryState.PERMANENTLY_FAILED.value,
        DeliveryState.SUPPRESSED.value,
        DeliveryState.CANCELED.value,
    }
