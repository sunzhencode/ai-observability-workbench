"""SQLAlchemy notification configuration, routing and Outbox adapter."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import cast
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app.adapters.persistence.sources import AlertRecord, IncidentRecord, SourceRecord
from app.application.notifications import (
    AttemptView,
    ChannelDraft,
    ChannelView,
    ClaimedDelivery,
    DeliveryView,
    IncidentNotificationView,
    PolicyDraft,
    PolicyPreview,
    PolicyPreviewSample,
    PolicyView,
    PreparedDelivery,
    ProviderResult,
    ResolvedChannel,
    RouteTargetView,
    RouteView,
)
from app.platform.persistence.codecs import (
    aware_utc as _aware,
    canonical_json as _json,
    stored_utc as _stored,
)
from app.domains.notifications.models import (
    DeliveryEvent,
    DeliveryState,
    IncidentNotificationFact,
    MAX_DELIVERY_ATTEMPTS,
    Matcher,
    MatcherOperator,
    NotificationChange,
    NotificationEnrichment,
    PolicyCandidate,
    ProviderKind,
    choose_policy,
    delivery_event_key,
    is_escalation,
    next_rate_limit_permit,
    retry_delay_seconds,
    response_notification_policy,
    should_create_route,
    validate_channel_configuration,
    validate_replacement_secret,
)
from app.domains.incidents.models import IncidentLifecycleChange
from app.domains.noise.models import NotificationNoiseDecision
from app.platform.persistence.database import SessionFactory


UTC = timezone.utc


class Base(DeclarativeBase):
    pass


class NotificationChannelRecord(Base):
    __tablename__ = "notification_channel"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    active_revision_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class NotificationChannelRevisionRecord(Base):
    __tablename__ = "notification_channel_revision"
    __table_args__ = (
        UniqueConstraint("channel_id", "revision_no", name="uq_notification_channel_revision"),
        Index("ix_notification_channel_revision_state", "channel_id", "state", "revision_no"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[str] = mapped_column(String(128), ForeignKey("notification_channel.id", ondelete="CASCADE"))
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    config_json: Mapped[str] = mapped_column(Text, nullable=False)
    secret_envelopes_json: Mapped[str] = mapped_column(Text, nullable=False)
    tested_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_test_code: Mapped[str | None] = mapped_column(String(96))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class NotificationChannelAuditRecord(Base):
    __tablename__ = "notification_channel_audit"
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[str] = mapped_column(String(128), ForeignKey("notification_channel.id"))
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    changed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class NotificationPolicyRevisionRecord(Base):
    __tablename__ = "notification_policy_revision"
    __table_args__ = (
        UniqueConstraint("logical_id", "version", name="uq_notification_policy_revision"),
        Index("ix_notification_policy_selection", "state", "priority", "id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    logical_id: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    matchers_json: Mapped[str] = mapped_column(Text, nullable=False)
    scope_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    source_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    repeat_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime)


class NotificationPolicyChannelRecord(Base):
    __tablename__ = "notification_policy_channel"
    policy_revision_id: Mapped[int] = mapped_column(Integer, ForeignKey("notification_policy_revision.id", ondelete="CASCADE"), primary_key=True)
    channel_id: Mapped[str] = mapped_column(String(128), ForeignKey("notification_channel.id"), primary_key=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)


class NotificationPolicyAuditRecord(Base):
    __tablename__ = "notification_policy_audit"
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    logical_id: Mapped[str] = mapped_column(String(128), nullable=False)
    revision_id: Mapped[int] = mapped_column(Integer, ForeignKey("notification_policy_revision.id"))
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    changed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class NotificationRouteRecord(Base):
    __tablename__ = "notification_route"
    __table_args__ = (
        UniqueConstraint("incident_id", "occurrence_no", name="uq_notification_route_occurrence"),
        Index("ix_notification_route_reminder", "status", "next_reminder_at", "id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(Integer)
    occurrence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_revision_id: Mapped[int] = mapped_column(Integer, ForeignKey("notification_policy_revision.id"))
    policy_name: Mapped[str] = mapped_column(String(120), nullable=False)
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_priority: Mapped[int] = mapped_column(Integer, nullable=False)
    match_context_json: Mapped[str] = mapped_column(Text, nullable=False)
    repeat_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    current_source_state: Mapped[str] = mapped_column(String(24), nullable=False)
    current_freshness_state: Mapped[str] = mapped_column(String(16), nullable=False)
    current_handling_state: Mapped[str] = mapped_column(String(24), nullable=False)
    current_response_state: Mapped[str] = mapped_column(
        String(24), nullable=False, default="UNACKNOWLEDGED"
    )
    reminders_paused: Mapped[bool] = mapped_column(Boolean, nullable=False)
    last_notified_severity: Mapped[str | None] = mapped_column(String(16))
    last_successful_event_at: Mapped[datetime | None] = mapped_column(DateTime)
    next_reminder_at: Mapped[datetime | None] = mapped_column(DateTime)
    repeat_slot: Mapped[int] = mapped_column(Integer, nullable=False)
    termination_reason: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime)


class NotificationRouteTargetRecord(Base):
    __tablename__ = "notification_route_target"
    __table_args__ = (
        UniqueConstraint("route_id", "channel_id", name="uq_notification_route_channel"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    route_id: Mapped[int] = mapped_column(Integer, ForeignKey("notification_route.id", ondelete="CASCADE"))
    channel_id: Mapped[str] = mapped_column(String(128), ForeignKey("notification_channel.id"))
    routed_channel_revision_id: Mapped[int] = mapped_column(Integer, ForeignKey("notification_channel_revision.id"))
    channel_name: Mapped[str] = mapped_column(String(120), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    channel_revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    opened_success_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime)


class NotificationDeliveryRecord(Base):
    __tablename__ = "notification_delivery"
    __table_args__ = (
        Index("ix_notification_delivery_due", "state", "next_attempt_at", "id"),
        Index("ix_notification_delivery_route", "route_id", "created_at", "id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    incident_id: Mapped[int] = mapped_column(Integer)
    occurrence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    route_id: Mapped[int] = mapped_column(Integer, ForeignKey("notification_route.id"))
    route_target_id: Mapped[int] = mapped_column(Integer, ForeignKey("notification_route_target.id"))
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    incident_change_version: Mapped[int] = mapped_column(Integer, nullable=False)
    repeat_slot: Mapped[int | None] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    next_attempt_trigger: Mapped[str] = mapped_column(String(16), nullable=False)
    lease_token: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    suppression_reason: Mapped[str | None] = mapped_column(String(96))
    execution_mode: Mapped[str] = mapped_column(
        String(24), nullable=False, default="UNKNOWN_LEGACY"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    succeeded_at: Mapped[datetime | None] = mapped_column(DateTime)


class NotificationAttemptRecord(Base):
    __tablename__ = "notification_attempt"
    __table_args__ = (
        UniqueConstraint("delivery_id", "attempt_no", name="uq_notification_delivery_attempt"),
        Index("ix_notification_attempt_started", "started_at", "id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    delivery_id: Mapped[int] = mapped_column(Integer, ForeignKey("notification_delivery.id", ondelete="CASCADE"))
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    outcome: Mapped[str | None] = mapped_column(String(32))
    http_status: Mapped[int | None] = mapped_column(Integer)
    provider_request_id: Mapped[str | None] = mapped_column(String(256))
    error_code: Mapped[str | None] = mapped_column(String(128))


class PlatformSettingRecord(Base):
    __tablename__ = "platform_setting"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


def _object(value: str) -> dict[str, object]:
    raw = json.loads(value)
    if not isinstance(raw, dict):
        raise RuntimeError("NOTIFICATION_STORED_JSON_INVALID")
    return cast(dict[str, object], raw)


def _strings(value: str) -> tuple[str, ...]:
    raw = json.loads(value)
    if not isinstance(raw, list):
        raise RuntimeError("NOTIFICATION_STORED_JSON_INVALID")
    return tuple(str(item) for item in raw)


def _matchers(value: str) -> tuple[Matcher, ...]:
    raw = json.loads(value)
    if not isinstance(raw, list):
        raise RuntimeError("NOTIFICATION_STORED_JSON_INVALID")
    return tuple(
        Matcher(str(item[0]), MatcherOperator(str(item[1])), str(item[2]))
        for item in raw
        if isinstance(item, list) and len(item) == 3
    )


def _validate_name(value: str, code: str) -> str:
    clean = value.strip()
    if not clean or len(clean) > 120:
        raise ValueError(code)
    return clean


class SqlAlchemyNotificationStore:
    """Owns short notification transactions; provider I/O happens elsewhere."""

    def __init__(
        self,
        sessions: SessionFactory,
        *,
        encrypt_secret: Callable[[str], str] | None = None,
        decrypt_secret: Callable[[str], str] | None = None,
        response_state_reader: Callable[[Session, int, int], str | None] | None = None,
        enrichment_reader: Callable[
            [Session, int, int, datetime, bool], NotificationEnrichment
        ]
        | None = None,
        noise_decider: Callable[
            [Session, IncidentNotificationFact, str, datetime],
            NotificationNoiseDecision,
        ]
        | None = None,
        storm_summary_claim: Callable[..., bool] | None = None,
        workbench_url: str = "",
        execution_mode: str = "UNKNOWN_LEGACY",
    ) -> None:
        self._sessions = sessions
        self._encrypt = encrypt_secret or (lambda value: value)
        self._decrypt = decrypt_secret or (lambda value: value)
        self._response_state_reader = response_state_reader
        self._enrichment_reader = enrichment_reader
        self._noise_decider = noise_decider
        self._storm_summary_claim = storm_summary_claim
        self._workbench_url = workbench_url.rstrip("/")
        if execution_mode not in {"FAKE", "EXTERNAL", "UNKNOWN_LEGACY"}:
            raise ValueError("NOTIFICATION_EXECUTION_MODE_INVALID")
        self._execution_mode = execution_mode

    def get_workbench_url(self) -> str:
        with self._sessions() as session:
            setting = session.get(PlatformSettingRecord, "workbench_url")
            return self._workbench_url if setting is None else setting.value

    def save_workbench_url(self, url: str, *, now: datetime) -> str:
        clean = url.strip().rstrip("/")
        if clean and not clean.startswith(("http://", "https://")):
            raise ValueError("WORKBENCH_URL_INVALID")
        if len(clean) > 2048:
            raise ValueError("WORKBENCH_URL_INVALID")
        with self._sessions.begin() as session:
            setting = session.get(PlatformSettingRecord, "workbench_url")
            if setting is None:
                session.add(
                    PlatformSettingRecord(
                        key="workbench_url",
                        value=clean,
                        updated_at=_stored(now),
                    )
                )
            else:
                setting.value = clean
                setting.updated_at = _stored(now)
        return clean

    def _latest_channel_revision(
        self, session: Session, channel_id: str, *, draft: bool = False
    ) -> NotificationChannelRevisionRecord:
        statement = select(NotificationChannelRevisionRecord).where(
            NotificationChannelRevisionRecord.channel_id == channel_id
        )
        if draft:
            statement = statement.where(NotificationChannelRevisionRecord.state == "DRAFT")
        revision = session.scalar(statement.order_by(NotificationChannelRevisionRecord.revision_no.desc()).limit(1))
        if revision is None:
            raise LookupError("NOTIFICATION_CHANNEL_REVISION_NOT_FOUND")
        return revision

    def _channel_view(
        self, session: Session, channel: NotificationChannelRecord, revision: NotificationChannelRevisionRecord | None = None
    ) -> ChannelView:
        chosen = revision or self._latest_channel_revision(session, channel.id)
        envelopes = _object(chosen.secret_envelopes_json)
        return ChannelView(
            id=channel.id,
            name=channel.name,
            provider=channel.provider,
            enabled=channel.enabled,
            revision_no=chosen.revision_no,
            revision_state=chosen.state,
            config=_object(chosen.config_json),
            secret_configured={key: bool(value) for key, value in envelopes.items()},
            tested_at=_aware(chosen.tested_at),
            last_test_code=chosen.last_test_code,
        )

    def _secret_envelopes(
        self,
        draft: ChannelDraft,
        *,
        previous: Mapping[str, object] | None = None,
    ) -> dict[str, str]:
        values = {str(key): str(value) for key, value in (previous or {}).items() if value}
        for name, change in draft.secrets.items():
            action = change.action.upper()
            if action == "KEEP":
                continue
            if action == "CLEAR":
                values.pop(name, None)
                continue
            if action != "REPLACE" or not change.value:
                raise ValueError("NOTIFICATION_SECRET_ACTION_INVALID")
            validate_replacement_secret(draft.provider, name, change.value)
            values[name] = self._encrypt(change.value)
        required = {"webhook"} if draft.provider == ProviderKind.FEISHU_CUSTOM_BOT.value else set()
        if not required.issubset(values):
            raise ValueError("NOTIFICATION_REQUIRED_SECRET_MISSING")
        return values

    def create_channel(self, draft: ChannelDraft, *, now: datetime) -> ChannelView:
        name = _validate_name(draft.name, "NOTIFICATION_CHANNEL_NAME_INVALID")
        provider = ProviderKind(draft.provider).value
        channel_id = f"notification-{uuid4()}"
        with self._sessions.begin() as session:
            channel = NotificationChannelRecord(
                id=channel_id,
                name=name,
                provider=provider,
                enabled=False,
                active_revision_id=None,
                created_at=_stored(now),
                updated_at=_stored(now),
            )
            revision = NotificationChannelRevisionRecord(
                channel_id=channel_id,
                revision_no=1,
                state="DRAFT",
                config_json=_json(validate_channel_configuration(provider, draft.config)),
                secret_envelopes_json=_json(self._secret_envelopes(draft)),
                tested_at=None,
                last_test_code=None,
                created_at=_stored(now),
            )
            session.add_all((channel, revision))
            session.flush()
            session.add(NotificationChannelAuditRecord(channel_id=channel_id, action="CREATE_DRAFT", revision_no=1, changed_at=_stored(now)))
            return self._channel_view(session, channel, revision)

    def update_channel(self, channel_id: str, draft: ChannelDraft, *, expected_revision: int, now: datetime) -> ChannelView:
        with self._sessions.begin() as session:
            channel = session.get(NotificationChannelRecord, channel_id)
            if channel is None:
                raise LookupError("NOTIFICATION_CHANNEL_NOT_FOUND")
            current = self._latest_channel_revision(session, channel_id)
            if current.revision_no != expected_revision:
                raise FileExistsError("NOTIFICATION_CHANNEL_REVISION_CONFLICT")
            if ProviderKind(draft.provider).value != channel.provider:
                raise ValueError("NOTIFICATION_PROVIDER_IMMUTABLE")
            existing_draft = session.scalar(
                select(NotificationChannelRevisionRecord).where(
                    NotificationChannelRevisionRecord.channel_id == channel_id,
                    NotificationChannelRevisionRecord.state == "DRAFT",
                )
            )
            previous = _object(current.secret_envelopes_json)
            envelopes = self._secret_envelopes(draft, previous=previous)
            revision_no = current.revision_no if existing_draft is current else current.revision_no + 1
            if existing_draft is None or existing_draft is not current:
                revision = NotificationChannelRevisionRecord(
                    channel_id=channel_id,
                    revision_no=revision_no,
                    state="DRAFT",
                    config_json=_json(validate_channel_configuration(channel.provider, draft.config)),
                    secret_envelopes_json=_json(envelopes),
                    tested_at=None,
                    last_test_code=None,
                    created_at=_stored(now),
                )
                session.add(revision)
            else:
                revision = existing_draft
                revision.config_json = _json(validate_channel_configuration(channel.provider, draft.config))
                revision.secret_envelopes_json = _json(envelopes)
                revision.tested_at = None
                revision.last_test_code = None
            channel.name = _validate_name(draft.name, "NOTIFICATION_CHANNEL_NAME_INVALID")
            channel.updated_at = _stored(now)
            session.flush()
            session.add(NotificationChannelAuditRecord(channel_id=channel_id, action="UPDATE_DRAFT", revision_no=revision.revision_no, changed_at=_stored(now)))
            return self._channel_view(session, channel, revision)

    def list_channels(self) -> tuple[ChannelView, ...]:
        with self._sessions() as session:
            return tuple(
                self._channel_view(session, item)
                for item in session.scalars(select(NotificationChannelRecord).order_by(NotificationChannelRecord.name))
            )

    def load_channel(self, channel_id: str, *, require_draft: bool = False) -> ResolvedChannel:
        with self._sessions() as session:
            channel = session.get(NotificationChannelRecord, channel_id)
            if channel is None:
                raise LookupError("NOTIFICATION_CHANNEL_NOT_FOUND")
            revision = self._latest_channel_revision(session, channel_id, draft=require_draft)
            envelopes = _object(revision.secret_envelopes_json)
            return ResolvedChannel(
                self._channel_view(session, channel, revision),
                {key: self._decrypt(str(value)) for key, value in envelopes.items()},
            )

    def record_channel_test(self, channel_id: str, *, expected_revision: int, result: ProviderResult, now: datetime) -> ChannelView:
        with self._sessions.begin() as session:
            channel = session.get(NotificationChannelRecord, channel_id)
            if channel is None:
                raise LookupError("NOTIFICATION_CHANNEL_NOT_FOUND")
            revision = self._latest_channel_revision(session, channel_id, draft=True)
            if revision.revision_no != expected_revision:
                raise FileExistsError("NOTIFICATION_CHANNEL_REVISION_CONFLICT")
            revision.tested_at = _stored(now)
            revision.last_test_code = result.code[:96]
            session.add(NotificationChannelAuditRecord(channel_id=channel_id, action="TEST_SUCCESS" if result.ok else "TEST_FAILED", revision_no=revision.revision_no, changed_at=_stored(now)))
            session.flush()
            return self._channel_view(session, channel, revision)

    def activate_channel(self, channel_id: str, *, expected_revision: int, now: datetime) -> ChannelView:
        with self._sessions.begin() as session:
            channel = session.get(NotificationChannelRecord, channel_id)
            if channel is None:
                raise LookupError("NOTIFICATION_CHANNEL_NOT_FOUND")
            revision = self._latest_channel_revision(session, channel_id, draft=True)
            if revision.revision_no != expected_revision:
                raise FileExistsError("NOTIFICATION_CHANNEL_REVISION_CONFLICT")
            if revision.last_test_code != "OK" or revision.tested_at is None:
                raise RuntimeError("NOTIFICATION_CHANNEL_NOT_TESTED")
            if channel.active_revision_id is not None:
                active = session.get(NotificationChannelRevisionRecord, channel.active_revision_id)
                if active is not None:
                    active.state = "RETIRED"
            revision.state = "ACTIVE"
            channel.active_revision_id = revision.id
            channel.enabled = True
            channel.updated_at = _stored(now)
            session.add(NotificationChannelAuditRecord(channel_id=channel_id, action="ACTIVATE", revision_no=revision.revision_no, changed_at=_stored(now)))
            session.flush()
            return self._channel_view(session, channel, revision)

    def set_channel_enabled(self, channel_id: str, *, expected_revision: int, enabled: bool, now: datetime) -> ChannelView:
        with self._sessions.begin() as session:
            channel = session.get(NotificationChannelRecord, channel_id)
            if channel is None:
                raise LookupError("NOTIFICATION_CHANNEL_NOT_FOUND")
            if enabled and channel.active_revision_id is None:
                raise RuntimeError("NOTIFICATION_CHANNEL_HAS_NO_ACTIVE_REVISION")
            revision = self._latest_channel_revision(session, channel_id)
            if revision.revision_no != expected_revision:
                raise FileExistsError("NOTIFICATION_CHANNEL_REVISION_CONFLICT")
            channel.enabled = enabled
            channel.updated_at = _stored(now)
            session.add(NotificationChannelAuditRecord(channel_id=channel_id, action="ENABLE" if enabled else "DISABLE", revision_no=revision.revision_no, changed_at=_stored(now)))
            session.flush()
            return self._channel_view(session, channel, revision)

    def _policy_view(self, session: Session, record: NotificationPolicyRevisionRecord) -> PolicyView:
        channels = tuple(
            session.scalars(
                select(NotificationPolicyChannelRecord.channel_id)
                .where(NotificationPolicyChannelRecord.policy_revision_id == record.id)
                .order_by(NotificationPolicyChannelRecord.position)
            )
        )
        return PolicyView(
            revision_id=record.id,
            logical_id=record.logical_id,
            version=record.version,
            name=record.name,
            state=record.state,
            priority=record.priority,
            matchers=_matchers(record.matchers_json),
            repeat_interval_seconds=record.repeat_interval_seconds,
            channel_ids=channels,
            scope_mode=record.scope_mode,
            source_ids=_strings(record.source_ids_json),
        )

    def _validate_policy_draft(self, session: Session, draft: PolicyDraft) -> None:
        _validate_name(draft.name, "NOTIFICATION_POLICY_NAME_INVALID")
        if len(set(draft.channel_ids)) != len(draft.channel_ids):
            raise ValueError("NOTIFICATION_POLICY_CHANNELS_DUPLICATED")
        existing_channels = set(session.scalars(select(NotificationChannelRecord.id).where(NotificationChannelRecord.id.in_(draft.channel_ids))))
        if existing_channels != set(draft.channel_ids):
            raise ValueError("NOTIFICATION_POLICY_CHANNEL_NOT_FOUND")
        if draft.scope_mode == "SELECTED":
            existing_sources = set(session.scalars(select(SourceRecord.id).where(SourceRecord.id.in_(draft.source_ids))))
            if existing_sources != set(draft.source_ids):
                raise ValueError("NOTIFICATION_POLICY_SOURCE_NOT_FOUND")
        PolicyCandidate(0, "validate", draft.name, 1, draft.priority, draft.matchers, draft.scope_mode, draft.source_ids, draft.channel_ids, draft.repeat_interval_seconds)

    def _bind_policy_channels(self, session: Session, revision_id: int, channel_ids: Sequence[str]) -> None:
        for item in session.scalars(select(NotificationPolicyChannelRecord).where(NotificationPolicyChannelRecord.policy_revision_id == revision_id)):
            session.delete(item)
        session.flush()
        session.add_all(
            NotificationPolicyChannelRecord(policy_revision_id=revision_id, channel_id=channel_id, position=position)
            for position, channel_id in enumerate(channel_ids)
        )

    def create_policy(self, draft: PolicyDraft, *, now: datetime) -> PolicyView:
        with self._sessions.begin() as session:
            self._validate_policy_draft(session, draft)
            record = NotificationPolicyRevisionRecord(
                logical_id=f"policy-{uuid4()}", version=1, name=draft.name.strip(), state="DRAFT",
                priority=draft.priority,
                matchers_json=_json([[item.field, item.operator.value, item.value] for item in draft.matchers]),
                scope_mode=draft.scope_mode,
                source_ids_json=_json(list(draft.source_ids)),
                repeat_interval_seconds=draft.repeat_interval_seconds,
                created_at=_stored(now), updated_at=_stored(now), activated_at=None,
            )
            session.add(record)
            session.flush()
            self._bind_policy_channels(session, record.id, draft.channel_ids)
            session.add(NotificationPolicyAuditRecord(
                logical_id=record.logical_id,
                revision_id=record.id,
                action="CREATE_DRAFT",
                version=record.version,
                changed_at=_stored(now),
            ))
            return self._policy_view(session, record)

    def update_policy(self, logical_id: str, draft: PolicyDraft, *, expected_version: int, now: datetime) -> PolicyView:
        with self._sessions.begin() as session:
            self._validate_policy_draft(session, draft)
            latest = session.scalar(select(NotificationPolicyRevisionRecord).where(NotificationPolicyRevisionRecord.logical_id == logical_id).order_by(NotificationPolicyRevisionRecord.version.desc()).limit(1))
            if latest is None:
                raise LookupError("NOTIFICATION_POLICY_NOT_FOUND")
            if latest.version != expected_version:
                raise FileExistsError("NOTIFICATION_POLICY_VERSION_CONFLICT")
            draft_record = session.scalar(select(NotificationPolicyRevisionRecord).where(NotificationPolicyRevisionRecord.logical_id == logical_id, NotificationPolicyRevisionRecord.state == "DRAFT"))
            if draft_record is latest:
                record = draft_record
            else:
                record = NotificationPolicyRevisionRecord(
                    logical_id=logical_id, version=latest.version + 1, state="DRAFT",
                    created_at=_stored(now), activated_at=None,
                    name="", priority=0, matchers_json="[]", scope_mode="ALL", source_ids_json="[]", repeat_interval_seconds=0, updated_at=_stored(now),
                )
                session.add(record)
            record.name = draft.name.strip()
            record.priority = draft.priority
            record.matchers_json = _json([[item.field, item.operator.value, item.value] for item in draft.matchers])
            record.scope_mode = draft.scope_mode
            record.source_ids_json = _json(list(draft.source_ids))
            record.repeat_interval_seconds = draft.repeat_interval_seconds
            record.updated_at = _stored(now)
            session.flush()
            self._bind_policy_channels(session, record.id, draft.channel_ids)
            session.add(NotificationPolicyAuditRecord(
                logical_id=record.logical_id,
                revision_id=record.id,
                action="UPDATE_DRAFT",
                version=record.version,
                changed_at=_stored(now),
            ))
            session.flush()
            return self._policy_view(session, record)

    def list_policies(self) -> tuple[PolicyView, ...]:
        with self._sessions() as session:
            return tuple(self._policy_view(session, item) for item in session.scalars(select(NotificationPolicyRevisionRecord).order_by(NotificationPolicyRevisionRecord.priority, NotificationPolicyRevisionRecord.logical_id, NotificationPolicyRevisionRecord.version.desc())))

    def _active_policies(self, session: Session) -> tuple[PolicyCandidate, ...]:
        return tuple(self._policy_view(session, item).candidate() for item in session.scalars(select(NotificationPolicyRevisionRecord).where(NotificationPolicyRevisionRecord.state == "ACTIVE")))

    def _incident_fact(self, session: Session, incident: IncidentRecord) -> IncidentNotificationFact:
        source = session.get(SourceRecord, incident.source_id)
        members: list[dict[str, str]] = []
        alerts = list(session.scalars(select(AlertRecord).where(AlertRecord.incident_id == incident.id).order_by(AlertRecord.id)))
        for alert in alerts:
            labels = _object(alert.labels_json)
            annotations = _object(alert.annotations_json)
            summary = ""
            for key in ("summary", "description", "message"):
                if key in annotations:
                    summary = str(annotations[key])[:240]
                    break
            members.append({
                "alertname": alert.alertname,
                "severity": alert.severity,
                "source_state": alert.source_state,
                "cluster": str(labels.get("cluster") or alert.cluster),
                "namespace": str(labels.get("namespace") or ""),
                "service": str(labels.get("service") or ""),
                "job": str(labels.get("job") or ""),
                "workload": str(labels.get("workload") or ""),
                "summary": summary,
            })
        return IncidentNotificationFact(
            incident_id=incident.id,
            occurrence_no=incident.occurrence_no,
            source_id=incident.source_id,
            source_name=source.name if source is not None else incident.source_id,
            title=incident.title,
            severity=incident.severity,
            source_state=incident.source_state,
            freshness_state=incident.freshness_state,
            aggregation_rule_id=incident.aggregation_rule_id,
            group_labels={str(key): str(value) for key, value in _object(incident.group_labels_json).items()},
            member_count=len(alerts),
            members=tuple(members),
        )

    def preview_policy(self, candidate: PolicyCandidate) -> PolicyPreview:
        with self._sessions() as session:
            active = tuple(item for item in self._active_policies(session) if item.logical_id != candidate.logical_id)
            combined = active + (candidate,)
            direct = shadowed = final = firing = unrouted = 0
            samples: list[PolicyPreviewSample] = []
            for incident in session.scalars(select(IncidentRecord).order_by(IncidentRecord.id)):
                fact = self._incident_fact(session, incident)
                direct_match = candidate.matches(fact)
                winner = choose_policy(fact, combined)
                direct += int(direct_match)
                shadowed += int(direct_match and (winner is None or winner.revision_id != candidate.revision_id))
                final += int(winner is not None and winner.revision_id == candidate.revision_id)
                firing += int(winner is not None and winner.revision_id == candidate.revision_id and fact.source_state == "FIRING")
                unrouted += int(winner is None)
                if len(samples) < 10 and (direct_match or winner is None):
                    samples.append(PolicyPreviewSample(fact.incident_id, None if winner is None else winner.revision_id, None if winner is None else winner.name))
            disabled = sum(
                1 for channel_id in candidate.channel_ids
                if (channel := session.get(NotificationChannelRecord, channel_id)) is None or not channel.enabled or channel.active_revision_id is None
            )
            return PolicyPreview(direct, shadowed, final, firing, unrouted, disabled, tuple(samples))

    def get_policy(self, revision_id: int) -> PolicyView:
        with self._sessions() as session:
            record = session.get(NotificationPolicyRevisionRecord, revision_id)
            if record is None:
                raise LookupError("NOTIFICATION_POLICY_NOT_FOUND")
            return self._policy_view(session, record)

    def _eligible_existing(self, session: Session, revision: NotificationPolicyRevisionRecord) -> tuple[IncidentRecord, ...]:
        candidate = self._policy_view(session, revision).candidate()
        if not any(
            (channel := session.get(NotificationChannelRecord, channel_id)) is not None
            and channel.enabled and channel.active_revision_id is not None
            for channel_id in candidate.channel_ids
        ):
            return ()
        active = tuple(item for item in self._active_policies(session) if item.logical_id != candidate.logical_id) + (candidate,)
        eligible: list[IncidentRecord] = []
        for incident in session.scalars(select(IncidentRecord).where(IncidentRecord.source_state == "FIRING").order_by(IncidentRecord.id)):
            if incident.freshness_state == "STALE":
                continue
            route = session.scalar(select(NotificationRouteRecord.id).where(NotificationRouteRecord.incident_id == incident.id, NotificationRouteRecord.occurrence_no == incident.occurrence_no))
            live = session.scalar(select(AlertRecord.id).where(AlertRecord.incident_id == incident.id, AlertRecord.origin == "LIVE_POLL"))
            fact = self._incident_fact(session, incident)
            winner = choose_policy(fact, active)
            if route is None and live is not None and winner is not None and winner.revision_id == candidate.revision_id:
                eligible.append(incident)
        return tuple(eligible)

    def eligible_existing_incident_ids(self, revision_id: int) -> tuple[int, ...]:
        with self._sessions() as session:
            revision = session.get(NotificationPolicyRevisionRecord, revision_id)
            if revision is None or revision.state != "DRAFT":
                raise LookupError("NOTIFICATION_POLICY_DRAFT_NOT_FOUND")
            return tuple(item.id for item in self._eligible_existing(session, revision))

    def activate_policy(self, revision_id: int, *, expected_version: int, notify_existing: bool, now: datetime) -> PolicyView:
        with self._sessions.begin() as session:
            revision = session.get(NotificationPolicyRevisionRecord, revision_id)
            if revision is None or revision.state != "DRAFT":
                raise LookupError("NOTIFICATION_POLICY_DRAFT_NOT_FOUND")
            if revision.version != expected_version:
                raise FileExistsError("NOTIFICATION_POLICY_VERSION_CONFLICT")
            eligible = self._eligible_existing(session, revision) if notify_existing else ()
            for active in session.scalars(select(NotificationPolicyRevisionRecord).where(NotificationPolicyRevisionRecord.logical_id == revision.logical_id, NotificationPolicyRevisionRecord.state == "ACTIVE")):
                active.state = "RETIRED"
            revision.state = "ACTIVE"
            revision.activated_at = _stored(now)
            revision.updated_at = _stored(now)
            session.add(NotificationPolicyAuditRecord(
                logical_id=revision.logical_id,
                revision_id=revision.id,
                action="ACTIVATE_NOTIFY_EXISTING" if notify_existing else "ACTIVATE",
                version=revision.version,
                changed_at=_stored(now),
            ))
            session.flush()
            for incident in eligible:
                fact = self._incident_fact(session, incident)
                change = NotificationChange(incident.source_state, incident.source_state, incident.severity, incident.severity, "NEW", "NEW", 1, "EXPLICIT_ACTIVATION", now)
                self.plan_change_in_session(session, fact, change)
            return self._policy_view(session, revision)

    def disable_policy(self, revision_id: int, *, expected_version: int, now: datetime) -> PolicyView:
        with self._sessions.begin() as session:
            revision = session.get(NotificationPolicyRevisionRecord, revision_id)
            if revision is None:
                raise LookupError("NOTIFICATION_POLICY_NOT_FOUND")
            if revision.state != "ACTIVE":
                raise RuntimeError("NOTIFICATION_POLICY_NOT_ACTIVE")
            if revision.version != expected_version:
                raise FileExistsError("NOTIFICATION_POLICY_VERSION_CONFLICT")
            revision.state = "RETIRED"
            revision.updated_at = _stored(now)
            session.add(NotificationPolicyAuditRecord(
                logical_id=revision.logical_id,
                revision_id=revision.id,
                action="DISABLE",
                version=revision.version,
                changed_at=_stored(now),
            ))
            session.flush()
            return self._policy_view(session, revision)

    def _route(self, session: Session, fact: IncidentNotificationFact) -> NotificationRouteRecord | None:
        return session.scalar(select(NotificationRouteRecord).where(NotificationRouteRecord.incident_id == fact.incident_id, NotificationRouteRecord.occurrence_no == fact.occurrence_no))

    @staticmethod
    def _reuse_route_evidence(
        session: Session,
        route_id: int,
        payload: dict[str, object],
    ) -> None:
        """Reuse frozen first/escalation facts; never query a source for later events."""
        rows = session.scalars(
            select(NotificationDeliveryRecord)
            .where(
                NotificationDeliveryRecord.route_id == route_id,
                NotificationDeliveryRecord.event_type.in_(
                    (
                        DeliveryEvent.FIRING_OPENED.value,
                        DeliveryEvent.SEVERITY_ESCALATED.value,
                    )
                ),
            )
            .order_by(NotificationDeliveryRecord.id.desc())
        )
        for row in rows:
            prior = _object(row.payload_snapshot_json)
            evidence = prior.get("metric_evidence")
            if not isinstance(evidence, list) or not evidence:
                continue
            payload["metric_evidence"] = evidence[:2]
            links = prior.get("deep_links")
            payload["deep_links"] = links[:2] if isinstance(links, list) else []
            payload["evidence_status"] = "REUSED"
            payload.pop("evidence_safe_code", None)
            return

    def _delivery(
        self,
        session: Session,
        *,
        fact: IncidentNotificationFact,
        route: NotificationRouteRecord,
        target: NotificationRouteTargetRecord,
        event: DeliveryEvent,
        change: NotificationChange,
        repeat_slot: int | None = None,
    ) -> NotificationDeliveryRecord:
        key = delivery_event_key(incident_id=fact.incident_id, route_id=route.id, target_id=target.id, event_type=event, change_version=change.change_version, repeat_slot=repeat_slot)
        existing = session.scalar(select(NotificationDeliveryRecord).where(NotificationDeliveryRecord.event_key == key))
        if existing is not None:
            return existing
        channel = session.get(NotificationChannelRecord, target.channel_id)
        state = DeliveryState.PENDING.value if channel is not None and channel.enabled else DeliveryState.SUPPRESSED.value
        noise = (
            None
            if state != DeliveryState.PENDING.value or self._noise_decider is None
            else self._noise_decider(session, fact, event.value, change.observed_at)
        )
        payload = fact.payload_snapshot(change_version=change.change_version)
        if self._enrichment_reader is not None:
            try:
                enrichment = self._enrichment_reader(
                    session,
                    fact.incident_id,
                    fact.occurrence_no,
                    change.observed_at,
                    event
                    in {
                        DeliveryEvent.FIRING_OPENED,
                        DeliveryEvent.SEVERITY_ESCALATED,
                    },
                )
            except Exception:
                enrichment = NotificationEnrichment(
                    occurrence_id=None,
                    evidence_status="UNAVAILABLE",
                    safe_code="NOTIFICATION_EVIDENCE_PROJECTION_FAILED",
                )
            payload.update(enrichment.payload())
            if event in {DeliveryEvent.REMINDER, DeliveryEvent.RECOVERED}:
                self._reuse_route_evidence(session, route.id, payload)
        storm_summary = False
        if (
            noise is not None
            and noise.state.value == "STORM"
            and self._storm_summary_claim is not None
        ):
            target_set = tuple(
                sorted(
                    session.scalars(
                        select(NotificationRouteTargetRecord.channel_id).where(
                            NotificationRouteTargetRecord.route_id == route.id
                        )
                    )
                )
            )
            window_started_at = change.observed_at.replace(
                minute=change.observed_at.minute - change.observed_at.minute % 5,
                second=0,
                microsecond=0,
            )
            raw_key = "|".join(
                (
                    fact.source_id,
                    str(route.policy_revision_id),
                    ",".join(target_set),
                    window_started_at.isoformat(),
                    target.channel_id,
                )
            )
            summary_key = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
            storm_summary = self._storm_summary_claim(
                session,
                summary_key=summary_key,
                source_id=fact.source_id,
                policy_revision_id=route.policy_revision_id,
                target_set=target_set,
                route_target_id=target.id,
                window_started_at=window_started_at,
                now=change.observed_at,
            )
            if storm_summary:
                payload["noise_summary"] = {
                    "state": "STORM",
                    "scope": "SOURCE",
                    "window_seconds": 300,
                    "new_alert_count": noise.summary_alert_count,
                    "new_occurrence_count": noise.summary_occurrence_count,
                    "message": "来源在最近 5 分钟进入告警风暴；后续同策略与目标集合的消息将聚合。",
                }
        if noise is not None and noise.suppress and not storm_summary:
            state = DeliveryState.SUPPRESSED.value
        available_at = change.observed_at + timedelta(
            seconds=0 if noise is None else noise.delay_seconds
        )
        delivery = NotificationDeliveryRecord(
            event_key=key, incident_id=fact.incident_id, occurrence_no=fact.occurrence_no,
            route_id=route.id, route_target_id=target.id, event_type=event.value,
            incident_change_version=change.change_version, repeat_slot=repeat_slot, state=state,
            payload_snapshot_json=_json(payload),
            scheduled_at=_stored(available_at), next_attempt_at=_stored(available_at),
            attempt_count=0, next_attempt_trigger="AUTO", lease_token=None, lease_expires_at=None,
            suppression_reason=(
                None
                if state == DeliveryState.PENDING.value
                else (
                    noise.reason_code
                    if noise is not None and noise.suppress
                    else "CHANNEL_DISABLED"
                )
            ),
            execution_mode=self._execution_mode,
            created_at=_stored(change.observed_at), updated_at=_stored(change.observed_at), succeeded_at=None,
        )
        session.add(delivery)
        session.flush()
        return delivery

    def _cancel_opened(self, session: Session, target_id: int, reason: str) -> None:
        for delivery in session.scalars(select(NotificationDeliveryRecord).where(NotificationDeliveryRecord.route_target_id == target_id, NotificationDeliveryRecord.event_type == DeliveryEvent.FIRING_OPENED.value, NotificationDeliveryRecord.state.in_((DeliveryState.PENDING.value, DeliveryState.RETRY_WAIT.value)))):
            delivery.state = DeliveryState.CANCELED.value
            delivery.suppression_reason = reason

    def _cancel_route(self, session: Session, route_id: int, reason: str) -> None:
        for delivery in session.scalars(select(NotificationDeliveryRecord).where(NotificationDeliveryRecord.route_id == route_id, NotificationDeliveryRecord.state.in_((DeliveryState.PENDING.value, DeliveryState.RETRY_WAIT.value)))):
            delivery.state = DeliveryState.CANCELED.value
            delivery.suppression_reason = reason

    def _cancel_route_event(
        self,
        session: Session,
        route_id: int,
        event: DeliveryEvent,
        reason: str,
    ) -> None:
        for delivery in session.scalars(
            select(NotificationDeliveryRecord).where(
                NotificationDeliveryRecord.route_id == route_id,
                NotificationDeliveryRecord.event_type == event.value,
                NotificationDeliveryRecord.state.in_(
                    (DeliveryState.PENDING.value, DeliveryState.RETRY_WAIT.value)
                ),
            )
        ):
            delivery.state = DeliveryState.CANCELED.value
            delivery.suppression_reason = reason

    def _create_route(self, session: Session, fact: IncidentNotificationFact, change: NotificationChange) -> NotificationRouteRecord | None:
        winner = choose_policy(fact, self._active_policies(session))
        if winner is None:
            return None
        targets: list[tuple[NotificationChannelRecord, NotificationChannelRevisionRecord]] = []
        for channel_id in winner.channel_ids:
            channel = session.get(NotificationChannelRecord, channel_id)
            if channel is None or not channel.enabled or channel.active_revision_id is None:
                continue
            revision = session.get(NotificationChannelRevisionRecord, channel.active_revision_id)
            if revision is not None and revision.state == "ACTIVE":
                targets.append((channel, revision))
        if not targets:
            return None
        response_state = (
            None
            if self._response_state_reader is None
            else self._response_state_reader(
                session, fact.incident_id, fact.occurrence_no
            )
        ) or "UNACKNOWLEDGED"
        response_policy = response_notification_policy(response_state)
        if response_policy.terminated:
            return None
        route = NotificationRouteRecord(
            incident_id=fact.incident_id, occurrence_no=fact.occurrence_no,
            policy_revision_id=winner.revision_id, policy_name=winner.name,
            policy_version=winner.version, policy_priority=winner.priority,
            match_context_json=_json(fact.route_context()),
            repeat_interval_seconds=winner.repeat_interval_seconds, status="ACTIVE",
            current_source_state=change.current_source_state, current_freshness_state=fact.freshness_state,
            current_handling_state=change.current_handling_state,
            current_response_state=response_state,
            reminders_paused=not response_policy.allows(DeliveryEvent.REMINDER),
            last_notified_severity=None, last_successful_event_at=None, next_reminder_at=None,
            repeat_slot=0, termination_reason=None, created_at=_stored(change.observed_at), closed_at=None,
        )
        session.add(route)
        session.flush()
        route_targets: list[NotificationRouteTargetRecord] = []
        for channel, revision in targets:
            target = NotificationRouteTargetRecord(
                route_id=route.id, channel_id=channel.id,
                routed_channel_revision_id=revision.id, channel_name=channel.name,
                provider=channel.provider, channel_revision_no=revision.revision_no,
                opened_success_at=None, last_success_at=None,
            )
            session.add(target)
            route_targets.append(target)
        session.flush()
        for target in route_targets:
            self._delivery(session, fact=fact, route=route, target=target, event=DeliveryEvent.FIRING_OPENED, change=change)
        return route

    def plan_change_in_session(self, session: Session, fact: IncidentNotificationFact, change: NotificationChange) -> int | None:
        """Plan inside the caller's transaction so Incident and Outbox commit together."""
        route = self._route(session, fact)
        if route is None:
            if should_create_route(fact, change):
                created = self._create_route(session, fact, change)
                return None if created is None else created.id
            return None
        route.current_source_state = change.current_source_state
        route.current_freshness_state = fact.freshness_state
        route.current_handling_state = change.current_handling_state
        response_policy = response_notification_policy(route.current_response_state)
        route.reminders_paused = not response_policy.allows(DeliveryEvent.REMINDER)
        if response_policy.terminated:
            return route.id
        targets = tuple(session.scalars(select(NotificationRouteTargetRecord).where(NotificationRouteTargetRecord.route_id == route.id)))
        if (
            change.current_source_state.upper() == "RECOVERED"
            and response_policy.allows(DeliveryEvent.RECOVERED)
        ):
            for target in targets:
                if target.opened_success_at is None:
                    self._cancel_opened(session, target.id, "SUPERSEDED_BEFORE_OPEN")
                else:
                    self._delivery(session, fact=fact, route=route, target=target, event=DeliveryEvent.RECOVERED, change=change)
            route.status = "RECOVERED"
            route.closed_at = _stored(change.observed_at)
            route.next_reminder_at = None
            return route.id
        if (
            change.current_source_state.upper() == "FIRING"
            and is_escalation(change)
            and response_policy.allows(DeliveryEvent.SEVERITY_ESCALATED)
        ):
            for target in targets:
                if target.opened_success_at is None:
                    self._cancel_opened(session, target.id, "SUPERSEDED_BY_ESCALATION")
                    event = DeliveryEvent.FIRING_OPENED
                else:
                    event = DeliveryEvent.SEVERITY_ESCALATED
                self._delivery(session, fact=fact, route=route, target=target, event=event, change=change)
        return route.id

    def apply_occurrence_response_in_session(
        self,
        session: Session,
        *,
        incident_id: int,
        occurrence_no: int,
        response_state: str,
        observed_at: datetime,
    ) -> int | None:
        """Apply the one Response-to-notification mapping in the caller UoW."""
        route = session.scalar(
            select(NotificationRouteRecord).where(
                NotificationRouteRecord.incident_id == incident_id,
                NotificationRouteRecord.occurrence_no == occurrence_no,
            )
        )
        if route is None:
            return None
        response_policy = response_notification_policy(response_state)
        route.current_response_state = response_state
        route.reminders_paused = not response_policy.allows(DeliveryEvent.REMINDER)
        route.next_reminder_at = None
        if not response_policy.allows(DeliveryEvent.REMINDER):
            self._cancel_route_event(
                session,
                route.id,
                DeliveryEvent.REMINDER,
                f"RESPONSE_{response_state}",
            )
        if response_policy.terminated:
            route.status = "TERMINATED"
            route.termination_reason = "RESPONSE_RESOLVED"
            route.closed_at = _stored(observed_at)
            self._cancel_route(session, route.id, "RESPONSE_RESOLVED")
        return route.id

    def plan_change(self, fact: IncidentNotificationFact, change: NotificationChange) -> int | None:
        with self._sessions.begin() as session:
            return self.plan_change_in_session(session, fact, change)

    def plan_incident_change_in_session(
        self,
        session: Session,
        incident_id: int,
        change: IncidentLifecycleChange,
    ) -> int | None:
        incident = session.get(IncidentRecord, incident_id)
        if incident is None:
            raise LookupError("INCIDENT_NOT_FOUND")
        fact = self._incident_fact(session, incident)
        notification_change = NotificationChange(
            change.previous_source_state,
            change.current_source_state,
            change.previous_severity,
            change.current_severity,
            change.previous_handling_state,
            change.current_handling_state,
            change.change_version,
            change.origin,
            change.observed_at,
        )
        return self.plan_change_in_session(session, fact, notification_change)

    def plan_due_reminders(self, *, now: datetime) -> int:
        created = 0
        with self._sessions.begin() as session:
            routes = tuple(session.scalars(select(NotificationRouteRecord).where(
                NotificationRouteRecord.status == "ACTIVE",
                NotificationRouteRecord.repeat_interval_seconds > 0,
                NotificationRouteRecord.next_reminder_at.is_not(None),
                NotificationRouteRecord.next_reminder_at <= _stored(now),
                NotificationRouteRecord.reminders_paused.is_(False),
            )))
            for route in routes:
                if route.current_source_state.upper() != "FIRING" or route.current_freshness_state == "STALE":
                    continue
                outstanding = session.scalar(select(NotificationDeliveryRecord.id).where(NotificationDeliveryRecord.route_id == route.id, NotificationDeliveryRecord.event_type == DeliveryEvent.REMINDER.value, NotificationDeliveryRecord.state.in_((DeliveryState.PENDING.value, DeliveryState.IN_FLIGHT.value, DeliveryState.RETRY_WAIT.value))))
                if outstanding is not None:
                    continue
                incident = session.get(IncidentRecord, route.incident_id)
                if incident is None:
                    continue
                fact = self._incident_fact(session, incident)
                route.repeat_slot += 1
                route.next_reminder_at = _stored(now + timedelta(seconds=route.repeat_interval_seconds))
                change = NotificationChange(route.current_source_state, route.current_source_state, fact.severity, fact.severity, route.current_handling_state, route.current_handling_state, route.repeat_slot, "REMINDER", now)
                for target in session.scalars(select(NotificationRouteTargetRecord).where(NotificationRouteTargetRecord.route_id == route.id, NotificationRouteTargetRecord.opened_success_at.is_not(None))):
                    self._delivery(session, fact=fact, route=route, target=target, event=DeliveryEvent.REMINDER, change=change, repeat_slot=route.repeat_slot)
                    created += 1
        return created

    def has_due_deliveries(self, *, now: datetime) -> bool:
        with self._sessions() as session:
            return session.scalar(select(NotificationDeliveryRecord.id).where(NotificationDeliveryRecord.state.in_((DeliveryState.PENDING.value, DeliveryState.RETRY_WAIT.value)), NotificationDeliveryRecord.next_attempt_at <= _stored(now)).limit(1)) is not None

    def has_due_reminders(self, *, now: datetime) -> bool:
        with self._sessions() as session:
            return session.scalar(
                select(NotificationRouteRecord.id)
                .where(
                    NotificationRouteRecord.status == "ACTIVE",
                    NotificationRouteRecord.repeat_interval_seconds > 0,
                    NotificationRouteRecord.next_reminder_at.is_not(None),
                    NotificationRouteRecord.next_reminder_at <= _stored(now),
                    NotificationRouteRecord.reminders_paused.is_(False),
                )
                .limit(1)
            ) is not None

    def _expire_leases(self, session: Session, now: datetime) -> None:
        candidates = session.scalars(
            select(NotificationDeliveryRecord)
            .where(
                NotificationDeliveryRecord.state == DeliveryState.IN_FLIGHT.value,
                NotificationDeliveryRecord.lease_expires_at.is_not(None),
            )
            .order_by(NotificationDeliveryRecord.lease_expires_at, NotificationDeliveryRecord.id)
            .limit(100)
        )
        for delivery in candidates:
            expires_at = _aware(delivery.lease_expires_at)
            if expires_at is None or expires_at > now.astimezone(UTC):
                continue
            attempt = session.scalar(select(NotificationAttemptRecord).where(NotificationAttemptRecord.delivery_id == delivery.id, NotificationAttemptRecord.finished_at.is_(None)).order_by(NotificationAttemptRecord.attempt_no.desc()).limit(1))
            if attempt is not None:
                attempt.finished_at = _stored(now)
                attempt.outcome = "AMBIGUOUS"
                attempt.error_code = "LEASE_EXPIRED"
            delivery.state = DeliveryState.RETRY_WAIT.value
            delivery.next_attempt_at = _stored(now)
            delivery.lease_token = None
            delivery.lease_expires_at = None
            delivery.updated_at = _stored(now)
        session.flush()

    def claim_deliveries(self, *, now: datetime, batch_size: int, lease_seconds: int) -> tuple[ClaimedDelivery, ...]:
        claimed: list[ClaimedDelivery] = []
        with self._sessions.begin() as session:
            self._expire_leases(session, now)
            rows = tuple(session.scalars(select(NotificationDeliveryRecord).where(NotificationDeliveryRecord.state.in_((DeliveryState.PENDING.value, DeliveryState.RETRY_WAIT.value)), NotificationDeliveryRecord.next_attempt_at <= _stored(now)).order_by(NotificationDeliveryRecord.next_attempt_at, NotificationDeliveryRecord.id).limit(min(max(batch_size, 1), 100))))
            for delivery in rows:
                token = str(uuid4())
                delivery.state = DeliveryState.IN_FLIGHT.value
                delivery.lease_token = token
                delivery.lease_expires_at = _stored(now + timedelta(seconds=min(max(lease_seconds, 10), 300)))
                delivery.updated_at = _stored(now)
                claimed.append(ClaimedDelivery(delivery.id, token))
        return tuple(claimed)

    def _channel_attempt_times(self, session: Session, channel_id: str, now: datetime) -> tuple[datetime, ...]:
        result: list[datetime] = []
        attempts = session.scalars(select(NotificationAttemptRecord).where(NotificationAttemptRecord.started_at > _stored(now - timedelta(seconds=60))))
        for attempt in attempts:
            delivery = session.get(NotificationDeliveryRecord, attempt.delivery_id)
            target = None if delivery is None else session.get(NotificationRouteTargetRecord, delivery.route_target_id)
            aware = _aware(attempt.started_at)
            if target is not None and target.channel_id == channel_id and aware is not None:
                result.append(aware)
        return tuple(result)

    def _render_payload(self, delivery: NotificationDeliveryRecord) -> dict[str, object]:
        snapshot = _object(delivery.payload_snapshot_json)
        incident_id = int(str(snapshot.get("incident_id") or delivery.incident_id))
        subject = f"[{snapshot.get('severity', 'unknown')}] {snapshot.get('title', 'Incident')}"
        lines = [
            f"事件 INC-{incident_id} · {delivery.event_type}",
            f"来源：{snapshot.get('source_name') or snapshot.get('source_id') or 'unknown'}",
            f"信号：{snapshot.get('source_state') or 'unknown'} · 严重度：{snapshot.get('severity') or 'unknown'}",
            f"成员：{snapshot.get('member_count') or 0}",
        ]
        members = snapshot.get("members")
        if isinstance(members, list):
            for item in members[:5]:
                if isinstance(item, Mapping):
                    lines.append(f"- {item.get('alertname', '')}: {item.get('summary', '')}"[:400])
        evidence = snapshot.get("metric_evidence")
        if isinstance(evidence, list) and evidence:
            lines.append("已有确定性指标：")
            for item in evidence[:2]:
                if not isinstance(item, Mapping):
                    continue
                name = item.get("display_name") or item.get("metric_id") or "指标"
                latest = item.get("latest")
                minimum = item.get("minimum")
                maximum = item.get("maximum")
                unit = str(item.get("unit") or "")
                suffix = f" {unit}" if unit else ""
                lines.append(
                    f"- {name}：最新 {latest} · 范围 {minimum}–{maximum}{suffix}"[:400]
                )
        if snapshot.get("evidence_status") == "UNAVAILABLE":
            code = snapshot.get("evidence_safe_code") or "NOTIFICATION_EVIDENCE_UNAVAILABLE"
            lines.append(f"指标证据：本次未能附加；通知仍按时发送（{code}）")
        workbench_url = self.get_workbench_url()
        occurrence_id = snapshot.get("operational_occurrence_id")
        if workbench_url and isinstance(occurrence_id, int):
            lines.append(f"工作台：{workbench_url}/incidents/{occurrence_id}")
        deep_links = snapshot.get("deep_links")
        if isinstance(deep_links, list):
            for item in deep_links[:2]:
                if isinstance(item, Mapping) and item.get("url"):
                    label = item.get("label") or "指标面板"
                    lines.append(f"Grafana · {label}：{item['url']}"[:2300])
        return {"schema_version": 1, "event_type": delivery.event_type, "subject": subject[:200], "text": "\n".join(lines)}

    def prepare_delivery(self, claim: ClaimedDelivery, *, now: datetime) -> PreparedDelivery | None:
        with self._sessions.begin() as session:
            delivery = session.get(NotificationDeliveryRecord, claim.delivery_id)
            if delivery is None or delivery.state != DeliveryState.IN_FLIGHT.value or delivery.lease_token != claim.lease_token:
                return None
            route = session.get(NotificationRouteRecord, delivery.route_id)
            target = session.get(NotificationRouteTargetRecord, delivery.route_target_id)
            if route is None or target is None:
                delivery.state = DeliveryState.PERMANENTLY_FAILED.value
                delivery.suppression_reason = "BROKEN_REFERENCE"
                delivery.lease_token = delivery.lease_expires_at = None
                return None
            reason: str | None = None
            event = DeliveryEvent(delivery.event_type)
            response_policy = response_notification_policy(route.current_response_state)
            if not response_policy.allows(event):
                reason = f"RESPONSE_{route.current_response_state}"
            elif route.status == "TERMINATED":
                reason = route.termination_reason or "ROUTE_TERMINATED"
            elif delivery.event_type != DeliveryEvent.RECOVERED.value and route.current_freshness_state == "STALE":
                reason = "SOURCE_STALE"
            elif delivery.event_type != DeliveryEvent.RECOVERED.value and route.current_source_state.upper() != "FIRING":
                reason = "INCIDENT_NOT_FIRING"
            channel = session.get(NotificationChannelRecord, target.channel_id)
            if channel is None or not channel.enabled:
                delivery.state = DeliveryState.SUPPRESSED.value
                delivery.suppression_reason = "CHANNEL_DISABLED"
                delivery.lease_token = delivery.lease_expires_at = None
                return None
            if reason is not None:
                delivery.state = DeliveryState.CANCELED.value
                delivery.suppression_reason = reason
                delivery.lease_token = delivery.lease_expires_at = None
                return None
            permit = next_rate_limit_permit(self._channel_attempt_times(session, target.channel_id, now), now=now)
            if permit > now:
                delivery.state = DeliveryState.PENDING.value
                delivery.next_attempt_at = _stored(permit)
                delivery.lease_token = delivery.lease_expires_at = None
                return None
            revision = session.get(NotificationChannelRevisionRecord, target.routed_channel_revision_id)
            if revision is None:
                delivery.state = DeliveryState.PERMANENTLY_FAILED.value
                delivery.suppression_reason = "CHANNEL_REVISION_MISSING"
                delivery.lease_token = delivery.lease_expires_at = None
                return None
            envelopes = _object(revision.secret_envelopes_json)
            try:
                secrets = {key: self._decrypt(str(value)) for key, value in envelopes.items()}
            except Exception:
                delivery.state = DeliveryState.RETRY_WAIT.value
                delivery.next_attempt_at = _stored(now + timedelta(seconds=60))
                delivery.suppression_reason = "SECRET_UNAVAILABLE"
                delivery.lease_token = delivery.lease_expires_at = None
                return None
            delivery.attempt_count += 1
            attempt = NotificationAttemptRecord(
                delivery_id=delivery.id, attempt_no=delivery.attempt_count,
                trigger=delivery.next_attempt_trigger, started_at=_stored(now),
                finished_at=None, outcome=None, http_status=None,
                provider_request_id=None, error_code=None,
            )
            delivery.next_attempt_trigger = "AUTO"
            delivery.updated_at = _stored(now)
            session.add(attempt)
            session.flush()
            return PreparedDelivery(delivery.id, claim.lease_token, attempt.id, target.provider, self._render_payload(delivery), _object(revision.config_json), secrets)

    def finish_delivery(self, prepared: PreparedDelivery, result: ProviderResult, *, now: datetime) -> bool:
        with self._sessions.begin() as session:
            delivery = session.get(NotificationDeliveryRecord, prepared.delivery_id)
            attempt = session.get(NotificationAttemptRecord, prepared.attempt_id)
            if delivery is None or attempt is None or delivery.state != DeliveryState.IN_FLIGHT.value or delivery.lease_token != prepared.lease_token:
                return False
            attempt.finished_at = _stored(now)
            attempt.http_status = result.http_status
            attempt.provider_request_id = None if result.request_id is None else result.request_id[:256]
            attempt.error_code = None if result.ok else result.code[:128]
            route = session.get(NotificationRouteRecord, delivery.route_id)
            target = session.get(NotificationRouteTargetRecord, delivery.route_target_id)
            if result.ok:
                attempt.outcome = "SUCCESS"
                delivery.state = DeliveryState.SUCCEEDED.value
                delivery.succeeded_at = _stored(now)
                if target is not None:
                    if delivery.event_type == DeliveryEvent.FIRING_OPENED.value and target.opened_success_at is None:
                        target.opened_success_at = _stored(now)
                    target.last_success_at = _stored(now)
                if route is not None:
                    route.last_successful_event_at = _stored(now)
                    snapshot = _object(delivery.payload_snapshot_json)
                    route.last_notified_severity = str(snapshot.get("severity") or "unknown")[:16]
                    if delivery.event_type != DeliveryEvent.RECOVERED.value and route.repeat_interval_seconds > 0 and not route.reminders_paused:
                        route.next_reminder_at = _stored(now + timedelta(seconds=route.repeat_interval_seconds))
                    elif delivery.event_type == DeliveryEvent.RECOVERED.value:
                        route.next_reminder_at = None
            else:
                attempt.outcome = "AMBIGUOUS" if result.code in {"TIMEOUT", "NETWORK", "SMTP_NETWORK"} else "TRANSIENT_FAILURE" if result.transient else "PERMANENT_FAILURE"
                if result.transient and delivery.attempt_count < MAX_DELIVERY_ATTEMPTS:
                    delivery.state = DeliveryState.RETRY_WAIT.value
                    delivery.next_attempt_at = _stored(now + timedelta(seconds=retry_delay_seconds(delivery.event_key, delivery.attempt_count)))
                else:
                    delivery.state = DeliveryState.PERMANENTLY_FAILED.value
            delivery.lease_token = None
            delivery.lease_expires_at = None
            delivery.updated_at = _stored(now)
            return True

    def _attempt_view(self, record: NotificationAttemptRecord) -> AttemptView:
        return AttemptView(record.id, record.attempt_no, record.trigger, _aware(record.started_at), _aware(record.finished_at), record.outcome, record.http_status, record.provider_request_id, record.error_code)

    def _delivery_view(self, session: Session, record: NotificationDeliveryRecord, *, details: bool = False) -> DeliveryView:
        target = session.get(NotificationRouteTargetRecord, record.route_target_id)
        if target is None:
            raise RuntimeError("NOTIFICATION_DELIVERY_TARGET_MISSING")
        attempts = tuple(self._attempt_view(item) for item in session.scalars(select(NotificationAttemptRecord).where(NotificationAttemptRecord.delivery_id == record.id).order_by(NotificationAttemptRecord.attempt_no))) if details else ()
        return DeliveryView(
            record.id, record.event_key, record.incident_id, record.occurrence_no,
            record.route_id, record.route_target_id, target.channel_id, target.channel_name,
            target.provider, record.event_type, record.state, record.attempt_count,
            _aware(record.scheduled_at), _aware(record.next_attempt_at),
            record.suppression_reason, _object(record.payload_snapshot_json), _aware(record.succeeded_at), attempts,
        )

    def list_deliveries(self, *, state: str | None = None, event_type: str | None = None, channel_id: str | None = None, incident_id: int | None = None, before_id: int | None = None, limit: int = 100) -> tuple[DeliveryView, ...]:
        with self._sessions() as session:
            statement = select(NotificationDeliveryRecord).order_by(NotificationDeliveryRecord.id.desc())
            if state:
                statement = statement.where(NotificationDeliveryRecord.state == state)
            if event_type:
                statement = statement.where(NotificationDeliveryRecord.event_type == event_type)
            if channel_id is not None:
                statement = statement.join(
                    NotificationRouteTargetRecord,
                    NotificationDeliveryRecord.route_target_id == NotificationRouteTargetRecord.id,
                ).where(NotificationRouteTargetRecord.channel_id == channel_id)
            if incident_id is not None:
                statement = statement.where(NotificationDeliveryRecord.incident_id == incident_id)
            if before_id is not None:
                statement = statement.where(NotificationDeliveryRecord.id < before_id)
            rows = tuple(session.scalars(statement.limit(min(max(limit, 1), 500))))
            return tuple(self._delivery_view(session, item) for item in rows)

    def get_delivery(self, delivery_id: int) -> DeliveryView:
        with self._sessions() as session:
            record = session.get(NotificationDeliveryRecord, delivery_id)
            if record is None:
                raise LookupError("NOTIFICATION_DELIVERY_NOT_FOUND")
            return self._delivery_view(session, record, details=True)

    def retry_delivery(self, delivery_id: int, *, now: datetime) -> DeliveryView:
        with self._sessions.begin() as session:
            record = session.get(NotificationDeliveryRecord, delivery_id)
            if record is None:
                raise LookupError("NOTIFICATION_DELIVERY_NOT_FOUND")
            if record.state != DeliveryState.PERMANENTLY_FAILED.value:
                raise RuntimeError("NOTIFICATION_DELIVERY_NOT_RETRYABLE")
            route = session.get(NotificationRouteRecord, record.route_id)
            if route is None or route.status == "TERMINATED":
                raise RuntimeError("NOTIFICATION_DELIVERY_LIFECYCLE_ENDED")
            record.state = DeliveryState.PENDING.value
            record.next_attempt_at = _stored(now)
            record.next_attempt_trigger = "MANUAL"
            record.suppression_reason = None
            record.updated_at = _stored(now)
            session.flush()
            return self._delivery_view(session, record, details=True)

    def get_incident_notification(self, incident_id: int) -> IncidentNotificationView:
        with self._sessions() as session:
            incident = session.get(IncidentRecord, incident_id)
            if incident is None:
                raise LookupError("INCIDENT_NOT_FOUND")
            route = session.scalar(
                select(NotificationRouteRecord).where(
                    NotificationRouteRecord.incident_id == incident_id,
                    NotificationRouteRecord.occurrence_no == incident.occurrence_no,
                )
            )
            if route is None:
                return IncidentNotificationView(
                    incident_id, incident.occurrence_no, None, (), ()
                )
            targets = tuple(
                RouteTargetView(
                    item.id,
                    item.channel_id,
                    item.channel_name,
                    item.provider,
                    item.channel_revision_no,
                    _aware(item.opened_success_at),
                    _aware(item.last_success_at),
                )
                for item in session.scalars(
                    select(NotificationRouteTargetRecord)
                    .where(NotificationRouteTargetRecord.route_id == route.id)
                    .order_by(NotificationRouteTargetRecord.id)
                )
            )
            deliveries = tuple(
                self._delivery_view(session, item)
                for item in session.scalars(
                    select(NotificationDeliveryRecord)
                    .where(NotificationDeliveryRecord.route_id == route.id)
                    .order_by(NotificationDeliveryRecord.created_at, NotificationDeliveryRecord.id)
                )
            )
            return IncidentNotificationView(
                incident_id,
                incident.occurrence_no,
                RouteView(
                    route.id,
                    route.status,
                    route.policy_name,
                    route.policy_version,
                    route.repeat_interval_seconds,
                    _aware(route.next_reminder_at),
                    route.termination_reason,
                ),
                targets,
                deliveries,
            )
