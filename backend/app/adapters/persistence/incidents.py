"""SQLAlchemy current-Incident, append-only history and retention adapter."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
import hashlib
import json
from typing import Any, Literal, cast

from sqlalchemy import (
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    and_,
    case,
    func,
    or_,
    select,
    update,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app.adapters.persistence.notifications import NotificationRouteRecord, SqlAlchemyNotificationStore
from app.adapters.persistence.commands import CommandReceiptRecord
from app.application.commands import CommandConflict
from app.adapters.persistence.sources import (
    AggregationRuleRecord,
    AlertRecord,
    IncidentRecord,
    SourceRecord,
)
from app.platform.persistence.codecs import aware_utc as _aware, stored_utc as _stored
from app.application.incidents import (
    CollaborationCommandResult,
    HandlingAuditView,
    IncidentDetailView,
    IncidentMemberView,
    IncidentTaskView,
    OperationalOccurrencePage,
    OperationalOccurrenceView,
    OccurrencePage,
    OccurrenceView,
    RetentionView,
    ResponseCommandResult,
    ServiceAssignmentCommandResult,
    SimilarHistoryMatchReasonView,
    SimilarHistoryView,
    TimelineEntryView,
)
from app.domains.incidents.collaboration import (
    IncidentTaskDraft,
    IncidentTaskState,
    decide_task_transition,
)
from app.domains.incidents.actors import (
    InteractiveOperatorActor,
    require_interactive_operator,
)
from app.domains.incidents.models import (
    HandlingState,
    IncidentLifecycleChange,
    IncidentLifecycleSnapshot,
    can_change_handling,
    is_recurrence,
    should_seal_occurrence,
)
from app.domains.incidents.queue import (
    ACK_SLA_AT_RISK_PERCENT,
    ResponseState,
    SignalState,
    UNMAPPED_ACK_SLA_SECONDS,
    ack_sla_state,
    signal_state,
)
from app.domains.incidents.response import (
    ResponseAction,
    ResponseActionKind,
    ResponseState as CommandResponseState,
    decide_response,
)
from app.domains.incidents.similar_history import (
    MAX_SIMILAR_OCCURRENCES,
    MINIMUM_SIMILARITY_SCORE,
    SimilarityProfileV1,
    score_similar_occurrence,
)
from app.domains.sources.models import PollCompleteness
from app.platform.persistence.database import SessionFactory
from app.platform.cursor import CursorError, SignedCursorCodec

SEVERITY_RANK = {"unknown": 0, "info": 1, "warning": 2, "critical": 3}


class Base(DeclarativeBase):
    pass


class IncidentAuditRecord(Base):
    __tablename__ = "incident_audit"
    __table_args__ = (Index("ix_incident_audit_incident_created", "incident_id", "created_at", "id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # The database migration owns the cross-module FK. The adapter metadata is
    # intentionally isolated, so it maps the stable identifier as an integer.
    incident_id: Mapped[int] = mapped_column(Integer, nullable=False)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    from_state: Mapped[str] = mapped_column(String(24), nullable=False)
    to_state: Mapped[str] = mapped_column(String(24), nullable=False)
    reason: Mapped[str] = mapped_column(String(2000), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class IncidentOccurrenceRecord(Base):
    __tablename__ = "incident_occurrence"
    __table_args__ = (
        UniqueConstraint("incident_id", "occurrence_no", name="uq_incident_occurrence"),
        Index("ix_incident_occurrence_recovered", "recovered_at", "id"),
        Index("ix_incident_occurrence_source_recovered", "source_id", "recovered_at", "id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(Integer, nullable=False)
    occurrence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_name: Mapped[str] = mapped_column(String(160), nullable=False)
    group_key: Mapped[str] = mapped_column(String(512), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    aggregation_rule_id: Mapped[int | None] = mapped_column(Integer)
    aggregation_rule_name: Mapped[str | None] = mapped_column(String(120))
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    recovered_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    member_count: Mapped[int] = mapped_column(Integer, nullable=False)
    member_max_severity: Mapped[str] = mapped_column(String(16), nullable=False)
    handling_conclusion: Mapped[str] = mapped_column(String(24), nullable=False)


class OperationalOccurrenceRecord(Base):
    __tablename__ = "operational_occurrence"
    __table_args__ = (
        UniqueConstraint(
            "incident_id", "occurrence_no", name="uq_operational_occurrence_number"
        ),
        Index(
            "ix_operational_occurrence_queue",
            "response_state",
            "ack_sla_due_at",
            "response_priority",
            "latest_activity_at",
            "id",
        ),
        Index(
            "ix_operational_occurrence_source",
            "source_id",
            "response_state",
            "latest_activity_at",
            "id",
        ),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(Integer, nullable=False)
    occurrence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    group_key: Mapped[str] = mapped_column(String(512), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    signal_state: Mapped[str] = mapped_column(String(16), nullable=False)
    signal_severity: Mapped[str] = mapped_column(String(16), nullable=False)
    response_state: Mapped[str] = mapped_column(String(24), nullable=False)
    response_priority: Mapped[str] = mapped_column(String(2), nullable=False)
    resolution_code: Mapped[str | None] = mapped_column(String(24))
    duplicate_of_occurrence_id: Mapped[int | None] = mapped_column(Integer)
    service_id: Mapped[int | None] = mapped_column(Integer)
    primary_alertname: Mapped[str | None] = mapped_column(String(256))
    aggregation_rule_id: Mapped[int | None] = mapped_column(Integer)
    group_labels_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    assignment_origin: Mapped[str] = mapped_column(String(16), nullable=False)
    creation_unmapped: Mapped[bool | None] = mapped_column(nullable=True)
    member_count: Mapped[int] = mapped_column(Integer, nullable=False)
    evidence_completeness: Mapped[str] = mapped_column(String(16), nullable=False)
    detected_at: Mapped[datetime | None] = mapped_column(DateTime)
    source_started_at: Mapped[datetime | None] = mapped_column(DateTime)
    ack_sla_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    ack_sla_due_at: Mapped[datetime | None] = mapped_column(DateTime)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime)
    first_investigating_at: Mapped[datetime | None] = mapped_column(DateTime)
    mitigated_at: Mapped[datetime | None] = mapped_column(DateTime)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)
    latest_activity_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)


class IncidentTimelineEntryRecord(Base):
    __tablename__ = "incident_timeline_entry"
    __table_args__ = (
        UniqueConstraint(
            "occurrence_id", "sequence", name="uq_incident_timeline_sequence"
        ),
        Index("ix_incident_timeline_occurrence", "occurrence_id", "sequence"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    occurrence_id: Mapped[int] = mapped_column(Integer, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    summary: Mapped[str] = mapped_column(String(512), nullable=False)
    detail_json: Mapped[str] = mapped_column(Text, nullable=False)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_ip: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class IncidentTaskRecord(Base):
    __tablename__ = "incident_task"
    __table_args__ = (
        Index("ix_incident_task_occurrence_status", "occurrence_id", "status", "id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    occurrence_id: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    due_at: Mapped[datetime | None] = mapped_column(DateTime)
    runbook_link: Mapped[str | None] = mapped_column(String(2048))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    result: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class IncidentNoteContentRecord(Base):
    __tablename__ = "incident_note_content"
    __table_args__ = (
        UniqueConstraint("timeline_entry_id", name="uq_incident_note_timeline"),
        Index("ix_incident_note_purge", "purge_after", "id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    occurrence_id: Mapped[int] = mapped_column(Integer, nullable=False)
    timeline_entry_id: Mapped[int] = mapped_column(Integer, nullable=False)
    content_envelope: Mapped[str | None] = mapped_column(Text)
    redacted_at: Mapped[datetime | None] = mapped_column(DateTime)
    purge_after: Mapped[datetime | None] = mapped_column(DateTime)


def occurrence_response_state_in_session(
    session: Session, incident_id: int, occurrence_no: int
) -> str | None:
    """Read the operational Response projection without exposing its ORM type."""
    return session.scalar(
        select(OperationalOccurrenceRecord.response_state).where(
            OperationalOccurrenceRecord.incident_id == incident_id,
            OperationalOccurrenceRecord.occurrence_no == occurrence_no,
        )
    )


def _aware_optional(value: datetime | None) -> datetime | None:
    return None if value is None else _aware(value)


class SqlAlchemyIncidentStore:
    """Own current handling/history transactions and source-change reconciliation."""

    def __init__(
        self,
        sessions: SessionFactory,
        *,
        cursor_codec: SignedCursorCodec,
        notifications: SqlAlchemyNotificationStore | None = None,
        encrypt_secret: Callable[[str], str] | None = None,
        decrypt_secret: Callable[[str], str] | None = None,
        service_assignment_resolver: Callable[[Session, int], Any] | None = None,
        service_descriptor: Callable[
            [Session, int | None, str], tuple[str | None, str]
        ]
        | None = None,
        active_service_lookup: Callable[[Session, int], tuple[str, int] | None]
        | None = None,
        noise_descriptor: Callable[[Session, int, datetime], Any] | None = None,
        investigation_cancel: Callable[[Session, int, datetime], None] | None = None,
    ) -> None:
        self._sessions = sessions
        self._cursor = cursor_codec
        self._notifications = notifications
        self._encrypt = encrypt_secret or (lambda value: value)
        self._decrypt = decrypt_secret or (lambda value: value)
        self._service_assignment_resolver = service_assignment_resolver
        self._service_descriptor = service_descriptor or self._default_service_descriptor
        self._active_service_lookup = active_service_lookup
        self._noise_descriptor = noise_descriptor
        self._investigation_cancel = investigation_cancel

    @staticmethod
    def _default_service_descriptor(
        _session: Session, service_id: int | None, assignment_origin: str
    ) -> tuple[str | None, str]:
        if service_id is not None:
            return str(service_id), "MAPPED"
        return None, "SERVICE_AMBIGUOUS" if assignment_origin == "MAPPING" else "UNMAPPED"

    def _resolve_service_assignment(self, session: Session, incident_id: int) -> Any:
        if self._service_assignment_resolver is None:
            return type(
                "UnmappedServiceProjection",
                (),
                {
                    "service_id": None,
                    "assignment_origin": "UNMAPPED",
                    "assignment_state": "UNMAPPED",
                    "ack_sla_seconds": UNMAPPED_ACK_SLA_SECONDS,
                },
            )()
        return self._service_assignment_resolver(session, incident_id)

    @staticmethod
    def _primary_alertname(session: Session, incident_id: int) -> str | None:
        row = session.execute(
            select(AlertRecord.alertname, func.count(AlertRecord.id).label("member_count"))
            .where(AlertRecord.incident_id == incident_id)
            .group_by(AlertRecord.alertname)
            .order_by(func.count(AlertRecord.id).desc(), AlertRecord.alertname)
            .limit(1)
        ).first()
        return None if row is None else str(row[0])

    @staticmethod
    def snapshot(session: Session, source_id: str) -> dict[int, IncidentLifecycleSnapshot]:
        return {
            item.id: IncidentLifecycleSnapshot(
                item.source_state,
                item.severity,
                item.handling_state,
                item.occurrence_no,
                item.change_version,
            )
            for item in session.scalars(
                select(IncidentRecord).where(IncidentRecord.source_id == source_id)
            )
        }

    def _sync_operational_occurrence(
        self,
        session: Session,
        incident: IncidentRecord,
        *,
        observed_at: datetime,
        completeness: PollCompleteness,
    ) -> OperationalOccurrenceRecord:
        occurrence = session.scalar(
            select(OperationalOccurrenceRecord).where(
                OperationalOccurrenceRecord.incident_id == incident.id,
                OperationalOccurrenceRecord.occurrence_no == incident.occurrence_no,
            )
        )
        trusted_detection = (
            completeness is PollCompleteness.COMPLETE
            and incident.source_state == "FIRING"
        )
        if occurrence is None:
            detected_at = _stored(observed_at) if trusted_detection else None
            assignment = self._resolve_service_assignment(session, incident.id)
            member_count = session.scalar(
                select(func.count(AlertRecord.id)).where(
                    AlertRecord.incident_id == incident.id
                )
            )
            occurrence = OperationalOccurrenceRecord(
                incident_id=incident.id,
                occurrence_no=incident.occurrence_no,
                source_id=incident.source_id,
                group_key=incident.group_key,
                title=incident.title,
                signal_state=signal_state(
                    incident.source_state, incident.freshness_state
                ).value,
                signal_severity=incident.severity,
                response_state=ResponseState.UNACKNOWLEDGED.value,
                # Frozen pre-cutover compatibility column; not a product fact.
                response_priority="P3",
                resolution_code=None,
                duplicate_of_occurrence_id=None,
                service_id=assignment.service_id,
                primary_alertname=self._primary_alertname(session, incident.id),
                aggregation_rule_id=incident.aggregation_rule_id,
                group_labels_json=incident.group_labels_json,
                assignment_origin=assignment.assignment_origin,
                creation_unmapped=assignment.service_id is None,
                member_count=int(member_count or 0),
                evidence_completeness=completeness.value,
                detected_at=detected_at,
                source_started_at=session.scalar(
                    select(AlertRecord.starts_at)
                    .where(
                        AlertRecord.incident_id == incident.id,
                        AlertRecord.starts_at.is_not(None),
                    )
                    .order_by(AlertRecord.starts_at)
                    .limit(1)
                ),
                ack_sla_seconds=assignment.ack_sla_seconds,
                ack_sla_due_at=(
                    None
                    if detected_at is None
                    else detected_at + timedelta(seconds=assignment.ack_sla_seconds)
                ),
                acknowledged_at=None,
                first_investigating_at=None,
                mitigated_at=None,
                resolved_at=None,
                latest_activity_at=_stored(observed_at),
                version=1,
            )
            session.add(occurrence)
            session.flush()
            return occurrence
        next_signal_state = signal_state(
            incident.source_state, incident.freshness_state
        ).value
        next_signal_severity = (
            occurrence.signal_severity
            if next_signal_state != SignalState.FIRING.value
            and incident.severity.lower() == "unknown"
            else incident.severity
        )
        member_count = int(
            session.scalar(
                select(func.count(AlertRecord.id)).where(
                    AlertRecord.incident_id == incident.id
                )
            )
            or 0
        )
        assignment_changed = False
        if (
            occurrence.response_state != ResponseState.RESOLVED.value
            and occurrence.assignment_origin != "MANUAL"
        ):
            assignment = self._resolve_service_assignment(session, incident.id)
            assignment_changed = (
                occurrence.service_id != assignment.service_id
                or occurrence.assignment_origin != assignment.assignment_origin
            )
            if assignment_changed:
                previous_service_id = occurrence.service_id
                occurrence.service_id = assignment.service_id
                occurrence.assignment_origin = assignment.assignment_origin
                occurrence.version += 1
                sequence = int(
                    session.scalar(
                        select(func.max(IncidentTimelineEntryRecord.sequence)).where(
                            IncidentTimelineEntryRecord.occurrence_id == occurrence.id
                        )
                    )
                    or 0
                ) + 1
                session.add(
                    IncidentTimelineEntryRecord(
                        occurrence_id=occurrence.id,
                        sequence=sequence,
                        actor_type="SYSTEM",
                        event_type="SERVICE_MAPPING_UPDATED",
                        summary="告警成员变化更新了当前事件的服务归属",
                        detail_json=json.dumps(
                            {
                                "assignment_state": assignment.assignment_state,
                                "previous_service_id": previous_service_id,
                                "service_id": assignment.service_id,
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        request_id="source-reconciliation",
                        source_ip="local-system",
                        created_at=_stored(observed_at),
                    )
                )
        changed = (
            occurrence.group_key != incident.group_key
            or occurrence.title != incident.title
            or occurrence.signal_state != next_signal_state
            or occurrence.signal_severity != next_signal_severity
            or occurrence.member_count != member_count
            or occurrence.evidence_completeness != completeness.value
            or assignment_changed
        )
        occurrence.group_key = incident.group_key
        occurrence.title = incident.title
        occurrence.signal_state = next_signal_state
        occurrence.signal_severity = next_signal_severity
        occurrence.member_count = member_count
        occurrence.evidence_completeness = completeness.value
        if occurrence.response_state != ResponseState.RESOLVED.value:
            occurrence.primary_alertname = self._primary_alertname(session, incident.id)
            occurrence.aggregation_rule_id = incident.aggregation_rule_id
            occurrence.group_labels_json = incident.group_labels_json
        if occurrence.detected_at is None and trusted_detection:
            occurrence.detected_at = _stored(observed_at)
            occurrence.ack_sla_due_at = _stored(observed_at) + timedelta(
                seconds=occurrence.ack_sla_seconds
            )
            changed = True
        if changed:
            occurrence.latest_activity_at = _stored(observed_at)
        return occurrence

    @staticmethod
    def mark_source_stale(
        session: Session,
        source_id: str,
        *,
        observed_at: datetime,
    ) -> None:
        for occurrence in session.scalars(
            select(OperationalOccurrenceRecord).where(
                OperationalOccurrenceRecord.source_id == source_id,
                OperationalOccurrenceRecord.response_state
                != ResponseState.RESOLVED.value,
            )
        ):
            if occurrence.signal_state != "STALE":
                occurrence.signal_state = "STALE"
                occurrence.evidence_completeness = "UNKNOWN"
                occurrence.latest_activity_at = _stored(observed_at)

    def reconcile_source_changes(
        self,
        session: Session,
        before: Mapping[int, IncidentLifecycleSnapshot],
        *,
        source_id: str,
        observed_at: datetime,
        completeness: PollCompleteness,
    ) -> tuple[IncidentLifecycleChange, ...]:
        changes: list[IncidentLifecycleChange] = []
        for incident in session.scalars(
            select(IncidentRecord).where(IncidentRecord.source_id == source_id).order_by(IncidentRecord.id)
        ):
            previous = before.get(incident.id)
            if is_recurrence(previous, incident.source_state, "LIVE_POLL"):
                assert previous is not None
                incident.occurrence_no = previous.occurrence_no + 1
                incident.occurrence_started_at = _stored(observed_at)
                if incident.handling_state != HandlingState.NEW.value:
                    session.add(
                        IncidentAuditRecord(
                            incident_id=incident.id,
                            actor="system",
                            from_state=incident.handling_state,
                            to_state=HandlingState.NEW.value,
                            reason="source_recurrence",
                            created_at=_stored(observed_at),
                        )
                    )
                    incident.handling_state = HandlingState.NEW.value
                    incident.handling_version += 1
            self._sync_operational_occurrence(
                session,
                incident,
                observed_at=observed_at,
                completeness=completeness,
            )
            meaningful = previous is None or any(
                (
                    previous.source_state != incident.source_state,
                    previous.severity != incident.severity,
                    previous.handling_state != incident.handling_state,
                    previous.occurrence_no != incident.occurrence_no,
                )
            )
            if not meaningful:
                continue
            incident.change_version = (previous.change_version if previous else 0) + 1
            incident.change_origin = "LIVE_POLL"
            incident.updated_at = _stored(observed_at)
            session.flush()
            change = IncidentLifecycleChange(
                incident.id,
                incident.occurrence_no,
                None if previous is None else previous.source_state,
                incident.source_state,
                None if previous is None else previous.severity,
                incident.severity,
                None if previous is None else previous.handling_state,
                incident.handling_state,
                incident.change_version,
                "LIVE_POLL",
                observed_at,
            )
            self._seal_if_ended(session, incident, change, completeness=completeness)
            if self._notifications is not None:
                self._notifications.plan_incident_change_in_session(session, incident.id, change)
            changes.append(change)
        return tuple(changes)

    def _seal_if_ended(
        self,
        session: Session,
        incident: IncidentRecord,
        change: IncidentLifecycleChange,
        *,
        completeness: PollCompleteness,
    ) -> None:
        members = tuple(session.scalars(select(AlertRecord).where(AlertRecord.incident_id == incident.id)))
        if not should_seal_occurrence(
            change,
            complete_poll=completeness is PollCompleteness.COMPLETE,
            member_count=len(members),
        ):
            return
        exists = session.scalar(
            select(IncidentOccurrenceRecord.id).where(
                IncidentOccurrenceRecord.incident_id == incident.id,
                IncidentOccurrenceRecord.occurrence_no == incident.occurrence_no,
            )
        )
        if exists is not None:
            return
        source = session.get(SourceRecord, incident.source_id)
        rule = None if incident.aggregation_rule_id is None else session.get(AggregationRuleRecord, incident.aggregation_rule_id)
        severity = max(
            (item.severity for item in members),
            key=lambda item: SEVERITY_RANK.get(item.lower(), 0),
            default="unknown",
        )
        session.add(
            IncidentOccurrenceRecord(
                incident_id=incident.id,
                occurrence_no=incident.occurrence_no,
                source_id=incident.source_id,
                source_name=source.name if source is not None else incident.source_id,
                group_key=incident.group_key,
                title=incident.title,
                aggregation_rule_id=incident.aggregation_rule_id,
                aggregation_rule_name=None if rule is None else rule.name,
                started_at=incident.occurrence_started_at,
                recovered_at=_stored(change.observed_at),
                member_count=len(members),
                member_max_severity=severity,
                handling_conclusion=incident.handling_state,
            )
        )

    @staticmethod
    def _audit_view(item: IncidentAuditRecord) -> HandlingAuditView:
        return HandlingAuditView(
            item.id,
            item.incident_id,
            item.actor,
            item.from_state,
            item.to_state,
            item.reason,
            _aware(item.created_at),
        )

    def get_incident(self, incident_id: int) -> IncidentDetailView:
        with self._sessions() as session:
            incident = session.get(IncidentRecord, incident_id)
            if incident is None:
                raise LookupError("INCIDENT_NOT_FOUND")
            source = session.get(SourceRecord, incident.source_id)
            audits = tuple(
                self._audit_view(item)
                for item in session.scalars(
                    select(IncidentAuditRecord)
                    .where(IncidentAuditRecord.incident_id == incident_id)
                    .order_by(IncidentAuditRecord.created_at, IncidentAuditRecord.id)
                )
            )
            member_records = tuple(
                session.scalars(
                    select(AlertRecord)
                    .where(AlertRecord.incident_id == incident_id)
                    .order_by(AlertRecord.id)
                )
            )
            members = tuple(
                IncidentMemberView(
                    item.id,
                    item.source_id,
                    item.upstream_fingerprint,
                    item.alertname,
                    item.severity,
                    item.cluster,
                    cast(dict[str, str], json.loads(item.labels_json)),
                    cast(dict[str, str], json.loads(item.annotations_json)),
                    _aware_optional(item.starts_at),
                    _aware_optional(item.missing_since_at),
                    item.source_state,
                    item.origin,
                    item.evidence_completeness,
                    _aware(item.last_seen_at),
                    item.incident_id,
                )
                for item in member_records
            )
            rule = (
                None
                if incident.aggregation_rule_id is None
                else session.get(AggregationRuleRecord, incident.aggregation_rule_id)
            )
            missing_labels = cast(list[str], json.loads(incident.missing_labels_json))
            group_labels = cast(dict[str, str], json.loads(incident.group_labels_json))
            aggregation_status: Literal["matched", "unmatched", "missing_labels"]
            if incident.aggregation_rule_id is None:
                aggregation_status = "unmatched"
                grouping_explanation = "未命中启用的聚合规则，按来源与告警 fingerprint 安全隔离。"
            elif missing_labels:
                aggregation_status = "missing_labels"
                grouping_explanation = (
                    "已命中聚合规则，但缺少标签 " + ", ".join(missing_labels) + "，未与其它告警合并。"
                )
            else:
                aggregation_status = "matched"
                grouping_explanation = "按 " + ", ".join(
                    f"{name}={value}" for name, value in sorted(group_labels.items())
                ) + " 聚合。"
            return IncidentDetailView(
                incident.id,
                incident.source_id,
                source.name if source is not None else incident.source_id,
                incident.group_key,
                incident.title,
                incident.severity,
                incident.source_state,
                incident.freshness_state,
                incident.handling_state,
                incident.handling_version,
                incident.occurrence_no,
                _aware(incident.occurrence_started_at),
                _aware(incident.updated_at),
                len(members),
                incident.aggregation_rule_id,
                None if rule is None else rule.name,
                aggregation_status,
                grouping_explanation,
                incident.aggregation_rule_version,
                members,
                audits,
            )

    def change_handling(
        self,
        incident_id: int,
        *,
        target: str,
        reason: str,
        actor: str,
        expected_version: int,
        now: datetime,
    ) -> HandlingAuditView:
        clean_reason = reason.strip()
        clean_actor = actor.strip() or "local-user"
        if not clean_reason or len(clean_reason) > 2000 or len(clean_actor) > 128:
            raise ValueError("INCIDENT_HANDLING_INPUT_INVALID")
        with self._sessions.begin() as session:
            incident = session.get(IncidentRecord, incident_id)
            if incident is None:
                raise LookupError("INCIDENT_NOT_FOUND")
            if incident.handling_version != expected_version:
                raise FileExistsError("INCIDENT_HANDLING_VERSION_CONFLICT")
            if not can_change_handling(incident.handling_state, target):
                raise RuntimeError("INCIDENT_HANDLING_TRANSITION_INVALID")
            previous = incident.handling_state
            incident.handling_state = HandlingState(target).value
            incident.handling_version += 1
            incident.change_version += 1
            incident.change_origin = "INTERACTIVE_HANDLING"
            incident.updated_at = _stored(now)
            audit = IncidentAuditRecord(
                incident_id=incident.id,
                actor=clean_actor,
                from_state=previous,
                to_state=incident.handling_state,
                reason=clean_reason,
                created_at=_stored(now),
            )
            session.add(audit)
            current = session.scalar(
                select(IncidentOccurrenceRecord).where(
                    IncidentOccurrenceRecord.incident_id == incident.id,
                    IncidentOccurrenceRecord.occurrence_no == incident.occurrence_no,
                )
            )
            if current is not None:
                current.handling_conclusion = incident.handling_state
            session.flush()
            change = IncidentLifecycleChange(
                incident.id,
                incident.occurrence_no,
                incident.source_state,
                incident.source_state,
                incident.severity,
                incident.severity,
                previous,
                incident.handling_state,
                incident.change_version,
                "INTERACTIVE_HANDLING",
                now,
            )
            if self._notifications is not None:
                self._notifications.plan_incident_change_in_session(session, incident.id, change)
            session.flush()
            return self._audit_view(audit)

    @staticmethod
    def _occurrence_view(item: IncidentOccurrenceRecord) -> OccurrenceView:
        return OccurrenceView(
            item.id,
            item.incident_id,
            item.occurrence_no,
            item.source_id,
            item.source_name,
            item.group_key,
            item.title,
            item.aggregation_rule_id,
            item.aggregation_rule_name,
            _aware(item.started_at),
            _aware(item.recovered_at),
            item.member_count,
            item.member_max_severity,
            item.handling_conclusion,
        )

    def _visible_sources(self, session: Session, requested: tuple[str, ...], include_archived: bool) -> tuple[str, ...]:
        if requested:
            known = {item.id: item.state for item in session.scalars(select(SourceRecord).where(SourceRecord.id.in_(requested)))}
            return tuple(
                item for item in requested
                if item not in known or known[item] != "ARCHIVED" or include_archived
            )
        enabled = tuple(session.scalars(select(SourceRecord.id).where(SourceRecord.state == "ENABLED")))
        historical = tuple(session.scalars(select(IncidentOccurrenceRecord.source_id).distinct()))
        known_ids = set(session.scalars(select(SourceRecord.id)))
        return tuple(sorted(set(enabled) | {item for item in historical if item not in known_ids}))

    def list_occurrences(
        self,
        *,
        source_ids: tuple[str, ...],
        conclusion: str | None,
        include_archived: bool,
        cursor: str | None,
        limit: int,
    ) -> OccurrencePage:
        normalized_sources = tuple(sorted(set(source_ids)))
        filters: dict[str, Any] = {
            "source_ids": normalized_sources,
            "conclusion": conclusion or "",
            "include_archived": include_archived,
        }
        with self._sessions() as session:
            visible = self._visible_sources(session, normalized_sources, include_archived)
            if not visible:
                return OccurrencePage((), None)
            statement = select(IncidentOccurrenceRecord).where(IncidentOccurrenceRecord.source_id.in_(visible))
            if conclusion is not None:
                if conclusion not in {item.value for item in HandlingState}:
                    raise ValueError("INCIDENT_CONCLUSION_INVALID")
                statement = statement.where(IncidentOccurrenceRecord.handling_conclusion == conclusion)
            if cursor is not None:
                position = self._cursor.decode(cursor, filters=filters).position
                anchor = session.get(IncidentOccurrenceRecord, position)
                if anchor is None:
                    raise CursorError("CURSOR_INVALID")
                statement = statement.where(
                    or_(
                        IncidentOccurrenceRecord.recovered_at < anchor.recovered_at,
                        and_(
                            IncidentOccurrenceRecord.recovered_at == anchor.recovered_at,
                            IncidentOccurrenceRecord.id < anchor.id,
                        ),
                    )
                )
            rows = tuple(
                session.scalars(
                    statement.order_by(IncidentOccurrenceRecord.recovered_at.desc(), IncidentOccurrenceRecord.id.desc()).limit(limit + 1)
                )
            )
            page = rows[:limit]
            next_cursor = (
                self._cursor.encode(position=page[-1].id, filters=filters)
                if len(rows) > limit and page
                else None
            )
            return OccurrencePage(tuple(self._occurrence_view(item) for item in page), next_cursor)

    def get_occurrence(self, occurrence_id: int, *, include_archived: bool) -> OccurrenceView:
        with self._sessions() as session:
            item = session.get(IncidentOccurrenceRecord, occurrence_id)
            if item is None:
                raise LookupError("INCIDENT_OCCURRENCE_NOT_FOUND")
            source = session.get(SourceRecord, item.source_id)
            if source is not None and source.state == "ARCHIVED" and not include_archived:
                raise LookupError("INCIDENT_OCCURRENCE_NOT_FOUND")
            return self._occurrence_view(item)

    @staticmethod
    def _operational_order(now: datetime) -> tuple[Any, Any, Any]:
        stored_now = _stored(now)
        remaining_seconds = (
            func.strftime("%s", OperationalOccurrenceRecord.ack_sla_due_at)
            - func.strftime("%s", stored_now)
        )
        running = and_(
            OperationalOccurrenceRecord.acknowledged_at.is_(None),
            OperationalOccurrenceRecord.ack_sla_due_at.is_not(None),
        )
        breached = and_(
            running, OperationalOccurrenceRecord.ack_sla_due_at <= stored_now
        )
        at_risk = and_(
            running,
            OperationalOccurrenceRecord.ack_sla_due_at > stored_now,
            remaining_seconds
            <= OperationalOccurrenceRecord.ack_sla_seconds
            * ACK_SLA_AT_RISK_PERCENT
            / 100,
        )
        urgency = case((breached, 0), (at_risk, 1), else_=2)
        severity = case(
            (func.lower(OperationalOccurrenceRecord.signal_severity) == "critical", 0),
            (func.lower(OperationalOccurrenceRecord.signal_severity) == "warning", 1),
            (func.lower(OperationalOccurrenceRecord.signal_severity) == "info", 2),
            else_=3,
        )
        unacknowledged = case(
            (
                OperationalOccurrenceRecord.response_state
                == ResponseState.UNACKNOWLEDGED.value,
                0,
            ),
            else_=1,
        )
        return urgency, severity, unacknowledged

    def _operational_view(
        self,
        item: OperationalOccurrenceRecord,
        *,
        session: Session,
        source_name: str,
        now: datetime,
    ) -> OperationalOccurrenceView:
        due_at = _aware_optional(item.ack_sla_due_at)
        acknowledged_at = _aware_optional(item.acknowledged_at)
        state = ack_sla_state(
            due_at=due_at,
            ack_sla_seconds=item.ack_sla_seconds,
            acknowledged_at=acknowledged_at,
            now=now,
        )
        remaining = (
            None
            if due_at is None or acknowledged_at is not None
            else int((due_at - now).total_seconds())
        )
        service_name, assignment_state = self._service_descriptor(
            session, item.service_id, item.assignment_origin
        )
        noise = (
            None
            if self._noise_descriptor is None
            else self._noise_descriptor(session, item.id, now)
        )
        noise_ends_at = None if noise is None else noise.ends_at
        noise_remaining = (
            None
            if noise_ends_at is None
            else max(0, int((noise_ends_at - now).total_seconds()))
        )
        return OperationalOccurrenceView(
            id=item.id,
            incident_id=item.incident_id,
            occurrence_no=item.occurrence_no,
            source_id=item.source_id,
            source_name=source_name,
            group_key=item.group_key,
            title=item.title,
            signal_state=item.signal_state,
            signal_severity=item.signal_severity,
            response_state=item.response_state,
            resolution_code=item.resolution_code,
            duplicate_of_occurrence_id=item.duplicate_of_occurrence_id,
            service_id=item.service_id,
            service_name=service_name,
            assignment_origin=item.assignment_origin,
            service_assignment_state=assignment_state,
            member_count=item.member_count,
            evidence_completeness=item.evidence_completeness,
            detected_at=_aware_optional(item.detected_at),
            source_started_at=_aware_optional(item.source_started_at),
            ack_sla_seconds=item.ack_sla_seconds,
            ack_sla_due_at=due_at,
            acknowledged_at=acknowledged_at,
            resolved_at=_aware_optional(item.resolved_at),
            ack_sla_state=state.value,
            ack_sla_remaining_seconds=remaining,
            noise_state="NONE" if noise is None else noise.state,
            noise_reason=None if noise is None else noise.reason,
            noise_scope=None if noise is None else noise.scope,
            noise_starts_at=None if noise is None else noise.starts_at,
            noise_ends_at=noise_ends_at,
            noise_remaining_seconds=noise_remaining,
            latest_activity_at=_aware(item.latest_activity_at),
            version=item.version,
        )

    @staticmethod
    def _operational_filters(
        statement: Any,
        *,
        view: str,
        source_ids: tuple[str, ...],
        signal_states: tuple[str, ...],
        now: datetime,
    ) -> Any:
        stored_now = _stored(now)
        remaining_seconds = (
            func.strftime("%s", OperationalOccurrenceRecord.ack_sla_due_at)
            - func.strftime("%s", stored_now)
        )
        if view == "ALL":
            statement = statement.where(
                OperationalOccurrenceRecord.response_state
                != ResponseState.RESOLVED.value
            )
        elif view == "UNACKNOWLEDGED":
            statement = statement.where(
                OperationalOccurrenceRecord.response_state
                == ResponseState.UNACKNOWLEDGED.value
            )
        elif view == "SLA_AT_RISK":
            statement = statement.where(
                OperationalOccurrenceRecord.acknowledged_at.is_(None),
                OperationalOccurrenceRecord.ack_sla_due_at.is_not(None),
                remaining_seconds
                <= OperationalOccurrenceRecord.ack_sla_seconds
                * ACK_SLA_AT_RISK_PERCENT
                / 100,
            )
        elif view == "UNMAPPED":
            statement = statement.where(
                OperationalOccurrenceRecord.service_id.is_(None),
                OperationalOccurrenceRecord.response_state
                != ResponseState.RESOLVED.value,
            )
        elif view == "RESOLVED":
            statement = statement.where(
                OperationalOccurrenceRecord.response_state
                == ResponseState.RESOLVED.value
            )
        else:
            raise ValueError("INCIDENT_QUEUE_VIEW_INVALID")
        if source_ids:
            statement = statement.where(
                OperationalOccurrenceRecord.source_id.in_(source_ids)
            )
        if signal_states:
            statement = statement.where(
                OperationalOccurrenceRecord.signal_state.in_(signal_states)
            )
        return statement

    def list_operational_occurrences(
        self,
        *,
        view: str,
        source_ids: tuple[str, ...],
        signal_states: tuple[str, ...],
        cursor: str | None,
        limit: int,
        now: datetime,
    ) -> OperationalOccurrencePage:
        normalized_sources = tuple(sorted(set(source_ids)))
        normalized_signals = tuple(sorted(set(signal_states)))
        if any(item not in {state.value for state in SignalState} for item in normalized_signals):
            raise ValueError("INCIDENT_QUEUE_SIGNAL_INVALID")
        filters: dict[str, Any] = {
            "view": view,
            "source_ids": normalized_sources,
            "signal_states": normalized_signals,
        }
        urgency, severity, unacknowledged = self._operational_order(now)
        with self._sessions() as session:
            statement = self._operational_filters(
                select(OperationalOccurrenceRecord),
                view=view,
                source_ids=normalized_sources,
                signal_states=normalized_signals,
                now=now,
            )
            if cursor is not None:
                position = self._cursor.decode(cursor, filters=filters).position
                anchor = session.get(OperationalOccurrenceRecord, position)
                if anchor is None:
                    raise CursorError("CURSOR_INVALID")
                anchor_due = _aware_optional(anchor.ack_sla_due_at)
                anchor_sla = ack_sla_state(
                    due_at=anchor_due,
                    ack_sla_seconds=anchor.ack_sla_seconds,
                    acknowledged_at=_aware_optional(anchor.acknowledged_at),
                    now=now,
                )
                anchor_urgency = {
                    "BREACHED": 0,
                    "AT_RISK": 1,
                }.get(anchor_sla.value, 2)
                anchor_severity = {
                    "critical": 0,
                    "warning": 1,
                    "info": 2,
                }.get(anchor.signal_severity.lower(), 3)
                anchor_unack = int(
                    anchor.response_state != ResponseState.UNACKNOWLEDGED.value
                )
                statement = statement.where(
                    or_(
                        urgency > anchor_urgency,
                        and_(urgency == anchor_urgency, severity > anchor_severity),
                        and_(
                            urgency == anchor_urgency,
                            severity == anchor_severity,
                            unacknowledged > anchor_unack,
                        ),
                        and_(
                            urgency == anchor_urgency,
                            severity == anchor_severity,
                            unacknowledged == anchor_unack,
                            OperationalOccurrenceRecord.latest_activity_at
                            < anchor.latest_activity_at,
                        ),
                        and_(
                            urgency == anchor_urgency,
                            severity == anchor_severity,
                            unacknowledged == anchor_unack,
                            OperationalOccurrenceRecord.latest_activity_at
                            == anchor.latest_activity_at,
                            OperationalOccurrenceRecord.id < anchor.id,
                        ),
                    )
                )
            rows = tuple(
                session.scalars(
                    statement.order_by(
                        urgency,
                        severity,
                        unacknowledged,
                        OperationalOccurrenceRecord.latest_activity_at.desc(),
                        OperationalOccurrenceRecord.id.desc(),
                    ).limit(limit + 1)
                )
            )
            page = rows[:limit]
            source_names = {
                item.id: item.name
                for item in session.scalars(
                    select(SourceRecord).where(
                        SourceRecord.id.in_({row.source_id for row in page})
                    )
                )
            }
            next_cursor = (
                self._cursor.encode(position=page[-1].id, filters=filters)
                if len(rows) > limit and page
                else None
            )
            return OperationalOccurrencePage(
                tuple(
                    self._operational_view(
                        item,
                        session=session,
                        source_name=source_names.get(item.source_id, item.source_id),
                        now=now,
                    )
                    for item in page
                ),
                next_cursor,
                now,
            )

    def get_operational_occurrence(
        self, occurrence_id: int, *, now: datetime
    ) -> OperationalOccurrenceView:
        with self._sessions() as session:
            item = session.get(OperationalOccurrenceRecord, occurrence_id)
            if item is None:
                raise LookupError("OPERATIONAL_OCCURRENCE_NOT_FOUND")
            source = session.get(SourceRecord, item.source_id)
            return self._operational_view(
                item,
                session=session,
                source_name=item.source_id if source is None else source.name,
                now=now,
            )

    @staticmethod
    def _task_view(item: IncidentTaskRecord) -> IncidentTaskView:
        return IncidentTaskView(
            id=item.id,
            occurrence_id=item.occurrence_id,
            title=item.title,
            description=item.description,
            due_at=_aware_optional(item.due_at),
            runbook_link=item.runbook_link,
            status=IncidentTaskState(item.status),
            result=item.result,
            version=item.version,
            created_at=_aware(item.created_at),
            updated_at=_aware(item.updated_at),
        )

    def _timeline_view(
        self,
        item: IncidentTimelineEntryRecord,
        *,
        session: Session | None = None,
    ) -> TimelineEntryView:
        detail: Any = json.loads(item.detail_json)
        if not isinstance(detail, dict):
            raise RuntimeError("INCIDENT_TIMELINE_DETAIL_INVALID")
        if item.event_type == "MANUAL_NOTE":
            if session is None:
                raise RuntimeError("INCIDENT_NOTE_SESSION_REQUIRED")
            note = session.scalar(
                select(IncidentNoteContentRecord).where(
                    IncidentNoteContentRecord.timeline_entry_id == item.id
                )
            )
            if note is None:
                raise RuntimeError("INCIDENT_NOTE_CONTENT_MISSING")
            detail = {
                "note_id": note.id,
                "redacted": note.redacted_at is not None,
                "text": (
                    "[REDACTED]"
                    if note.redacted_at is not None
                    else self._decrypt(str(note.content_envelope))
                ),
            }
        return TimelineEntryView(
            id=item.id,
            occurrence_id=item.occurrence_id,
            sequence=item.sequence,
            actor_type=item.actor_type,
            event_type=item.event_type,
            summary=item.summary,
            detail=cast(dict[str, Any], detail),
            request_id=item.request_id,
            source_ip=item.source_ip,
            created_at=_aware(item.created_at),
        )

    def list_tasks(self, occurrence_id: int) -> tuple[IncidentTaskView, ...]:
        with self._sessions() as session:
            if session.get(OperationalOccurrenceRecord, occurrence_id) is None:
                raise LookupError("OPERATIONAL_OCCURRENCE_NOT_FOUND")
            return tuple(
                self._task_view(item)
                for item in session.scalars(
                    select(IncidentTaskRecord)
                    .where(IncidentTaskRecord.occurrence_id == occurrence_id)
                    .order_by(IncidentTaskRecord.id)
                )
            )

    def list_timeline(
        self, occurrence_id: int, *, after_sequence: int = 0, limit: int = 100
    ) -> tuple[TimelineEntryView, ...]:
        if after_sequence < 0 or not 1 <= limit <= 200:
            raise ValueError("INCIDENT_TIMELINE_QUERY_INVALID")
        with self._sessions() as session:
            if session.get(OperationalOccurrenceRecord, occurrence_id) is None:
                raise LookupError("OPERATIONAL_OCCURRENCE_NOT_FOUND")
            return tuple(
                self._timeline_view(item, session=session)
                for item in session.scalars(
                    select(IncidentTimelineEntryRecord)
                    .where(
                        IncidentTimelineEntryRecord.occurrence_id == occurrence_id,
                        IncidentTimelineEntryRecord.sequence > after_sequence,
                    )
                    .order_by(IncidentTimelineEntryRecord.sequence)
                    .limit(limit)
                )
            )

    @staticmethod
    def _similarity_profile(item: OperationalOccurrenceRecord) -> SimilarityProfileV1:
        raw: Any = json.loads(item.group_labels_json or "{}")
        labels = (
            {str(name): str(value) for name, value in raw.items()}
            if isinstance(raw, dict)
            else {}
        )
        return SimilarityProfileV1(
            service_id=item.service_id,
            primary_alertname=item.primary_alertname,
            aggregation_rule_id=item.aggregation_rule_id,
            group_labels=labels,
        )

    def list_similar_history(
        self, occurrence_id: int, *, limit: int = MAX_SIMILAR_OCCURRENCES
    ) -> tuple[SimilarHistoryView, ...]:
        if not 1 <= limit <= MAX_SIMILAR_OCCURRENCES:
            raise ValueError("SIMILAR_HISTORY_LIMIT_INVALID")
        with self._sessions() as session:
            current = session.get(OperationalOccurrenceRecord, occurrence_id)
            if current is None:
                raise LookupError("OPERATIONAL_OCCURRENCE_NOT_FOUND")
            current_profile = self._similarity_profile(current)
            scored: list[tuple[int, datetime, OperationalOccurrenceRecord, Any]] = []
            for candidate in session.scalars(
                select(OperationalOccurrenceRecord).where(
                    OperationalOccurrenceRecord.source_id == current.source_id,
                    OperationalOccurrenceRecord.id != current.id,
                    OperationalOccurrenceRecord.response_state
                    == ResponseState.RESOLVED.value,
                    OperationalOccurrenceRecord.resolved_at.is_not(None),
                    OperationalOccurrenceRecord.resolution_code.is_not(None),
                )
            ):
                score = score_similar_occurrence(
                    current_profile, self._similarity_profile(candidate)
                )
                if score.score >= MINIMUM_SIMILARITY_SCORE:
                    assert candidate.resolved_at is not None
                    scored.append((score.score, candidate.resolved_at, candidate, score))
            scored.sort(key=lambda item: (item[0], item[1], item[2].id), reverse=True)
            results: list[SimilarHistoryView] = []
            for score_value, _ended_at, candidate, score in scored[:limit]:
                resolution = session.scalar(
                    select(IncidentTimelineEntryRecord)
                    .where(
                        IncidentTimelineEntryRecord.occurrence_id == candidate.id,
                        IncidentTimelineEntryRecord.event_type == "OCCURRENCE_RESOLVED",
                    )
                    .order_by(IncidentTimelineEntryRecord.sequence.desc())
                    .limit(1)
                )
                operator_conclusion: str | None = None
                if resolution is not None:
                    detail: Any = json.loads(resolution.detail_json)
                    if isinstance(detail, dict) and detail.get("reason"):
                        operator_conclusion = str(detail["reason"])[:2000]
                task_parts = [
                    f"{task.title}：{task.result}"
                    for task in session.scalars(
                        select(IncidentTaskRecord)
                        .where(
                            IncidentTaskRecord.occurrence_id == candidate.id,
                            IncidentTaskRecord.status == IncidentTaskState.DONE.value,
                            IncidentTaskRecord.result.is_not(None),
                        )
                        .order_by(IncidentTaskRecord.id)
                        .limit(5)
                    )
                    if task.result
                ]
                started_at = candidate.source_started_at or candidate.detected_at
                assert candidate.resolved_at is not None
                duration = (
                    0
                    if started_at is None
                    else max(
                        0,
                        int(
                            (
                                _aware(candidate.resolved_at) - _aware(started_at)
                            ).total_seconds()
                        ),
                    )
                )
                results.append(
                    SimilarHistoryView(
                        occurrence_id=candidate.id,
                        occurrence_no=candidate.occurrence_no,
                        title=candidate.title,
                        score=score_value,
                        match_reasons=tuple(
                            SimilarHistoryMatchReasonView(
                                item.kind, item.field, item.value, item.points
                            )
                            for item in score.reasons
                        ),
                        resolution_code=str(candidate.resolution_code),
                        operator_conclusion=operator_conclusion,
                        task_outcome=("；".join(task_parts)[:4000] or None),
                        handling_duration_seconds=duration,
                        resolved_at=_aware(candidate.resolved_at),
                    )
                )
            return tuple(results)

    def assign_service(
        self,
        occurrence_id: int,
        *,
        service_id: int,
        actor: InteractiveOperatorActor,
        expected_version: int,
        idempotency_key: str,
        request_id: str,
        source_ip: str,
        now: datetime,
    ) -> ServiceAssignmentCommandResult:
        require_interactive_operator(actor)
        payload = {
            "kind": "SERVICE_ASSIGNMENT",
            "occurrence_id": occurrence_id,
            "service_id": service_id,
            "expected_version": expected_version,
        }
        with self._sessions.begin() as session:
            receipt, replay = self._collaboration_request(
                session,
                occurrence_id=occurrence_id,
                idempotency_key=idempotency_key,
                payload=payload,
                now=now,
            )
            if replay is not None:
                occurrence = session.get(OperationalOccurrenceRecord, occurrence_id)
                timeline = session.get(
                    IncidentTimelineEntryRecord, int(replay["timeline_id"])
                )
                if occurrence is None or timeline is None:
                    raise RuntimeError("SERVICE_ASSIGNMENT_RECEIPT_INVALID")
                service_name, state = self._service_descriptor(
                    session, occurrence.service_id, occurrence.assignment_origin
                )
                if occurrence.service_id is None or service_name is None:
                    raise RuntimeError("SERVICE_ASSIGNMENT_RECEIPT_INVALID")
                return ServiceAssignmentCommandResult(
                    occurrence.id,
                    occurrence.service_id,
                    service_name,
                    occurrence.assignment_origin,
                    state,
                    occurrence.version,
                    self._timeline_view(timeline, session=session),
                    True,
                )
            occurrence = session.get(OperationalOccurrenceRecord, occurrence_id)
            if occurrence is None:
                raise LookupError("OPERATIONAL_OCCURRENCE_NOT_FOUND")
            if occurrence.response_state == CommandResponseState.RESOLVED.value:
                raise ValueError("SERVICE_ASSIGNMENT_RESOLVED_READ_ONLY")
            if occurrence.version != expected_version:
                raise FileExistsError("OCCURRENCE_VERSION_CONFLICT")
            service = (
                None
                if self._active_service_lookup is None
                else self._active_service_lookup(session, service_id)
            )
            if service is None:
                raise LookupError("ACTIVE_SERVICE_NOT_FOUND")
            service_name, _ack_sla_seconds = service
            previous_service_id = occurrence.service_id
            occurrence.service_id = service_id
            occurrence.assignment_origin = "MANUAL"
            occurrence.version += 1
            occurrence.latest_activity_at = _stored(now)
            timeline = self._append_timeline(
                session,
                occurrence_id=occurrence.id,
                actor=actor,
                event_type="SERVICE_ASSIGNMENT_CHANGED",
                summary="操作者更新了当前事件的服务归属",
                detail={
                    "previous_service_id": previous_service_id,
                    "service_id": service_id,
                    "service_name": service_name,
                    "ack_sla_frozen_seconds": occurrence.ack_sla_seconds,
                },
                request_id=request_id,
                source_ip=source_ip,
                now=now,
            )
            receipt.state = "COMPLETED"
            receipt.response_json = json.dumps(
                {"timeline_id": timeline.id},
                sort_keys=True,
                separators=(",", ":"),
            )
            receipt.updated_at = _stored(now)
            return ServiceAssignmentCommandResult(
                occurrence.id,
                service_id,
                service_name,
                "MANUAL",
                "MAPPED",
                occurrence.version,
                self._timeline_view(timeline, session=session),
                False,
            )

    @staticmethod
    def _next_timeline_sequence(session: Session, occurrence_id: int) -> int:
        return int(
            session.scalar(
                select(func.max(IncidentTimelineEntryRecord.sequence)).where(
                    IncidentTimelineEntryRecord.occurrence_id == occurrence_id
                )
            )
            or 0
        ) + 1

    @staticmethod
    def _append_timeline(
        session: Session,
        *,
        occurrence_id: int,
        actor: InteractiveOperatorActor,
        event_type: str,
        summary: str,
        detail: dict[str, Any],
        request_id: str,
        source_ip: str,
        now: datetime,
    ) -> IncidentTimelineEntryRecord:
        entry = IncidentTimelineEntryRecord(
            occurrence_id=occurrence_id,
            sequence=SqlAlchemyIncidentStore._next_timeline_sequence(
                session, occurrence_id
            ),
            actor_type=actor.kind,
            event_type=event_type,
            summary=summary,
            detail_json=json.dumps(detail, sort_keys=True, separators=(",", ":")),
            request_id=request_id[:128],
            source_ip=source_ip[:128],
            created_at=_stored(now),
        )
        session.add(entry)
        session.flush()
        return entry

    @staticmethod
    def _collaboration_request(
        session: Session,
        *,
        occurrence_id: int,
        idempotency_key: str,
        payload: Mapping[str, Any],
        now: datetime,
    ) -> tuple[CommandReceiptRecord, dict[str, Any] | None]:
        rendered = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        key_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        request_hash = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
        scope = f"occurrence-collaboration:{occurrence_id}"
        receipt = session.scalar(
            select(CommandReceiptRecord).where(
                CommandReceiptRecord.scope == scope,
                CommandReceiptRecord.key_hash == key_hash,
            )
        )
        if receipt is not None:
            if receipt.request_hash != request_hash:
                raise CommandConflict("IDEMPOTENCY_KEY_REUSED")
            if receipt.state != "COMPLETED" or receipt.response_json is None:
                raise CommandConflict("COMMAND_OUTCOME_UNKNOWN")
            replay: Any = json.loads(receipt.response_json)
            if not isinstance(replay, dict):
                raise RuntimeError("INCIDENT_COLLABORATION_RECEIPT_INVALID")
            return receipt, cast(dict[str, Any], replay)
        stored_now = _stored(now)
        receipt = CommandReceiptRecord(
            scope=scope,
            key_hash=key_hash,
            request_hash=request_hash,
            state="IN_PROGRESS",
            response_json=None,
            created_at=stored_now,
            updated_at=stored_now,
        )
        session.add(receipt)
        return receipt, None

    @staticmethod
    def _require_open_collaboration(
        occurrence: OperationalOccurrenceRecord,
    ) -> None:
        if occurrence.response_state == CommandResponseState.RESOLVED.value:
            raise ValueError("INCIDENT_COLLABORATION_RESOLVED_READ_ONLY")

    def _collaboration_replay(
        self, session: Session, payload: Mapping[str, Any]
    ) -> CollaborationCommandResult:
        timeline = session.get(IncidentTimelineEntryRecord, int(payload["timeline_id"]))
        if timeline is None:
            raise RuntimeError("INCIDENT_COLLABORATION_RECEIPT_INVALID")
        raw_task_id = payload.get("task_id")
        task = (
            None
            if raw_task_id is None
            else session.get(IncidentTaskRecord, int(raw_task_id))
        )
        if raw_task_id is not None and task is None:
            raise RuntimeError("INCIDENT_COLLABORATION_RECEIPT_INVALID")
        return CollaborationCommandResult(
            task=None if task is None else self._task_view(task),
            timeline=self._timeline_view(timeline, session=session),
            replayed=True,
        )

    @staticmethod
    def _complete_collaboration_receipt(
        receipt: CommandReceiptRecord,
        *,
        task: IncidentTaskRecord | None,
        timeline: IncidentTimelineEntryRecord,
        now: datetime,
    ) -> None:
        receipt.state = "COMPLETED"
        receipt.response_json = json.dumps(
            {
                "task_id": None if task is None else task.id,
                "timeline_id": timeline.id,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        receipt.updated_at = _stored(now)

    def create_task(
        self,
        occurrence_id: int,
        *,
        draft: IncidentTaskDraft,
        actor: InteractiveOperatorActor,
        idempotency_key: str,
        request_id: str,
        source_ip: str,
        now: datetime,
    ) -> CollaborationCommandResult:
        payload = {
            "kind": "TASK_CREATE",
            "occurrence_id": occurrence_id,
            "title": draft.title,
            "description": draft.description,
            "due_at": None if draft.due_at is None else draft.due_at.isoformat(),
            "runbook_link": draft.runbook_link,
        }
        with self._sessions.begin() as session:
            receipt, replay = self._collaboration_request(
                session,
                occurrence_id=occurrence_id,
                idempotency_key=idempotency_key,
                payload=payload,
                now=now,
            )
            if replay is not None:
                return self._collaboration_replay(session, replay)
            occurrence = session.get(OperationalOccurrenceRecord, occurrence_id)
            if occurrence is None:
                raise LookupError("OPERATIONAL_OCCURRENCE_NOT_FOUND")
            self._require_open_collaboration(occurrence)
            task = IncidentTaskRecord(
                occurrence_id=occurrence_id,
                title=draft.title,
                description=draft.description,
                due_at=None if draft.due_at is None else _stored(draft.due_at),
                runbook_link=draft.runbook_link,
                status=IncidentTaskState.TODO.value,
                result=None,
                version=1,
                created_at=_stored(now),
                updated_at=_stored(now),
            )
            session.add(task)
            session.flush()
            timeline = self._append_timeline(
                session,
                occurrence_id=occurrence_id,
                actor=actor,
                event_type="TASK_CREATED",
                summary="操作者建立了人工处置任务",
                detail={
                    "task_id": task.id,
                    "title": task.title,
                    "status": task.status,
                    "due_at": None if draft.due_at is None else draft.due_at.isoformat(),
                    "runbook_link": task.runbook_link,
                },
                request_id=request_id,
                source_ip=source_ip,
                now=now,
            )
            occurrence.latest_activity_at = _stored(now)
            self._complete_collaboration_receipt(
                receipt, task=task, timeline=timeline, now=now
            )
            return CollaborationCommandResult(
                self._task_view(task),
                self._timeline_view(timeline, session=session),
                False,
            )

    def transition_task(
        self,
        occurrence_id: int,
        task_id: int,
        *,
        expected_version: int,
        target: IncidentTaskState,
        result: str | None,
        reason: str | None,
        actor: InteractiveOperatorActor,
        idempotency_key: str,
        request_id: str,
        source_ip: str,
        now: datetime,
    ) -> CollaborationCommandResult:
        payload = {
            "kind": "TASK_TRANSITION",
            "occurrence_id": occurrence_id,
            "task_id": task_id,
            "expected_version": expected_version,
            "target": target.value,
            "result": result,
            "reason": reason,
        }
        with self._sessions.begin() as session:
            receipt, replay = self._collaboration_request(
                session,
                occurrence_id=occurrence_id,
                idempotency_key=idempotency_key,
                payload=payload,
                now=now,
            )
            if replay is not None:
                return self._collaboration_replay(session, replay)
            task = session.get(IncidentTaskRecord, task_id)
            if task is None or task.occurrence_id != occurrence_id:
                raise LookupError("INCIDENT_TASK_NOT_FOUND")
            occurrence = session.get(OperationalOccurrenceRecord, occurrence_id)
            if occurrence is None:
                raise LookupError("OPERATIONAL_OCCURRENCE_NOT_FOUND")
            self._require_open_collaboration(occurrence)
            if task.version != expected_version:
                raise FileExistsError("INCIDENT_TASK_VERSION_CONFLICT")
            previous = IncidentTaskState(task.status)
            decision = decide_task_transition(
                previous=previous,
                target=target,
                result=result,
                reason=reason,
            )
            changed = session.scalar(
                update(IncidentTaskRecord)
                .where(
                    IncidentTaskRecord.id == task_id,
                    IncidentTaskRecord.version == expected_version,
                )
                .values(
                    status=decision.target.value,
                    result=decision.result,
                    version=expected_version + 1,
                    updated_at=_stored(now),
                )
                .returning(IncidentTaskRecord.id)
            )
            if changed is None:
                raise FileExistsError("INCIDENT_TASK_VERSION_CONFLICT")
            session.refresh(task)
            timeline = self._append_timeline(
                session,
                occurrence_id=occurrence_id,
                actor=actor,
                event_type="TASK_STATE_CHANGED",
                summary="操作者更新了人工处置任务状态",
                detail={
                    "task_id": task.id,
                    "title": task.title,
                    "before_status": previous.value,
                    "after_status": decision.target.value,
                    "result": decision.result,
                    "reason": decision.reason,
                },
                request_id=request_id,
                source_ip=source_ip,
                now=now,
            )
            occurrence.latest_activity_at = _stored(now)
            self._complete_collaboration_receipt(
                receipt, task=task, timeline=timeline, now=now
            )
            return CollaborationCommandResult(
                self._task_view(task),
                self._timeline_view(timeline, session=session),
                False,
            )

    def add_note(
        self,
        occurrence_id: int,
        *,
        text: str,
        actor: InteractiveOperatorActor,
        idempotency_key: str,
        request_id: str,
        source_ip: str,
        now: datetime,
    ) -> CollaborationCommandResult:
        payload = {"kind": "NOTE_CREATE", "occurrence_id": occurrence_id, "text": text}
        with self._sessions.begin() as session:
            receipt, replay = self._collaboration_request(
                session,
                occurrence_id=occurrence_id,
                idempotency_key=idempotency_key,
                payload=payload,
                now=now,
            )
            if replay is not None:
                return self._collaboration_replay(session, replay)
            occurrence = session.get(OperationalOccurrenceRecord, occurrence_id)
            if occurrence is None:
                raise LookupError("OPERATIONAL_OCCURRENCE_NOT_FOUND")
            self._require_open_collaboration(occurrence)
            timeline = self._append_timeline(
                session,
                occurrence_id=occurrence_id,
                actor=actor,
                event_type="MANUAL_NOTE",
                summary="操作者补充了人工处置 Note",
                detail={"storage": "ENCRYPTED_NOTE_CONTENT"},
                request_id=request_id,
                source_ip=source_ip,
                now=now,
            )
            note = IncidentNoteContentRecord(
                occurrence_id=occurrence_id,
                timeline_entry_id=timeline.id,
                content_envelope=self._encrypt(text),
                redacted_at=None,
                purge_after=None,
            )
            session.add(note)
            occurrence.latest_activity_at = _stored(now)
            session.flush()
            self._complete_collaboration_receipt(
                receipt, task=None, timeline=timeline, now=now
            )
            return CollaborationCommandResult(
                None,
                self._timeline_view(timeline, session=session),
                False,
            )

    def redact_note(
        self,
        occurrence_id: int,
        sequence: int,
        *,
        reason: str,
        actor: InteractiveOperatorActor,
        idempotency_key: str,
        request_id: str,
        source_ip: str,
        now: datetime,
    ) -> CollaborationCommandResult:
        payload = {
            "kind": "NOTE_REDACT",
            "occurrence_id": occurrence_id,
            "sequence": sequence,
            "reason": reason,
        }
        with self._sessions.begin() as session:
            receipt, replay = self._collaboration_request(
                session,
                occurrence_id=occurrence_id,
                idempotency_key=idempotency_key,
                payload=payload,
                now=now,
            )
            if replay is not None:
                return self._collaboration_replay(session, replay)
            original = session.scalar(
                select(IncidentTimelineEntryRecord).where(
                    IncidentTimelineEntryRecord.occurrence_id == occurrence_id,
                    IncidentTimelineEntryRecord.sequence == sequence,
                    IncidentTimelineEntryRecord.event_type == "MANUAL_NOTE",
                )
            )
            if original is None:
                raise LookupError("INCIDENT_NOTE_NOT_FOUND")
            note = session.scalar(
                select(IncidentNoteContentRecord).where(
                    IncidentNoteContentRecord.timeline_entry_id == original.id
                )
            )
            if note is None:
                raise RuntimeError("INCIDENT_NOTE_CONTENT_MISSING")
            if note.redacted_at is not None:
                raise ValueError("INCIDENT_NOTE_ALREADY_REDACTED")
            note.redacted_at = _stored(now)
            note.purge_after = _stored(now + timedelta(days=7))
            timeline = self._append_timeline(
                session,
                occurrence_id=occurrence_id,
                actor=actor,
                event_type="NOTE_REDACTED",
                summary="操作者脱敏了一条人工处置 Note",
                detail={
                    "note_id": note.id,
                    "original_sequence": sequence,
                    "reason": reason,
                    "encrypted_original_purge_after": _aware(note.purge_after).isoformat(),
                },
                request_id=request_id,
                source_ip=source_ip,
                now=now,
            )
            occurrence = session.get(OperationalOccurrenceRecord, occurrence_id)
            if occurrence is not None:
                occurrence.latest_activity_at = _stored(now)
            self._complete_collaboration_receipt(
                receipt, task=None, timeline=timeline, now=now
            )
            return CollaborationCommandResult(
                None,
                self._timeline_view(timeline, session=session),
                False,
            )

    @staticmethod
    def _response_payload(result: ResponseCommandResult) -> dict[str, Any]:
        return {
            "occurrence_id": result.occurrence_id,
            "previous_state": result.previous_state,
            "current_state": result.current_state,
            "resolution_code": result.resolution_code,
            "version": result.version,
            "timeline": [
                {
                    "id": item.id,
                    "occurrence_id": item.occurrence_id,
                    "sequence": item.sequence,
                    "actor_type": item.actor_type,
                    "event_type": item.event_type,
                    "summary": item.summary,
                    "detail": item.detail,
                    "request_id": item.request_id,
                    "source_ip": item.source_ip,
                    "created_at": item.created_at.isoformat(),
                }
                for item in result.timeline
            ],
        }

    @staticmethod
    def _response_from_payload(payload: Mapping[str, Any]) -> ResponseCommandResult:
        raw_timeline = payload.get("timeline")
        if not isinstance(raw_timeline, list):
            raise RuntimeError("INCIDENT_RESPONSE_RECEIPT_INVALID")
        timeline = tuple(
            TimelineEntryView(
                id=int(item["id"]),
                occurrence_id=int(item["occurrence_id"]),
                sequence=int(item["sequence"]),
                actor_type=str(item["actor_type"]),
                event_type=str(item["event_type"]),
                summary=str(item["summary"]),
                detail=cast(dict[str, Any], item["detail"]),
                request_id=str(item["request_id"]),
                source_ip=str(item["source_ip"]),
                created_at=datetime.fromisoformat(str(item["created_at"])),
            )
            for item in raw_timeline
            if isinstance(item, dict)
        )
        if len(timeline) != len(raw_timeline):
            raise RuntimeError("INCIDENT_RESPONSE_RECEIPT_INVALID")
        return ResponseCommandResult(
            occurrence_id=int(payload["occurrence_id"]),
            previous_state=str(payload["previous_state"]),
            current_state=str(payload["current_state"]),
            resolution_code=(
                None
                if payload.get("resolution_code") is None
                else str(payload["resolution_code"])
            ),
            version=int(payload["version"]),
            timeline=timeline,
            replayed=True,
        )

    def execute_response_command(
        self,
        occurrence_id: int,
        *,
        action: ResponseAction,
        actor: InteractiveOperatorActor,
        expected_version: int,
        idempotency_key: str,
        request_id: str,
        source_ip: str,
        now: datetime,
    ) -> ResponseCommandResult:
        action_payload = {
            "occurrence_id": occurrence_id,
            "expected_version": expected_version,
            "kind": action.kind.value,
            "resolution_code": (
                None if action.resolution_code is None else action.resolution_code.value
            ),
            "duplicate_of": action.duplicate_of,
            "reason": action.reason,
        }
        rendered_request = json.dumps(
            action_payload, sort_keys=True, separators=(",", ":")
        )
        key_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        request_hash = hashlib.sha256(rendered_request.encode("utf-8")).hexdigest()
        scope = f"occurrence-response:{occurrence_id}"
        stored_now = _stored(now)
        with self._sessions.begin() as session:
            receipt = session.scalar(
                select(CommandReceiptRecord).where(
                    CommandReceiptRecord.scope == scope,
                    CommandReceiptRecord.key_hash == key_hash,
                )
            )
            if receipt is not None:
                if receipt.request_hash != request_hash:
                    raise CommandConflict("IDEMPOTENCY_KEY_REUSED")
                if receipt.state != "COMPLETED" or receipt.response_json is None:
                    raise CommandConflict("COMMAND_OUTCOME_UNKNOWN")
                replay: Any = json.loads(receipt.response_json)
                if not isinstance(replay, dict):
                    raise RuntimeError("INCIDENT_RESPONSE_RECEIPT_INVALID")
                return self._response_from_payload(replay)

            occurrence = session.get(OperationalOccurrenceRecord, occurrence_id)
            if occurrence is None:
                raise LookupError("OPERATIONAL_OCCURRENCE_NOT_FOUND")
            try:
                previous = CommandResponseState(occurrence.response_state)
                decision = decide_response(
                    previous=previous,
                    signal_state=occurrence.signal_state,
                    action=action,
                    actor=actor,
                )
            except PermissionError:
                raise
            except ValueError:
                raise
            if expected_version != occurrence.version:
                raise FileExistsError("OCCURRENCE_VERSION_CONFLICT")
            if action.resolution_code is not None and action.resolution_code.value == "DUPLICATE":
                target = session.get(
                    OperationalOccurrenceRecord, decision.duplicate_of
                )
                if (
                    target is None
                    or target.id == occurrence.id
                    or target.resolution_code == "DUPLICATE"
                    or target.duplicate_of_occurrence_id is not None
                ):
                    raise ValueError("DUPLICATE_TARGET_INVALID")

            receipt = CommandReceiptRecord(
                scope=scope,
                key_hash=key_hash,
                request_hash=request_hash,
                state="IN_PROGRESS",
                response_json=None,
                created_at=stored_now,
                updated_at=stored_now,
            )
            session.add(receipt)
            if action.kind is ResponseActionKind.RESOLVE:
                # Acquire SQLite's writer lock before checking the task set. A
                # concurrent task create must either be visible here or wait
                # until the resolved occurrence makes that create fail closed.
                session.flush()
                unfinished_task_id = session.scalar(
                    select(IncidentTaskRecord.id)
                    .where(
                        IncidentTaskRecord.occurrence_id == occurrence_id,
                        IncidentTaskRecord.status.in_(
                            (
                                IncidentTaskState.TODO.value,
                                IncidentTaskState.IN_PROGRESS.value,
                            )
                        ),
                    )
                    .limit(1)
                )
                if unfinished_task_id is not None:
                    raise ValueError("INCIDENT_OPEN_TASKS_REMAIN")
            next_version = occurrence.version + 1
            values: dict[str, Any] = {
                "response_state": decision.target_state.value,
                "latest_activity_at": stored_now,
                "version": next_version,
            }
            if decision.resolution_code is not None:
                values["resolution_code"] = decision.resolution_code.value
                values["duplicate_of_occurrence_id"] = decision.duplicate_of
                values["resolved_at"] = stored_now
            if action.kind is ResponseActionKind.START_HANDLING:
                if occurrence.acknowledged_at is None:
                    values["acknowledged_at"] = stored_now
            changed = session.scalar(
                update(OperationalOccurrenceRecord)
                .where(
                    OperationalOccurrenceRecord.id == occurrence_id,
                    OperationalOccurrenceRecord.version == expected_version,
                )
                .values(**values)
                .returning(OperationalOccurrenceRecord.id)
            )
            if changed is None:
                raise FileExistsError("OCCURRENCE_VERSION_CONFLICT")
            if (
                action.kind is ResponseActionKind.RESOLVE
                and self._investigation_cancel is not None
            ):
                self._investigation_cancel(session, occurrence_id, now)
            if self._notifications is not None:
                self._notifications.apply_occurrence_response_in_session(
                    session,
                    incident_id=occurrence.incident_id,
                    occurrence_no=occurrence.occurrence_no,
                    response_state=decision.target_state.value,
                    observed_at=now,
                )

            sequence = int(
                session.scalar(
                    select(func.max(IncidentTimelineEntryRecord.sequence)).where(
                        IncidentTimelineEntryRecord.occurrence_id == occurrence_id
                    )
                )
                or 0
            )
            summaries = {
                "RESPONSE_HANDLING_STARTED": "操作者已确认并开始处理本次事件",
                "OCCURRENCE_RESOLVED": "操作者已结束本次事件处理",
            }
            entries: list[IncidentTimelineEntryRecord] = []
            event_before = previous.value
            for event_type in decision.timeline_events:
                sequence += 1
                event_after = decision.target_state.value
                detail = {
                    "before_response_state": event_before,
                    "after_response_state": event_after,
                    "reason": decision.reason,
                    "resolution_code": (
                        None
                        if decision.resolution_code is None
                        else decision.resolution_code.value
                    ),
                    "duplicate_of_occurrence_id": decision.duplicate_of,
                    "signal_state": occurrence.signal_state,
                }
                entry = IncidentTimelineEntryRecord(
                    occurrence_id=occurrence_id,
                    sequence=sequence,
                    actor_type=actor.kind,
                    event_type=event_type,
                    summary=summaries[event_type],
                    detail_json=json.dumps(
                        detail, sort_keys=True, separators=(",", ":")
                    ),
                    request_id=request_id[:128],
                    source_ip=source_ip[:128],
                    created_at=stored_now,
                )
                session.add(entry)
                entries.append(entry)
                event_before = event_after
            session.flush()
            result = ResponseCommandResult(
                occurrence_id=occurrence_id,
                previous_state=previous.value,
                current_state=decision.target_state.value,
                resolution_code=(
                    None
                    if decision.resolution_code is None
                    else decision.resolution_code.value
                ),
                version=next_version,
                timeline=tuple(self._timeline_view(item) for item in entries),
                replayed=False,
            )
            receipt.state = "COMPLETED"
            receipt.response_json = json.dumps(
                self._response_payload(result), sort_keys=True, separators=(",", ":")
            )
            receipt.updated_at = stored_now
            return result

    def cleanup(self, *, now: datetime, runtime_days: int = 30, history_days: int = 365) -> RetentionView:
        runtime_cutoff = _stored(now - timedelta(days=max(0, runtime_days)))
        history_cutoff = _stored(now - timedelta(days=max(0, history_days)))
        alerts_deleted = audits_deleted = incidents_deleted = occurrences_deleted = 0
        with self._sessions.begin() as session:
            for note in session.scalars(
                select(IncidentNoteContentRecord).where(
                    IncidentNoteContentRecord.purge_after.is_not(None),
                    IncidentNoteContentRecord.purge_after <= _stored(now),
                    IncidentNoteContentRecord.content_envelope.is_not(None),
                )
            ):
                note.content_envelope = None
            for alert in session.scalars(select(AlertRecord)):
                if alert.source_state == "RECOVERED" and alert.last_seen_at < runtime_cutoff:
                    session.delete(alert)
                    alerts_deleted += 1
            for audit in session.scalars(select(IncidentAuditRecord)):
                if audit.created_at < runtime_cutoff:
                    session.delete(audit)
                    audits_deleted += 1
            for item in session.scalars(select(IncidentOccurrenceRecord)):
                if item.recovered_at < history_cutoff:
                    session.delete(item)
                    occurrences_deleted += 1
            session.flush()
            for incident in session.scalars(select(IncidentRecord)):
                if incident.source_state != "RECOVERED":
                    continue
                has_member = session.scalar(select(AlertRecord.id).where(AlertRecord.incident_id == incident.id).limit(1))
                has_audit = session.scalar(select(IncidentAuditRecord.id).where(IncidentAuditRecord.incident_id == incident.id).limit(1))
                has_route = session.scalar(select(NotificationRouteRecord.id).where(NotificationRouteRecord.incident_id == incident.id).limit(1))
                if has_member is None and has_audit is None and has_route is None:
                    session.delete(incident)
                    incidents_deleted += 1
        return RetentionView(alerts_deleted, audits_deleted, incidents_deleted, occurrences_deleted)
