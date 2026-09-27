"""SQLAlchemy 2 adapter for the isolated Incident Operations Sources & Alerting slice."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    delete,
    func,
    select,
    update,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app.application.sources import (
    AlertView,
    ApplyResult,
    EndpointDraft,
    DueSource,
    EndpointView,
    IncidentView,
    LabelCatalogItem,
    RulePreview,
    RulePreviewGroup,
    RulePreviewSource,
    RuleView,
    SourceDraft,
    SourceAuditView,
    SourceNoiseObserver,
    SourceView,
    WatchdogClusterView,
)
from app.platform.persistence.codecs import (
    aware_utc as _aware,
    stored_utc as _stored,
    stringified_json as _json,
)
from app.application.incidents import SourceIncidentReconciler
from app.domains.alerting.models import (
    AggregationRule,
    Matcher,
    MatcherOperator,
    NormalizedAlert,
    choose_aggregation,
    normalize_alert,
)
from app.domains.sources.models import (
    CollectionOutcome,
    EndpointSnapshot,
    PollCompleteness,
    SourceSnapshot,
    SourceState,
    WatchdogConfig,
)
from app.domains.noise.models import validate_grouping_window
from app.platform.persistence.database import SessionFactory

UTC = timezone.utc
SEVERITY_RANK = {"critical": 4, "warning": 3, "info": 2, "unknown": 1}
SOURCE_STATE_RANK = {"FIRING": 4, "PENDING_RESOLUTION": 3, "UNKNOWN": 2, "RECOVERED": 1}


class Base(DeclarativeBase):
    pass


class SourceRecord(Base):
    __tablename__ = "event_source"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    poll_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    resolution_grace_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    max_parallel_endpoints: Mapped[int] = mapped_column(Integer, nullable=False)
    watchdog_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    watchdog_alertname: Mapped[str] = mapped_column(String(128), nullable=False)
    watchdog_identity_label: Mapped[str] = mapped_column(String(128), nullable=False)
    watchdog_missing_after_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class EndpointRecord(Base):
    __tablename__ = "source_endpoint"
    __table_args__ = (
        UniqueConstraint("source_id", "canonical_url", name="uq_source_endpoint_url"),
    )

    source_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("event_source.id", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    canonical_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    auth_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    username: Mapped[str] = mapped_column(String(256), nullable=False)
    secret_envelope: Mapped[str | None] = mapped_column(Text)
    timeout_seconds: Mapped[float] = mapped_column(Float, nullable=False)


class SourceAuditRecord(Base):
    __tablename__ = "source_audit"
    __table_args__ = (
        Index("ix_source_audit_source_sequence", "source_id", "sequence"),
    )

    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(String(128), ForeignKey("event_source.id"))
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    source_version: Mapped[int] = mapped_column(Integer, nullable=False)
    changed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class PollRunRecord(Base):
    __tablename__ = "source_poll_run"
    __table_args__ = (
        Index("ix_source_poll_run_source_started", "source_id", "started_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(String(128), ForeignKey("event_source.id"))
    source_version: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    finished_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    completeness: Mapped[str] = mapped_column(String(16), nullable=False)
    endpoint_total: Mapped[int] = mapped_column(Integer, nullable=False)
    endpoint_succeeded: Mapped[int] = mapped_column(Integer, nullable=False)
    alert_count: Mapped[int] = mapped_column(Integer, nullable=False)
    safe_error_codes_json: Mapped[str] = mapped_column(Text, nullable=False)


class EndpointPollResultRecord(Base):
    __tablename__ = "endpoint_poll_result"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    poll_run_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("source_poll_run.id", ondelete="CASCADE")
    )
    endpoint_position: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    alert_count: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    safe_error_code: Mapped[str | None] = mapped_column(String(96))


class AggregationRuleRecord(Base):
    __tablename__ = "aggregation_rule"
    __table_args__ = (
        Index("ix_aggregation_rule_order", "enabled", "priority", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    matchers_json: Mapped[str] = mapped_column(Text, nullable=False)
    group_by_labels_json: Mapped[str] = mapped_column(Text, nullable=False)
    source_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    grouping_window_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class IncidentRecord(Base):
    __tablename__ = "incident"
    __table_args__ = (
        UniqueConstraint("source_id", "group_key", name="uq_incident_group"),
        Index("ix_incident_source_state", "source_id", "source_state", "updated_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(String(128), ForeignKey("event_source.id"))
    group_key: Mapped[str] = mapped_column(String(512), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    source_state: Mapped[str] = mapped_column(String(24), nullable=False)
    freshness_state: Mapped[str] = mapped_column(String(16), nullable=False)
    handling_state: Mapped[str] = mapped_column(String(24), nullable=False)
    handling_version: Mapped[int] = mapped_column(Integer, nullable=False)
    aggregation_rule_id: Mapped[int | None] = mapped_column(Integer)
    aggregation_rule_version: Mapped[int | None] = mapped_column(Integer)
    group_labels_json: Mapped[str] = mapped_column(Text, nullable=False)
    missing_labels_json: Mapped[str] = mapped_column(Text, nullable=False)
    occurrence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    occurrence_started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    change_version: Mapped[int] = mapped_column(Integer, nullable=False)
    change_origin: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class AlertRecord(Base):
    __tablename__ = "alert"
    __table_args__ = (
        UniqueConstraint("source_id", "upstream_fingerprint", name="uq_alert_identity"),
        Index("ix_alert_source_state", "source_id", "source_state", "last_seen_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(String(128), ForeignKey("event_source.id"))
    upstream_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    alertname: Mapped[str] = mapped_column(String(256), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    cluster: Mapped[str] = mapped_column(String(256), nullable=False)
    labels_json: Mapped[str] = mapped_column(Text, nullable=False)
    annotations_json: Mapped[str] = mapped_column(Text, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)
    origin: Mapped[str] = mapped_column(String(16), nullable=False)
    evidence_completeness: Mapped[str] = mapped_column(String(24), nullable=False)
    source_state: Mapped[str] = mapped_column(String(24), nullable=False)
    missing_since_at: Mapped[datetime | None] = mapped_column(DateTime)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    endpoint_positions_json: Mapped[str] = mapped_column(Text, nullable=False)
    incident_id: Mapped[int] = mapped_column(Integer, ForeignKey("incident.id"))


class WatchdogClusterRecord(Base):
    __tablename__ = "watchdog_cluster"
    __table_args__ = (
        UniqueConstraint("source_id", "identity_value", name="uq_watchdog_cluster_identity"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(String(128), ForeignKey("event_source.id"))
    identity_value: Mapped[str] = mapped_column(String(256), nullable=False)
    inventory_state: Mapped[str] = mapped_column(String(16), nullable=False)
    last_observed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


def _load_object(value: str) -> dict[str, str]:
    raw = json.loads(value)
    if not isinstance(raw, dict):
        raise RuntimeError("stored JSON object is invalid")
    return {str(key): str(item) for key, item in raw.items()}


def _load_strings(value: str) -> tuple[str, ...]:
    raw = json.loads(value)
    if not isinstance(raw, list):
        raise RuntimeError("stored JSON list is invalid")
    return tuple(str(item) for item in raw)


def _validate_draft(draft: SourceDraft) -> None:
    if not draft.id or len(draft.id) > 128:
        raise ValueError("source id must contain 1-128 characters")
    if not draft.name.strip() or len(draft.name.strip()) > 120:
        raise ValueError("source name must contain 1-120 characters")
    if not 1 <= len(draft.endpoints) <= 8:
        raise ValueError("source must contain 1-8 endpoints")
    positions = {item.position for item in draft.endpoints}
    if len(positions) != len(draft.endpoints) or any(not 0 <= item < 8 for item in positions):
        raise ValueError("endpoint positions must be unique values from 0 to 7")
    if not any(item.enabled for item in draft.endpoints):
        raise ValueError("source must contain an enabled endpoint")
    for endpoint in draft.endpoints:
        parts = urlsplit(endpoint.canonical_url.strip())
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or len(endpoint.canonical_url) > 2048
        ):
            raise ValueError("endpoint URL must be absolute HTTP(S) without credentials/query")
        if endpoint.auth_kind not in {"NONE", "BEARER", "BASIC"}:
            raise ValueError("endpoint auth kind is unsupported")
        if endpoint.auth_kind == "BASIC" and not endpoint.username.strip():
            raise ValueError("BASIC auth requires a username")
        if endpoint.secret_action not in {"KEEP", "REPLACE", "CLEAR"}:
            raise ValueError("endpoint secret action is invalid")
    if not 5 <= draft.poll_interval_seconds <= 3600:
        raise ValueError("poll interval is outside its allowed range")
    if not 0 <= draft.resolution_grace_seconds <= 86400:
        raise ValueError("resolution grace is outside its allowed range")
    if not 1 <= draft.max_parallel_endpoints <= 8:
        raise ValueError("endpoint concurrency is outside its allowed range")


def _validate_rule(
    name: str,
    priority: int,
    matchers: Sequence[tuple[str, str, str]],
    group_by_labels: Sequence[str],
) -> tuple[Matcher, ...]:
    if not name.strip() or len(name.strip()) > 120:
        raise ValueError("rule name must contain 1-120 characters")
    if not 0 <= priority <= 1_000_000:
        raise ValueError("rule priority is outside its allowed range")
    if len(matchers) > 20 or not 1 <= len(group_by_labels) <= 20:
        raise ValueError("rule matcher or group-label count is outside its allowed range")
    parsed: list[Matcher] = []
    label_pattern = re.compile(r"^[A-Za-z_][A-Za-z0-9_:.\-]{0,127}$")
    for label, operator, value in matchers:
        if label == "environment" or not label_pattern.fullmatch(label):
            raise ValueError("rule matcher label is invalid")
        parsed_operator = MatcherOperator(operator)
        if parsed_operator in {MatcherOperator.REGEX, MatcherOperator.NOT_REGEX}:
            re.compile(value)
        parsed.append(Matcher(label, parsed_operator, value))
    if len(set(group_by_labels)) != len(group_by_labels):
        raise ValueError("group labels must be unique")
    if any(label == "environment" or not label_pattern.fullmatch(label) for label in group_by_labels):
        raise ValueError("group label is invalid")
    return tuple(parsed)


class SqlAlchemySourceStore:
    """Transaction-owning adapter; no network client is reachable from here."""

    def __init__(
        self,
        sessions: SessionFactory,
        *,
        decrypt_secret: Callable[[str], str] | None = None,
        encrypt_secret: Callable[[str], str] | None = None,
        incident_reconciler: SourceIncidentReconciler | None = None,
        noise_observer: SourceNoiseObserver | None = None,
    ) -> None:
        self._sessions = sessions
        self._decrypt_secret = decrypt_secret
        self._encrypt_secret = encrypt_secret
        self._incident_reconciler = incident_reconciler
        self._noise_observer = noise_observer

    def _resolve_secret(
        self,
        endpoint: EndpointDraft,
        existing: str | None,
    ) -> str | None:
        if endpoint.secret_envelope is not None:
            return endpoint.secret_envelope
        if endpoint.secret_action == "KEEP":
            resolved = existing
        elif endpoint.secret_action == "CLEAR":
            resolved = None
        else:
            if not endpoint.secret_value:
                raise ValueError("REPLACE requires a non-empty endpoint secret")
            if self._encrypt_secret is None:
                raise RuntimeError("SOURCE_SECRET_UNAVAILABLE")
            resolved = self._encrypt_secret(endpoint.secret_value)
        if endpoint.auth_kind in {"BEARER", "BASIC"} and resolved is None:
            raise ValueError("selected endpoint auth requires a secret")
        return resolved

    def create_source(self, draft: SourceDraft, *, now: datetime) -> SourceView:
        _validate_draft(draft)
        with self._sessions.begin() as session:
            session.add(
                SourceRecord(
                    id=draft.id,
                    name=draft.name.strip(),
                    state=SourceState.ENABLED.value,
                    version=1,
                    poll_interval_seconds=draft.poll_interval_seconds,
                    resolution_grace_seconds=draft.resolution_grace_seconds,
                    max_parallel_endpoints=draft.max_parallel_endpoints,
                    watchdog_enabled=draft.watchdog_enabled,
                    watchdog_alertname=draft.watchdog_alertname,
                    watchdog_identity_label=draft.watchdog_identity_label,
                    watchdog_missing_after_seconds=draft.watchdog_missing_after_seconds,
                    created_at=_stored(now),
                    updated_at=_stored(now),
                )
            )
            # The mappings deliberately expose no relationship collection;
            # make the FK write order explicit inside this UoW.
            session.flush()
            session.add(
                SourceAuditRecord(
                    source_id=draft.id,
                    action="CREATED",
                    source_version=1,
                    changed_at=_stored(now),
                )
            )
            for endpoint in draft.endpoints:
                envelope = self._resolve_secret(endpoint, None)
                session.add(
                    EndpointRecord(
                        source_id=draft.id,
                        position=endpoint.position,
                        canonical_url=endpoint.canonical_url.rstrip("/"),
                        enabled=endpoint.enabled,
                        auth_kind=endpoint.auth_kind,
                        username=endpoint.username,
                        secret_envelope=envelope,
                        timeout_seconds=endpoint.timeout_seconds,
                    )
                )
            session.flush()
            source = session.get(SourceRecord, draft.id)
            if source is None:
                raise RuntimeError("created source disappeared")
            return self._source_view(session, source)

    def update_source(
        self,
        source_id: str,
        draft: SourceDraft,
        *,
        expected_version: int,
        now: datetime,
    ) -> SourceView:
        if draft.id != source_id:
            raise ValueError("source identity cannot change")
        _validate_draft(draft)
        with self._sessions.begin() as session:
            source = session.get(SourceRecord, source_id)
            if source is None:
                raise LookupError("SOURCE_NOT_FOUND")
            if source.version != expected_version:
                raise FileExistsError("SOURCE_VERSION_CONFLICT")
            if source.state == SourceState.ARCHIVED.value:
                raise RuntimeError("SOURCE_ARCHIVED")
            source.name = draft.name.strip()
            source.poll_interval_seconds = draft.poll_interval_seconds
            source.resolution_grace_seconds = draft.resolution_grace_seconds
            source.max_parallel_endpoints = draft.max_parallel_endpoints
            source.watchdog_enabled = draft.watchdog_enabled
            source.watchdog_alertname = draft.watchdog_alertname
            source.watchdog_identity_label = draft.watchdog_identity_label
            source.watchdog_missing_after_seconds = draft.watchdog_missing_after_seconds
            source.version += 1
            source.updated_at = _stored(now)
            existing_secrets = {
                item.position: item.secret_envelope
                for item in session.scalars(
                    select(EndpointRecord).where(EndpointRecord.source_id == source_id)
                )
            }
            session.execute(delete(EndpointRecord).where(EndpointRecord.source_id == source_id))
            for endpoint in draft.endpoints:
                envelope = self._resolve_secret(
                    endpoint,
                    existing_secrets.get(endpoint.position),
                )
                session.add(
                    EndpointRecord(
                        source_id=source_id,
                        position=endpoint.position,
                        canonical_url=endpoint.canonical_url.rstrip("/"),
                        enabled=endpoint.enabled,
                        auth_kind=endpoint.auth_kind,
                        username=endpoint.username,
                        secret_envelope=envelope,
                        timeout_seconds=endpoint.timeout_seconds,
                    )
                )
            session.add(
                SourceAuditRecord(
                    source_id=source_id,
                    action="UPDATED",
                    source_version=source.version,
                    changed_at=_stored(now),
                )
            )
            session.flush()
            return self._source_view(session, source)

    def _source_view(self, session: Session, source: SourceRecord) -> SourceView:
        latest = session.scalar(
            select(PollRunRecord)
            .where(PollRunRecord.source_id == source.id)
            .order_by(PollRunRecord.started_at.desc(), PollRunRecord.id.desc())
            .limit(1)
        )
        endpoints = tuple(
            EndpointView(
                position=item.position,
                canonical_url=item.canonical_url,
                enabled=item.enabled,
                auth_kind=item.auth_kind,
                username=item.username,
                secret_configured=item.secret_envelope is not None,
                timeout_seconds=item.timeout_seconds,
            )
            for item in session.scalars(
                select(EndpointRecord)
                .where(EndpointRecord.source_id == source.id)
                .order_by(EndpointRecord.position)
            )
        )
        return SourceView(
            id=source.id,
            name=source.name,
            state=source.state,
            version=source.version,
            poll_interval_seconds=source.poll_interval_seconds,
            resolution_grace_seconds=source.resolution_grace_seconds,
            max_parallel_endpoints=source.max_parallel_endpoints,
            watchdog_enabled=source.watchdog_enabled,
            watchdog_alertname=source.watchdog_alertname,
            watchdog_identity_label=source.watchdog_identity_label,
            watchdog_missing_after_seconds=source.watchdog_missing_after_seconds,
            endpoints=endpoints,
            created_at=_aware(source.created_at),
            updated_at=_aware(source.updated_at),
            last_poll_at=None if latest is None else _aware(latest.finished_at),
            last_poll_completeness=None if latest is None else latest.completeness,
            last_poll_safe_error_codes=(
                ()
                if latest is None
                else tuple(cast(list[str], json.loads(latest.safe_error_codes_json)))
            ),
        )

    def list_sources(self, *, include_archived: bool = False) -> tuple[SourceView, ...]:
        with self._sessions() as session:
            statement = select(SourceRecord)
            if not include_archived:
                statement = statement.where(
                    SourceRecord.state != SourceState.ARCHIVED.value
                )
            return tuple(
                self._source_view(session, item)
                for item in session.scalars(
                    statement.order_by(SourceRecord.name, SourceRecord.id)
                )
            )

    def due_sources(self, *, now: datetime) -> tuple[DueSource, ...]:
        with self._sessions() as session:
            due: list[DueSource] = []
            for source in session.scalars(
                select(SourceRecord)
                .where(SourceRecord.state == SourceState.ENABLED.value)
                .order_by(SourceRecord.id)
            ):
                latest = session.scalar(
                    select(PollRunRecord.started_at)
                    .where(PollRunRecord.source_id == source.id)
                    .order_by(PollRunRecord.started_at.desc(), PollRunRecord.id.desc())
                    .limit(1)
                )
                latest_utc = _aware(latest)
                if latest_utc is not None and (
                    now.astimezone(UTC) - latest_utc
                ).total_seconds() < source.poll_interval_seconds:
                    continue
                due.append(
                    DueSource(
                        source_id=source.id,
                        source_version=source.version,
                        poll_interval_seconds=source.poll_interval_seconds,
                    )
                )
            return tuple(due)

    def get_source(self, source_id: str) -> SourceView | None:
        with self._sessions() as session:
            source = session.get(SourceRecord, source_id)
            return None if source is None else self._source_view(session, source)

    def list_alerts(self, *, source_id: str | None = None) -> tuple[AlertView, ...]:
        with self._sessions() as session:
            statement = select(AlertRecord)
            if source_id is not None:
                statement = statement.where(AlertRecord.source_id == source_id)
            records = session.scalars(
                statement.order_by(AlertRecord.source_id, AlertRecord.id)
            )
            return tuple(
                AlertView(
                    id=item.id,
                    source_id=item.source_id,
                    upstream_fingerprint=item.upstream_fingerprint,
                    alertname=item.alertname,
                    severity=item.severity,
                    cluster=item.cluster,
                    source_state=item.source_state,
                    incident_id=item.incident_id,
                    labels=cast(dict[str, str], json.loads(item.labels_json)),
                    annotations=cast(dict[str, str], json.loads(item.annotations_json)),
                    starts_at=_aware(item.starts_at),
                    missing_since_at=_aware(item.missing_since_at),
                    origin=item.origin,
                    evidence_completeness=item.evidence_completeness,
                    last_seen_at=_aware(item.last_seen_at),
                )
                for item in records
            )

    def list_incidents(
        self, *, source_id: str | None = None
    ) -> tuple[IncidentView, ...]:
        with self._sessions() as session:
            member_counts = select(
                AlertRecord.incident_id.label("incident_id"),
                func.count(AlertRecord.id).label("member_count"),
            )
            if source_id is not None:
                member_counts = member_counts.where(
                    AlertRecord.source_id == source_id
                )
            member_counts_subquery = member_counts.group_by(
                AlertRecord.incident_id
            ).subquery()
            statement = (
                select(
                    IncidentRecord.id,
                    IncidentRecord.source_id,
                    IncidentRecord.group_key,
                    IncidentRecord.title,
                    IncidentRecord.severity,
                    IncidentRecord.source_state,
                    IncidentRecord.freshness_state,
                    IncidentRecord.handling_state,
                    IncidentRecord.handling_version,
                    IncidentRecord.occurrence_no,
                    IncidentRecord.occurrence_started_at,
                    IncidentRecord.updated_at,
                    IncidentRecord.aggregation_rule_id,
                    IncidentRecord.missing_labels_json,
                    func.coalesce(member_counts_subquery.c.member_count, 0).label(
                        "member_count"
                    ),
                    SourceRecord.name.label("source_name"),
                    AggregationRuleRecord.name.label("aggregation_rule_name"),
                )
                .outerjoin(
                    member_counts_subquery,
                    member_counts_subquery.c.incident_id == IncidentRecord.id,
                )
                .outerjoin(SourceRecord, SourceRecord.id == IncidentRecord.source_id)
                .outerjoin(
                    AggregationRuleRecord,
                    AggregationRuleRecord.id == IncidentRecord.aggregation_rule_id,
                )
            )
            if source_id is not None:
                statement = statement.where(IncidentRecord.source_id == source_id)
            records = session.execute(
                statement.order_by(IncidentRecord.source_id, IncidentRecord.id)
            )

            def aggregation_status(
                aggregation_rule_id: int | None,
                missing_labels_json: str,
            ) -> Literal[
                "matched", "unmatched", "missing_labels"
            ]:
                if aggregation_rule_id is None:
                    return "unmatched"
                if cast(list[object], json.loads(missing_labels_json)):
                    return "missing_labels"
                return "matched"

            return tuple(
                IncidentView(
                    id=item.id,
                    source_id=item.source_id,
                    group_key=item.group_key,
                    title=item.title,
                    severity=item.severity,
                    source_state=item.source_state,
                    freshness_state=item.freshness_state,
                    handling_state=item.handling_state,
                    handling_version=item.handling_version,
                    occurrence_no=item.occurrence_no,
                    occurrence_started_at=cast(datetime, _aware(item.occurrence_started_at)),
                    updated_at=cast(datetime, _aware(item.updated_at)),
                    member_count=int(item.member_count),
                    aggregation_rule_id=item.aggregation_rule_id,
                    source_name=item.source_name or item.source_id,
                    aggregation_rule_name=item.aggregation_rule_name,
                    aggregation_status=aggregation_status(
                        item.aggregation_rule_id,
                        item.missing_labels_json,
                    ),
                )
                for item in records
            )

    def list_rules(self) -> tuple[RuleView, ...]:
        with self._sessions() as session:
            records = session.scalars(
                select(AggregationRuleRecord).order_by(
                    AggregationRuleRecord.priority,
                    AggregationRuleRecord.id,
                )
            )
            return tuple(
                RuleView(
                    item.id,
                    item.name,
                    item.priority,
                    item.enabled,
                    item.version,
                    tuple(
                        (str(value[0]), str(value[1]), str(value[2]))
                        for value in cast(list[list[object]], json.loads(item.matchers_json))
                    ),
                    tuple(cast(list[str], json.loads(item.group_by_labels_json))),
                    tuple(cast(list[str], json.loads(item.source_ids_json))),
                    item.grouping_window_seconds,
                    _aware(item.created_at),
                    _aware(item.updated_at),
                )
                for item in records
            )

    def label_catalog(
        self,
        historical_series: tuple[dict[str, str], ...] = (),
    ) -> tuple[LabelCatalogItem, ...]:
        with self._sessions() as session:
            watchdog_names = set(
                session.scalars(select(SourceRecord.watchdog_alertname)).all()
            )
            current_rows = list(
                session.scalars(select(AlertRecord).order_by(AlertRecord.id))
            )
        rows: list[tuple[str, dict[str, str]]] = []
        for record in current_rows:
            if record.alertname in watchdog_names:
                continue
            labels = cast(dict[str, str], json.loads(record.labels_json))
            labels.setdefault("alertname", record.alertname)
            labels.setdefault("severity", record.severity)
            labels.setdefault("cluster", record.cluster)
            rows.append(("current", labels))
        for historical_labels in historical_series:
            labels = {
                str(key): str(value) for key, value in historical_labels.items()
            }
            if labels.get("alertname") in watchdog_names:
                continue
            rows.append(("history", labels))
        excluded = {
            "__name__",
            "alertstate",
            "__datasource_type__",
            "__datasource_uid__",
            "environment",
            "source_id",
        }
        total = len(rows)
        present: dict[str, int] = {}
        values: dict[str, set[str]] = {}
        sources: dict[str, set[str]] = {}
        for origin, labels in rows:
            for name, value in labels.items():
                if name in excluded or not str(value).strip():
                    continue
                present[name] = present.get(name, 0) + 1
                values.setdefault(name, set()).add(str(value))
                sources.setdefault(name, set()).add(origin)
        return tuple(
            LabelCatalogItem(
                name=name,
                coverage=0.0 if total == 0 else count / total,
                present_count=count,
                total_count=total,
                distinct_count=len(values[name]),
                sample_values=tuple(sorted(values[name])[:10]),
                sources=tuple(
                    origin
                    for origin in ("current", "history")
                    if origin in sources[name]
                ),
            )
            for name, count in sorted(present.items())
        )

    def list_source_audit(self, source_id: str) -> tuple[SourceAuditView, ...]:
        with self._sessions() as session:
            if session.get(SourceRecord, source_id) is None:
                raise LookupError("SOURCE_NOT_FOUND")
            return tuple(
                SourceAuditView(
                    sequence=item.sequence,
                    source_id=item.source_id,
                    action=item.action,
                    source_version=item.source_version,
                    changed_at=_aware(item.changed_at),
                )
                for item in session.scalars(
                    select(SourceAuditRecord)
                    .where(SourceAuditRecord.source_id == source_id)
                    .order_by(SourceAuditRecord.sequence)
                )
            )

    def _secret(self, envelope: str | None) -> str:
        if envelope is None:
            return ""
        if self._decrypt_secret is None:
            raise RuntimeError("SOURCE_SECRET_UNAVAILABLE")
        return self._decrypt_secret(envelope)

    def load_snapshot(self, source_id: str, *, expected_version: int) -> SourceSnapshot:
        return self._load_snapshot(
            source_id,
            expected_version=expected_version,
            require_enabled=True,
        )

    def load_test_snapshot(
        self, source_id: str, *, expected_version: int
    ) -> SourceSnapshot:
        return self._load_snapshot(
            source_id,
            expected_version=expected_version,
            require_enabled=False,
        )

    def record_test_result(
        self,
        source_id: str,
        *,
        expected_version: int,
        completeness: PollCompleteness,
        tested_at: datetime,
    ) -> None:
        with self._sessions.begin() as session:
            source = session.get(SourceRecord, source_id)
            if source is None:
                raise LookupError("SOURCE_NOT_FOUND")
            if source.version != expected_version:
                raise FileExistsError("SOURCE_VERSION_CONFLICT")
            session.add(
                SourceAuditRecord(
                    source_id=source_id,
                    action=f"TEST_{completeness.value}",
                    source_version=source.version,
                    changed_at=_stored(tested_at),
                )
            )

    def _load_snapshot(
        self,
        source_id: str,
        *,
        expected_version: int,
        require_enabled: bool,
    ) -> SourceSnapshot:
        with self._sessions() as session:
            source = session.get(SourceRecord, source_id)
            if source is None:
                raise LookupError("SOURCE_NOT_FOUND")
            if require_enabled and source.state != SourceState.ENABLED.value:
                raise RuntimeError("SOURCE_NOT_POLLABLE")
            if source.state == SourceState.ARCHIVED.value:
                raise RuntimeError("SOURCE_ARCHIVED")
            if source.version != expected_version:
                raise FileExistsError("SOURCE_VERSION_CONFLICT")
            endpoints = list(
                session.scalars(
                    select(EndpointRecord)
                    .where(
                        EndpointRecord.source_id == source_id,
                        EndpointRecord.enabled.is_(True),
                    )
                    .order_by(EndpointRecord.position)
                )
            )
            return SourceSnapshot(
                source_id=source.id,
                source_name=source.name,
                version=source.version,
                state=SourceState(source.state),
                poll_interval_seconds=source.poll_interval_seconds,
                resolution_grace_seconds=source.resolution_grace_seconds,
                max_parallel_endpoints=source.max_parallel_endpoints,
                endpoints=tuple(
                    EndpointSnapshot(
                        source_id=source.id,
                        source_version=source.version,
                        position=item.position,
                        canonical_url=item.canonical_url,
                        auth_kind=item.auth_kind,
                        username=item.username,
                        secret=self._secret(item.secret_envelope),
                        timeout_seconds=item.timeout_seconds,
                    )
                    for item in endpoints
                ),
                watchdog=WatchdogConfig(
                    enabled=source.watchdog_enabled,
                    alertname=source.watchdog_alertname,
                    identity_label=source.watchdog_identity_label,
                    missing_after_seconds=source.watchdog_missing_after_seconds,
                ),
            )

    def disable_source(self, source_id: str, *, expected_version: int, now: datetime) -> None:
        self.set_source_state(
            source_id,
            target=SourceState.DISABLED,
            expected_version=expected_version,
            now=now,
        )

    def set_source_state(
        self,
        source_id: str,
        *,
        target: SourceState,
        expected_version: int,
        now: datetime,
    ) -> SourceView:
        with self._sessions.begin() as session:
            source = session.get(SourceRecord, source_id)
            if source is None:
                raise LookupError("SOURCE_NOT_FOUND")
            if source.version != expected_version:
                raise FileExistsError("SOURCE_VERSION_CONFLICT")
            if source.state == SourceState.ARCHIVED.value:
                raise RuntimeError("SOURCE_ARCHIVED")
            if target not in {
                SourceState.ENABLED,
                SourceState.DISABLED,
                SourceState.ARCHIVED,
            }:
                raise ValueError("unsupported source lifecycle target")
            source.state = target.value
            source.version += 1
            source.updated_at = _stored(now)
            if target is not SourceState.ENABLED:
                for incident in session.scalars(
                    select(IncidentRecord).where(IncidentRecord.source_id == source_id)
                ):
                    incident.freshness_state = "STALE"
                if self._incident_reconciler is not None:
                    self._incident_reconciler.mark_source_stale(
                        session,
                        source_id,
                        observed_at=now,
                    )
            session.add(
                SourceAuditRecord(
                    source_id=source_id,
                    action=target.value,
                    source_version=source.version,
                    changed_at=_stored(now),
                )
            )
            session.flush()
            return self._source_view(session, source)

    def add_expected_watchdog_cluster(
        self,
        source_id: str,
        identity_value: str,
        *,
        now: datetime,
    ) -> None:
        identity = identity_value.strip()
        if not identity or len(identity) > 256:
            raise ValueError("watchdog identity must contain 1-256 characters")
        with self._sessions.begin() as session:
            if session.get(SourceRecord, source_id) is None:
                raise LookupError("SOURCE_NOT_FOUND")
            existing = session.scalar(
                select(WatchdogClusterRecord).where(
                    WatchdogClusterRecord.source_id == source_id,
                    WatchdogClusterRecord.identity_value == identity,
                )
            )
            if existing is None:
                session.add(
                    WatchdogClusterRecord(
                        source_id=source_id,
                        identity_value=identity,
                        inventory_state="EXPECTED",
                        last_observed_at=None,
                        created_at=_stored(now),
                    )
                )
            elif existing.inventory_state == "IGNORED":
                existing.inventory_state = "EXPECTED"

    def set_watchdog_inventory_state(
        self,
        source_id: str,
        identity_value: str,
        *,
        inventory_state: str,
    ) -> None:
        if inventory_state not in {"DISCOVERED", "EXPECTED", "IGNORED"}:
            raise ValueError("unsupported watchdog inventory state")
        with self._sessions.begin() as session:
            cluster = session.scalar(
                select(WatchdogClusterRecord).where(
                    WatchdogClusterRecord.source_id == source_id,
                    WatchdogClusterRecord.identity_value == identity_value,
                )
            )
            if cluster is None:
                raise LookupError("WATCHDOG_CLUSTER_NOT_FOUND")
            cluster.inventory_state = inventory_state

    def list_watchdog_clusters(
        self,
        source_id: str,
        *,
        now: datetime,
    ) -> tuple[WatchdogClusterView, ...]:
        with self._sessions() as session:
            source = session.get(SourceRecord, source_id)
            if source is None:
                raise LookupError("SOURCE_NOT_FOUND")
            latest = session.scalar(
                select(PollRunRecord)
                .where(PollRunRecord.source_id == source_id)
                .order_by(PollRunRecord.started_at.desc(), PollRunRecord.id.desc())
                .limit(1)
            )
            views: list[WatchdogClusterView] = []
            for item in session.scalars(
                select(WatchdogClusterRecord)
                .where(WatchdogClusterRecord.source_id == source_id)
                .order_by(WatchdogClusterRecord.identity_value)
            ):
                last_observed = _aware(item.last_observed_at)
                if (
                    item.inventory_state == "IGNORED"
                    or source.state != SourceState.ENABLED.value
                    or not source.watchdog_enabled
                ):
                    health = "NOT_MONITORED"
                elif latest is None:
                    health = "UNKNOWN"
                elif (
                    latest.completeness == PollCompleteness.PARTIAL.value
                    and last_observed is not None
                    and last_observed >= _aware(latest.started_at)
                ):
                    health = "HEALTHY"
                elif latest.completeness != PollCompleteness.COMPLETE.value:
                    health = "UNKNOWN"
                elif last_observed is not None and (
                    now.astimezone(UTC) - last_observed
                ).total_seconds() <= source.watchdog_missing_after_seconds:
                    health = "HEALTHY"
                else:
                    health = "MISSING"
                views.append(
                    WatchdogClusterView(
                        id=item.id,
                        source_id=item.source_id,
                        identity_value=item.identity_value,
                        inventory_state=item.inventory_state,
                        health_state=health,
                        last_observed_at=last_observed,
                    )
                )
            return tuple(views)

    def _rules(self, session: Session) -> tuple[AggregationRule, ...]:
        records = session.scalars(
            select(AggregationRuleRecord).where(AggregationRuleRecord.enabled.is_(True))
        ).all()
        values: list[AggregationRule] = []
        for item in records:
            raw_matchers = json.loads(item.matchers_json)
            if not isinstance(raw_matchers, list):
                raise RuntimeError("stored rule matchers are invalid")
            matchers = tuple(
                Matcher(
                    str(raw[0]),
                    MatcherOperator(str(raw[1])),
                    str(raw[2]),
                )
                for raw in raw_matchers
                if isinstance(raw, list) and len(raw) == 3
            )
            values.append(
                AggregationRule(
                    id=item.id,
                    name=item.name,
                    priority=item.priority,
                    enabled=item.enabled,
                    matchers=matchers,
                    group_by_labels=_load_strings(item.group_by_labels_json),
                    source_ids=_load_strings(item.source_ids_json),
                    version=item.version,
                    grouping_window_seconds=item.grouping_window_seconds,
                )
            )
        return tuple(sorted(values, key=lambda rule: (rule.priority, rule.id)))

    def _ensure_source_scope(
        self,
        session: Session,
        source_ids: Sequence[str],
    ) -> None:
        requested = set(source_ids)
        if len(requested) != len(source_ids) or len(requested) > 20:
            raise ValueError("source scope contains duplicates or too many sources")
        if not requested:
            return
        existing = set(
            session.scalars(
                select(SourceRecord.id).where(SourceRecord.id.in_(requested))
            )
        )
        if existing != requested:
            raise ValueError("source scope contains an unknown source")

    def _incident_for(
        self,
        session: Session,
        alert: NormalizedAlert,
        *,
        rules: tuple[AggregationRule, ...],
        observed_at: datetime,
        completeness: PollCompleteness,
    ) -> IncidentRecord:
        decision = choose_aggregation(alert, rules)
        incident = session.scalar(
            select(IncidentRecord).where(
                IncidentRecord.source_id == alert.source_id,
                IncidentRecord.group_key == decision.group_key,
            )
        )
        if incident is None:
            incident = IncidentRecord(
                source_id=alert.source_id,
                group_key=decision.group_key,
                title=decision.title,
                severity=alert.severity,
                source_state="FIRING",
                freshness_state=(
                    "FRESH" if completeness is PollCompleteness.COMPLETE else "UNKNOWN"
                ),
                handling_state="NEW",
                handling_version=1,
                aggregation_rule_id=decision.rule_id,
                aggregation_rule_version=decision.rule_version,
                group_labels_json=_json(decision.group_labels),
                missing_labels_json=_json(decision.missing_labels),
                occurrence_no=1,
                occurrence_started_at=_stored(observed_at),
                change_version=0,
                change_origin="LIVE_POLL",
                updated_at=_stored(observed_at),
            )
            session.add(incident)
            session.flush()
        elif incident.source_state == "RECOVERED":
            # A positive LIVE_POLL observation reopens an occurrence even when
            # another endpoint failed. Completeness gates absence, not presence.
            incident.occurrence_no += 1
            incident.occurrence_started_at = _stored(observed_at)
        incident.title = decision.title
        incident.aggregation_rule_id = decision.rule_id
        incident.aggregation_rule_version = decision.rule_version
        incident.group_labels_json = _json(decision.group_labels)
        incident.missing_labels_json = _json(decision.missing_labels)
        return incident

    def _upsert_alert(
        self,
        session: Session,
        alert: NormalizedAlert,
        *,
        endpoint_positions: tuple[int, ...],
        rules: tuple[AggregationRule, ...],
        observed_at: datetime,
        completeness: PollCompleteness,
    ) -> AlertRecord:
        incident = self._incident_for(
            session,
            alert,
            rules=rules,
            observed_at=observed_at,
            completeness=completeness,
        )
        record = session.scalar(
            select(AlertRecord).where(
                AlertRecord.source_id == alert.source_id,
                AlertRecord.upstream_fingerprint == alert.upstream_fingerprint,
            )
        )
        if record is None:
            record = AlertRecord(
                source_id=alert.source_id,
                upstream_fingerprint=alert.upstream_fingerprint,
                alertname=alert.alertname,
                severity=alert.severity,
                cluster=alert.cluster,
                labels_json=_json(alert.labels),
                annotations_json=_json(alert.annotations),
                raw_json=_json(alert.raw),
                origin="LIVE_POLL",
                evidence_completeness="COMPLETE",
                source_state="FIRING",
                missing_since_at=None,
                starts_at=None if alert.starts_at is None else _stored(alert.starts_at),
                last_seen_at=_stored(observed_at),
                endpoint_positions_json=_json(endpoint_positions),
                incident_id=incident.id,
            )
            session.add(record)
        else:
            record.alertname = alert.alertname
            record.severity = alert.severity
            record.cluster = alert.cluster
            record.labels_json = _json(alert.labels)
            record.annotations_json = _json(alert.annotations)
            record.raw_json = _json(alert.raw)
            record.origin = "LIVE_POLL"
            record.evidence_completeness = "COMPLETE"
            record.source_state = "FIRING"
            record.missing_since_at = None
            record.starts_at = None if alert.starts_at is None else _stored(alert.starts_at)
            record.last_seen_at = _stored(observed_at)
            record.endpoint_positions_json = _json(endpoint_positions)
            record.incident_id = incident.id
        return record

    def _record_watchdog(
        self,
        session: Session,
        *,
        snapshot: SourceSnapshot,
        raw: Mapping[str, object],
        observed_at: datetime,
    ) -> None:
        labels_raw = raw.get("labels")
        labels = labels_raw if isinstance(labels_raw, Mapping) else {}
        identity = str(labels.get(snapshot.watchdog.identity_label) or "__missing_identity__")
        cluster = session.scalar(
            select(WatchdogClusterRecord).where(
                WatchdogClusterRecord.source_id == snapshot.source_id,
                WatchdogClusterRecord.identity_value == identity,
            )
        )
        if cluster is None:
            cluster = WatchdogClusterRecord(
                source_id=snapshot.source_id,
                identity_value=identity,
                inventory_state="DISCOVERED",
                last_observed_at=_stored(observed_at),
                created_at=_stored(observed_at),
            )
            session.add(cluster)
        else:
            cluster.last_observed_at = _stored(observed_at)

    def _recompute_incidents(
        self,
        session: Session,
        *,
        source_id: str,
        observed_at: datetime,
        completeness: PollCompleteness,
    ) -> None:
        for incident in session.scalars(
            select(IncidentRecord).where(IncidentRecord.source_id == source_id)
        ):
            members = list(
                session.scalars(
                    select(AlertRecord).where(AlertRecord.incident_id == incident.id)
                )
            )
            if not members:
                continue
            state = max(members, key=lambda item: SOURCE_STATE_RANK[item.source_state]).source_state
            severity_candidates = [
                item.severity for item in members if item.source_state != "RECOVERED"
            ]
            severity = (
                max(severity_candidates, key=lambda item: SEVERITY_RANK.get(item, 0))
                if severity_candidates
                else "unknown"
            )
            changed = incident.source_state != state or incident.severity != severity
            incident.source_state = state
            incident.severity = severity
            if completeness is PollCompleteness.COMPLETE:
                incident.freshness_state = "FRESH"
            if changed:
                incident.updated_at = _stored(observed_at)

    def apply_collection(
        self,
        snapshot: SourceSnapshot,
        outcome: CollectionOutcome,
        *,
        observed_at: datetime,
    ) -> ApplyResult:
        with self._sessions() as session:
            try:
                claimed = session.execute(
                    update(SourceRecord)
                    .where(
                        SourceRecord.id == snapshot.source_id,
                        SourceRecord.state == SourceState.ENABLED.value,
                        SourceRecord.version == snapshot.version,
                    )
                    .values(updated_at=SourceRecord.updated_at)
                )
                if cast(Any, claimed).rowcount != 1:
                    session.rollback()
                    return ApplyResult(
                        completeness=outcome.completeness,
                        committed=False,
                        alerts_seen=len(outcome.alerts),
                        safe_error_code="SOURCE_CONFIG_CHANGED_DURING_POLL",
                    )
                run = PollRunRecord(
                    source_id=snapshot.source_id,
                    source_version=snapshot.version,
                    started_at=_stored(observed_at),
                    finished_at=_stored(datetime.now(UTC)),
                    completeness=outcome.completeness.value,
                    endpoint_total=len(outcome.endpoint_observations),
                    endpoint_succeeded=sum(
                        item.succeeded for item in outcome.endpoint_observations
                    ),
                    alert_count=len(outcome.alerts),
                    safe_error_codes_json=_json(outcome.safe_error_codes),
                )
                session.add(run)
                session.flush()
                for endpoint_observation in outcome.endpoint_observations:
                    session.add(
                        EndpointPollResultRecord(
                            poll_run_id=run.id,
                            endpoint_position=endpoint_observation.endpoint.position,
                            status=endpoint_observation.status,
                            alert_count=(
                                len(endpoint_observation.alerts)
                                if endpoint_observation.succeeded
                                else 0
                            ),
                            duration_ms=endpoint_observation.duration_ms,
                            safe_error_code=endpoint_observation.safe_error_code,
                        )
                    )
                if outcome.completeness is not PollCompleteness.FAILED:
                    before = (
                        self._incident_reconciler.snapshot(session, snapshot.source_id)
                        if self._incident_reconciler is not None
                        else {}
                    )
                    previous_alert_states = {
                        item.upstream_fingerprint: item.source_state
                        for item in session.scalars(
                            select(AlertRecord).where(
                                AlertRecord.source_id == snapshot.source_id
                            )
                        )
                    }
                    rules = self._rules(session)
                    observed: set[str] = set()
                    for merged_alert in outcome.alerts:
                        labels_raw = merged_alert.raw.get("labels")
                        labels = labels_raw if isinstance(labels_raw, Mapping) else {}
                        if str(labels.get("alertname") or "") == snapshot.watchdog.alertname:
                            if snapshot.watchdog.enabled:
                                self._record_watchdog(
                                    session,
                                    snapshot=snapshot,
                                    raw=merged_alert.raw,
                                    observed_at=observed_at,
                                )
                            continue
                        normalized = normalize_alert(
                            merged_alert.raw,
                            source_id=snapshot.source_id,
                        )
                        observed.add(normalized.upstream_fingerprint)
                        self._upsert_alert(
                            session,
                            normalized,
                            endpoint_positions=merged_alert.endpoint_positions,
                            rules=rules,
                            observed_at=observed_at,
                            completeness=outcome.completeness,
                        )
                    session.flush()
                    if outcome.completeness is PollCompleteness.COMPLETE:
                        for alert in session.scalars(
                            select(AlertRecord).where(AlertRecord.source_id == snapshot.source_id)
                        ):
                            if alert.upstream_fingerprint in observed or alert.source_state == "RECOVERED":
                                continue
                            missing_since = _aware(alert.missing_since_at)
                            if missing_since is None:
                                alert.source_state = "PENDING_RESOLUTION"
                                alert.missing_since_at = _stored(observed_at)
                            elif (
                                observed_at.astimezone(UTC) - missing_since
                            ).total_seconds() >= snapshot.resolution_grace_seconds:
                                alert.source_state = "RECOVERED"
                            else:
                                alert.source_state = "PENDING_RESOLUTION"
                    self._recompute_incidents(
                        session,
                        source_id=snapshot.source_id,
                        observed_at=observed_at,
                        completeness=outcome.completeness,
                    )
                    if (
                        outcome.completeness is PollCompleteness.COMPLETE
                        and self._noise_observer is not None
                    ):
                        self._noise_observer.observe_complete_collection_in_session(
                            session,
                            source_id=snapshot.source_id,
                            previous_alert_states=previous_alert_states,
                            previous_incidents=before,
                            observed_at=observed_at,
                        )
                    if self._incident_reconciler is not None:
                        self._incident_reconciler.reconcile_source_changes(
                            session,
                            before,
                            source_id=snapshot.source_id,
                            observed_at=observed_at,
                            completeness=outcome.completeness,
                        )
                session.commit()
                return ApplyResult(
                    completeness=outcome.completeness,
                    committed=True,
                    alerts_seen=len(outcome.alerts),
                )
            except Exception:
                session.rollback()
                raise

    def seed_alert_for_characterization(
        self,
        *,
        source_id: str,
        raw: Mapping[str, object],
        observed_at: datetime,
    ) -> None:
        with self._sessions.begin() as session:
            normalized = normalize_alert(raw, source_id=source_id)
            self._upsert_alert(
                session,
                normalized,
                endpoint_positions=(0,),
                rules=self._rules(session),
                observed_at=observed_at,
                completeness=PollCompleteness.COMPLETE,
            )
            session.flush()
            self._recompute_incidents(
                session,
                source_id=source_id,
                observed_at=observed_at,
                completeness=PollCompleteness.COMPLETE,
            )

    def preview_rule(
        self,
        *,
        rule_id: int | None = None,
        name: str,
        priority: int,
        enabled: bool,
        matchers: Sequence[tuple[str, str, str]],
        group_by_labels: Sequence[str],
        source_ids: Sequence[str],
        grouping_window_seconds: int = 30,
    ) -> RulePreview:
        parsed = _validate_rule(name, priority, matchers, group_by_labels)
        draft = AggregationRule(
            id=rule_id if rule_id is not None else 2_147_483_647,
            name=name,
            priority=priority,
            enabled=enabled,
            matchers=parsed,
            group_by_labels=tuple(group_by_labels),
            source_ids=tuple(source_ids),
            version=1,
            grouping_window_seconds=validate_grouping_window(grouping_window_seconds),
        )
        with self._sessions() as session:
            self._ensure_source_scope(session, source_ids)
            existing = tuple(
                item for item in self._rules(session) if item.id != rule_id
            )
            alerts = list(session.scalars(select(AlertRecord)))
            matcher_count = 0
            matcher_by_source: dict[str, int] = {}
            selected: list[tuple[str, str, str]] = []
            for record in alerts:
                raw = cast(Mapping[str, object], json.loads(record.raw_json))
                alert = normalize_alert(raw, source_id=record.source_id)
                if draft.applies_to(alert):
                    matcher_count += 1
                    matcher_by_source[record.source_id] = (
                        matcher_by_source.get(record.source_id, 0) + 1
                    )
                decision = choose_aggregation(alert, (*existing, draft))
                if decision.rule_id == draft.id:
                    selected.append(
                        (
                            record.source_id,
                            record.upstream_fingerprint,
                            decision.group_key,
                        )
                    )
            grouped: dict[tuple[str, str], list[str]] = {}
            for selected_source, fingerprint, group_key in selected:
                grouped.setdefault((selected_source, group_key), []).append(fingerprint)
            source_names = {
                item.id: item.name for item in session.scalars(select(SourceRecord))
            }
            source_rows = sorted(
                set(matcher_by_source) | {source_id for source_id, _fp, _key in selected}
            )
            return RulePreview(
                matcher_alert_count=matcher_count,
                selected_alert_count=len(selected),
                proposed_group_count=len(grouped),
                groups=tuple(
                    RulePreviewGroup(
                        source_id=source_id,
                        group_key=group_key,
                        fingerprints=tuple(sorted(fingerprints)),
                    )
                    for (source_id, group_key), fingerprints in sorted(grouped.items())
                ),
                by_source=tuple(
                    RulePreviewSource(
                        source_id=source_id,
                        source_name=source_names.get(source_id, source_id),
                        matcher_alert_count=matcher_by_source.get(source_id, 0),
                        selected_alert_count=sum(
                            selected_source == source_id
                            for selected_source, _fingerprint, _group in selected
                        ),
                        proposed_group_count=sum(
                            grouped_source == source_id
                            for grouped_source, _group in grouped
                        ),
                    )
                    for source_id in source_rows
                ),
            )

    def publish_rule(
        self,
        *,
        name: str,
        priority: int,
        enabled: bool,
        matchers: Sequence[tuple[str, str, str]],
        group_by_labels: Sequence[str],
        source_ids: Sequence[str],
        now: datetime,
        grouping_window_seconds: int = 30,
    ) -> RuleView:
        parsed = _validate_rule(name, priority, matchers, group_by_labels)
        with self._sessions.begin() as session:
            self._ensure_source_scope(session, source_ids)
            record = AggregationRuleRecord(
                name=name.strip(),
                priority=priority,
                enabled=enabled,
                matchers_json=_json(
                    [[item.label, item.operator.value, item.value] for item in parsed]
                ),
                group_by_labels_json=_json(tuple(group_by_labels)),
                source_ids_json=_json(tuple(sorted(set(source_ids)))),
                grouping_window_seconds=validate_grouping_window(grouping_window_seconds),
                version=1,
                created_at=_stored(now),
                updated_at=_stored(now),
            )
            session.add(record)
            session.flush()
            self._regroup(session, now=now)
            return RuleView(
                record.id,
                record.name,
                record.priority,
                record.enabled,
                record.version,
                tuple((item.label, item.operator.value, item.value) for item in parsed),
                tuple(group_by_labels),
                tuple(sorted(set(source_ids))),
                record.grouping_window_seconds,
                _aware(record.created_at),
                _aware(record.updated_at),
            )

    def update_rule(
        self,
        rule_id: int,
        *,
        expected_version: int,
        name: str,
        priority: int,
        enabled: bool,
        matchers: Sequence[tuple[str, str, str]],
        group_by_labels: Sequence[str],
        source_ids: Sequence[str],
        now: datetime,
        grouping_window_seconds: int = 30,
    ) -> RuleView:
        parsed = _validate_rule(name, priority, matchers, group_by_labels)
        with self._sessions.begin() as session:
            self._ensure_source_scope(session, source_ids)
            record = session.get(AggregationRuleRecord, rule_id)
            if record is None:
                raise LookupError("AGGREGATION_RULE_NOT_FOUND")
            if record.version != expected_version:
                raise FileExistsError("AGGREGATION_RULE_VERSION_CONFLICT")
            record.name = name.strip()
            record.priority = priority
            record.enabled = enabled
            record.matchers_json = _json(
                [[item.label, item.operator.value, item.value] for item in parsed]
            )
            record.group_by_labels_json = _json(tuple(group_by_labels))
            record.source_ids_json = _json(tuple(sorted(set(source_ids))))
            record.grouping_window_seconds = validate_grouping_window(grouping_window_seconds)
            record.version += 1
            record.updated_at = _stored(now)
            session.flush()
            self._regroup(session, now=now)
            return RuleView(
                record.id,
                record.name,
                record.priority,
                record.enabled,
                record.version,
                tuple((item.label, item.operator.value, item.value) for item in parsed),
                tuple(group_by_labels),
                tuple(sorted(set(source_ids))),
                record.grouping_window_seconds,
                    _aware(record.created_at),
                    _aware(record.updated_at),
            )

    def _regroup(self, session: Session, *, now: datetime) -> None:
        rules = self._rules(session)
        sources = {item.id: item.state for item in session.scalars(select(SourceRecord))}
        for alert_record in session.scalars(select(AlertRecord).order_by(AlertRecord.id)):
            raw = cast(Mapping[str, object], json.loads(alert_record.raw_json))
            alert = normalize_alert(raw, source_id=alert_record.source_id)
            decision = choose_aggregation(alert, rules)
            incident = session.scalar(
                select(IncidentRecord).where(
                    IncidentRecord.source_id == alert.source_id,
                    IncidentRecord.group_key == decision.group_key,
                )
            )
            if incident is None:
                incident = IncidentRecord(
                    source_id=alert.source_id,
                    group_key=decision.group_key,
                    title=decision.title,
                    severity=alert.severity,
                    source_state=alert_record.source_state,
                    freshness_state=(
                        "STALE"
                        if sources.get(alert.source_id) != SourceState.ENABLED.value
                        else "FRESH"
                    ),
                    handling_state="NEW",
                    handling_version=1,
                    aggregation_rule_id=decision.rule_id,
                    aggregation_rule_version=decision.rule_version,
                    group_labels_json=_json(decision.group_labels),
                    missing_labels_json=_json(decision.missing_labels),
                    occurrence_no=1,
                    occurrence_started_at=alert_record.last_seen_at,
                    change_version=0,
                    change_origin="REGROUP",
                    updated_at=_stored(now),
                )
                session.add(incident)
                session.flush()
            else:
                incident.title = decision.title
                incident.aggregation_rule_id = decision.rule_id
                incident.aggregation_rule_version = decision.rule_version
                incident.group_labels_json = _json(decision.group_labels)
                incident.missing_labels_json = _json(decision.missing_labels)
            alert_record.incident_id = incident.id
        session.flush()
        for incident in session.scalars(select(IncidentRecord)):
            members = session.scalar(
                select(AlertRecord.id).where(AlertRecord.incident_id == incident.id).limit(1)
            )
            if members is None:
                incident.source_state = "RECOVERED"
            elif sources.get(incident.source_id) != SourceState.ENABLED.value:
                incident.freshness_state = "STALE"
