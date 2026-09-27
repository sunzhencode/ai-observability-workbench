"""SQLAlchemy adapter for deterministic platform-notification noise controls."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from typing import Any, Mapping

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app.adapters.persistence.catalog import ServiceRecord
from app.adapters.persistence.incidents import (
    IncidentTimelineEntryRecord,
    OperationalOccurrenceRecord,
)
from app.adapters.persistence.sources import (
    AggregationRuleRecord,
    AlertRecord,
    IncidentRecord,
    SourceRecord,
)
from app.application.noise import (
    MaintenanceDraft,
    MaintenanceView,
    OccurrenceNoiseView,
    SourceNoiseControlsView,
    SuppressionView,
)
from app.domains.incidents.actors import InteractiveOperatorActor, require_interactive_operator
from app.domains.incidents.response import ResponseState
from app.platform.persistence.codecs import (
    aware_utc as _aware,
    canonical_json as _json,
    stored_utc as _stored,
)
from app.domains.incidents.models import IncidentLifecycleSnapshot
from app.domains.noise.models import (
    MAX_MAINTENANCE_SECONDS,
    STORM_WINDOW,
    NotificationNoiseDecision,
    decide_notification_noise,
    flapping_state,
    storm_state,
    validate_storm_thresholds,
    validate_suppression_duration,
    validate_window,
)
from app.domains.notifications.models import IncidentNotificationFact
from app.platform.persistence.database import SessionFactory


UTC = timezone.utc


class Base(DeclarativeBase):
    pass


class SourceNoiseControlRecord(Base):
    __tablename__ = "source_noise_control"

    source_id: Mapped[str] = mapped_column(
        String(128), primary_key=True
    )
    flapping_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    storm_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    storm_alert_threshold: Mapped[int] = mapped_column(Integer, nullable=False)
    storm_occurrence_threshold: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class AlertFlappingStateRecord(Base):
    __tablename__ = "alert_flapping_state"
    __table_args__ = (Index("ix_alert_flapping_active", "source_id", "flapping_since"),)

    source_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(256), primary_key=True)
    last_stable_state: Mapped[str] = mapped_column(String(16), nullable=False)
    transitions_json: Mapped[str] = mapped_column(Text, nullable=False)
    flapping_since: Mapped[datetime | None] = mapped_column(DateTime)
    last_transition_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class SourceStormStateRecord(Base):
    __tablename__ = "source_storm_state"

    source_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    active_since: Mapped[datetime | None] = mapped_column(DateTime)
    below_half_windows: Mapped[int] = mapped_column(Integer, nullable=False)
    window_started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    samples_json: Mapped[str] = mapped_column(Text, nullable=False)
    new_alert_count: Mapped[int] = mapped_column(Integer, nullable=False)
    new_occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class NoiseLifecycleFactRecord(Base):
    __tablename__ = "noise_lifecycle_fact"
    __table_args__ = (
        Index(
            "ix_noise_lifecycle_analytics",
            "occurred_at",
            "source_id",
            "service_key",
            "signal_severity",
            "kind",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fact_key: Mapped[str] = mapped_column(String(640), nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    transition: Mapped[str] = mapped_column(String(16), nullable=False)
    subject_key: Mapped[str] = mapped_column(String(512), nullable=False)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    service_key: Mapped[str] = mapped_column(String(32), nullable=False)
    signal_severity: Mapped[str] = mapped_column(String(16), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class StormNotificationSummaryRecord(Base):
    __tablename__ = "storm_notification_summary"
    __table_args__ = (Index("ix_storm_summary_window", "source_id", "window_started_at"),)

    summary_key: Mapped[str] = mapped_column(String(512), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    policy_revision_id: Mapped[int] = mapped_column(Integer, nullable=False)
    target_set_json: Mapped[str] = mapped_column(Text, nullable=False)
    route_target_id: Mapped[int] = mapped_column(Integer, nullable=False)
    window_started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class MaintenanceWindowRecord(Base):
    __tablename__ = "maintenance_window"
    __table_args__ = (Index("ix_maintenance_active_window", "status", "starts_at", "ends_at", "id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    source_id: Mapped[str | None] = mapped_column(String(128))
    service_id: Mapped[int | None] = mapped_column(Integer)
    aggregation_rule_id: Mapped[int | None] = mapped_column(Integer)
    starts_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class OccurrenceSuppressionRecord(Base):
    __tablename__ = "occurrence_suppression"
    __table_args__ = (Index("ix_suppression_occurrence_active", "occurrence_id", "status", "ends_at", "id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    occurrence_id: Mapped[int] = mapped_column(Integer, nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


def _required_aware(value: datetime) -> datetime:
    resolved = _aware(value)
    if resolved is None:
        raise RuntimeError("NOISE_TIMESTAMP_MISSING")
    return resolved


class SqlAlchemyNoiseStore:
    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    @staticmethod
    def _controls_view(
        item: SourceNoiseControlRecord, storm: SourceStormStateRecord | None
    ) -> SourceNoiseControlsView:
        return SourceNoiseControlsView(
            item.source_id,
            item.flapping_enabled,
            item.storm_enabled,
            item.storm_alert_threshold,
            item.storm_occurrence_threshold,
            storm is not None and storm.active_since is not None,
            None if storm is None else _aware(storm.active_since),
            item.version,
        )

    @staticmethod
    def _ensure_controls(
        session: Session, source_id: str, *, now: datetime
    ) -> SourceNoiseControlRecord:
        if session.get(SourceRecord, source_id) is None:
            raise LookupError("SOURCE_NOT_FOUND")
        item = session.get(SourceNoiseControlRecord, source_id)
        if item is None:
            item = SourceNoiseControlRecord(
                source_id=source_id,
                flapping_enabled=True,
                storm_enabled=True,
                storm_alert_threshold=100,
                storm_occurrence_threshold=20,
                version=1,
                updated_at=_stored(now),
            )
            session.add(item)
            session.flush()
        return item

    @staticmethod
    def _record_lifecycle(
        session: Session,
        *,
        kind: str,
        transition: str,
        subject_key: str,
        source_id: str,
        service_key: str,
        signal_severity: str,
        occurred_at: datetime,
    ) -> None:
        timestamp = occurred_at.astimezone(UTC).isoformat()
        fact_key = f"{kind}:{source_id}:{subject_key}:{transition}:{timestamp}"
        if session.scalar(
            select(NoiseLifecycleFactRecord.id).where(
                NoiseLifecycleFactRecord.fact_key == fact_key
            )
        ) is not None:
            return
        session.add(
            NoiseLifecycleFactRecord(
                fact_key=fact_key,
                kind=kind,
                transition=transition,
                subject_key=subject_key,
                source_id=source_id,
                service_key=service_key,
                signal_severity=signal_severity,
                occurred_at=_stored(occurred_at),
            )
        )

    @staticmethod
    def _alert_dimensions(
        session: Session, alert: AlertRecord
    ) -> tuple[str, str]:
        if alert.incident_id is None:
            return "__UNMAPPED__", "unknown"
        occurrence = session.scalar(
            select(OperationalOccurrenceRecord)
            .where(OperationalOccurrenceRecord.incident_id == alert.incident_id)
            .order_by(OperationalOccurrenceRecord.occurrence_no.desc())
        )
        if occurrence is None:
            return "__UNMAPPED__", "unknown"
        return (
            "__UNMAPPED__" if occurrence.service_id is None else str(occurrence.service_id),
            occurrence.signal_severity,
        )

    def get_source_controls(self, source_id: str) -> SourceNoiseControlsView:
        with self._sessions.begin() as session:
            item = self._ensure_controls(session, source_id, now=datetime.now(UTC))
            return self._controls_view(item, session.get(SourceStormStateRecord, source_id))

    def update_source_controls(
        self,
        source_id: str,
        *,
        flapping_enabled: bool,
        storm_enabled: bool,
        storm_alert_threshold: int,
        storm_occurrence_threshold: int,
        expected_version: int,
        actor: InteractiveOperatorActor,
        now: datetime,
    ) -> SourceNoiseControlsView:
        require_interactive_operator(actor)
        validate_storm_thresholds(storm_alert_threshold, storm_occurrence_threshold)
        with self._sessions.begin() as session:
            item = self._ensure_controls(session, source_id, now=now)
            if item.version != expected_version:
                raise FileExistsError("SOURCE_NOISE_VERSION_CONFLICT")
            item.flapping_enabled = flapping_enabled
            item.storm_enabled = storm_enabled
            item.storm_alert_threshold = storm_alert_threshold
            item.storm_occurrence_threshold = storm_occurrence_threshold
            item.version += 1
            item.updated_at = _stored(now)
            if not storm_enabled:
                storm = session.get(SourceStormStateRecord, source_id)
                if storm is not None:
                    if storm.active_since is not None:
                        self._record_lifecycle(
                            session,
                            kind="STORM",
                            transition="CLEARED",
                            subject_key=source_id,
                            source_id=source_id,
                            service_key="__ALL__",
                            signal_severity="unknown",
                            occurred_at=now,
                        )
                    storm.active_since = None
                    storm.below_half_windows = 0
            if not flapping_enabled:
                for flap in session.scalars(
                    select(AlertFlappingStateRecord).where(
                        AlertFlappingStateRecord.source_id == source_id
                    )
                ):
                    if flap.flapping_since is not None:
                        alert = session.scalar(
                            select(AlertRecord).where(
                                AlertRecord.source_id == source_id,
                                AlertRecord.upstream_fingerprint == flap.fingerprint,
                            )
                        )
                        service_key, severity = (
                            ("__UNMAPPED__", "unknown")
                            if alert is None
                            else self._alert_dimensions(session, alert)
                        )
                        self._record_lifecycle(
                            session,
                            kind="FLAPPING",
                            transition="CLEARED",
                            subject_key=flap.fingerprint,
                            source_id=source_id,
                            service_key=service_key,
                            signal_severity=severity,
                            occurred_at=now,
                        )
                    flap.flapping_since = None
            return self._controls_view(item, session.get(SourceStormStateRecord, source_id))

    @staticmethod
    def _maintenance_view(
        item: MaintenanceWindowRecord, *, now: datetime
    ) -> MaintenanceView:
        expired = item.status == "ACTIVE" and _required_aware(item.ends_at) <= now
        return MaintenanceView(
            item.id,
            item.scope_kind,
            item.source_id,
            item.service_id,
            item.aggregation_rule_id,
            _required_aware(item.starts_at),
            _required_aware(item.ends_at),
            item.reason,
            "ENDED" if expired else item.status,
            _required_aware(item.ends_at) if expired else _aware(item.ended_at),
            item.version,
            _required_aware(item.created_at),
        )

    @staticmethod
    def _suppression_view(item: OccurrenceSuppressionRecord) -> SuppressionView:
        return SuppressionView(
            item.id,
            item.occurrence_id,
            _required_aware(item.starts_at),
            _required_aware(item.ends_at),
            item.reason,
            item.status,
            _aware(item.ended_at),
            item.version,
            _required_aware(item.created_at),
        )

    def list_maintenance(self, *, include_ended: bool = False) -> tuple[MaintenanceView, ...]:
        now = datetime.now(UTC)
        with self._sessions() as session:
            statement = select(MaintenanceWindowRecord)
            if not include_ended:
                statement = statement.where(
                    MaintenanceWindowRecord.status == "ACTIVE",
                    MaintenanceWindowRecord.ends_at > _stored(now),
                )
            return tuple(
                self._maintenance_view(item, now=now)
                for item in session.scalars(statement.order_by(MaintenanceWindowRecord.starts_at, MaintenanceWindowRecord.id))
            )

    @staticmethod
    def _validate_scope(session: Session, draft: MaintenanceDraft) -> None:
        values = {
            "SOURCE": (draft.source_id, SourceRecord),
            "SERVICE": (draft.service_id, ServiceRecord),
            "RULE": (draft.aggregation_rule_id, AggregationRuleRecord),
        }
        if draft.scope_kind not in values:
            raise ValueError("MAINTENANCE_SCOPE_INVALID")
        target, model = values[draft.scope_kind]
        if target is None or session.get(model, target) is None:
            raise LookupError("MAINTENANCE_SCOPE_NOT_FOUND")
        supplied = sum(
            value is not None
            for value in (draft.source_id, draft.service_id, draft.aggregation_rule_id)
        )
        if supplied != 1:
            raise ValueError("MAINTENANCE_SCOPE_INVALID")

    def create_maintenance(
        self, draft: MaintenanceDraft, *, actor: InteractiveOperatorActor, now: datetime
    ) -> MaintenanceView:
        require_interactive_operator(actor)
        reason = draft.reason.strip()
        if not reason or len(reason) > 1000:
            raise ValueError("MAINTENANCE_REASON_INVALID")
        validate_window(
            starts_at=draft.starts_at,
            ends_at=draft.ends_at,
            max_seconds=MAX_MAINTENANCE_SECONDS,
        )
        if draft.ends_at <= now:
            raise ValueError("MAINTENANCE_WINDOW_INVALID")
        with self._sessions.begin() as session:
            self._validate_scope(session, draft)
            item = MaintenanceWindowRecord(
                scope_kind=draft.scope_kind,
                source_id=draft.source_id,
                service_id=draft.service_id,
                aggregation_rule_id=draft.aggregation_rule_id,
                starts_at=_stored(draft.starts_at),
                ends_at=_stored(draft.ends_at),
                reason=reason,
                status="ACTIVE",
                ended_at=None,
                version=1,
                created_at=_stored(now),
            )
            session.add(item)
            session.flush()
            return self._maintenance_view(item, now=now)

    def end_maintenance(
        self,
        maintenance_id: int,
        *,
        expected_version: int,
        actor: InteractiveOperatorActor,
        now: datetime,
    ) -> MaintenanceView:
        require_interactive_operator(actor)
        with self._sessions.begin() as session:
            item = session.get(MaintenanceWindowRecord, maintenance_id)
            if item is None:
                raise LookupError("MAINTENANCE_NOT_FOUND")
            if item.version != expected_version:
                raise FileExistsError("MAINTENANCE_VERSION_CONFLICT")
            if item.status == "ACTIVE":
                item.status = "ENDED"
                item.ended_at = _stored(now)
                item.version += 1
            return self._maintenance_view(item, now=now)

    @staticmethod
    def _append_timeline(
        session: Session,
        occurrence_id: int,
        *,
        event_type: str,
        summary: str,
        detail: Mapping[str, object],
        now: datetime,
    ) -> None:
        sequence = int(
            session.scalar(
                select(func.max(IncidentTimelineEntryRecord.sequence)).where(
                    IncidentTimelineEntryRecord.occurrence_id == occurrence_id
                )
            )
            or 0
        ) + 1
        session.add(
            IncidentTimelineEntryRecord(
                occurrence_id=occurrence_id,
                sequence=sequence,
                actor_type="INTERACTIVE_OPERATOR",
                event_type=event_type,
                summary=summary,
                detail_json=_json(detail),
                request_id="occurrence-noise-command",
                source_ip="local-interactive",
                created_at=_stored(now),
            )
        )

    def create_suppression(
        self,
        occurrence_id: int,
        *,
        duration_seconds: int,
        reason: str,
        actor: InteractiveOperatorActor,
        now: datetime,
    ) -> SuppressionView:
        require_interactive_operator(actor)
        validate_suppression_duration(duration_seconds)
        clean_reason = reason.strip()
        if not clean_reason or len(clean_reason) > 1000:
            raise ValueError("SUPPRESSION_REASON_INVALID")
        with self._sessions.begin() as session:
            occurrence = session.get(OperationalOccurrenceRecord, occurrence_id)
            if occurrence is None:
                raise LookupError("OCCURRENCE_NOT_FOUND")
            if occurrence.response_state == ResponseState.RESOLVED.value:
                raise RuntimeError("OCCURRENCE_RESOLVED_READ_ONLY")
            active = session.scalar(
                select(OccurrenceSuppressionRecord).where(
                    OccurrenceSuppressionRecord.occurrence_id == occurrence_id,
                    OccurrenceSuppressionRecord.status == "ACTIVE",
                    OccurrenceSuppressionRecord.ends_at > _stored(now),
                )
            )
            if active is not None:
                raise FileExistsError("SUPPRESSION_ALREADY_ACTIVE")
            item = OccurrenceSuppressionRecord(
                occurrence_id=occurrence_id,
                starts_at=_stored(now),
                ends_at=_stored(now + timedelta(seconds=duration_seconds)),
                reason=clean_reason,
                status="ACTIVE",
                ended_at=None,
                version=1,
                created_at=_stored(now),
            )
            session.add(item)
            session.flush()
            self._append_timeline(
                session,
                occurrence_id,
                event_type="SUPPRESSION_STARTED",
                summary="已暂停本次事件的平台协作通知",
                detail={"suppression_id": item.id, "ends_at": item.ends_at.isoformat()},
                now=now,
            )
            return self._suppression_view(item)

    def end_suppression(
        self,
        occurrence_id: int,
        *,
        expected_version: int,
        actor: InteractiveOperatorActor,
        now: datetime,
    ) -> SuppressionView:
        require_interactive_operator(actor)
        with self._sessions.begin() as session:
            item = session.scalar(
                select(OccurrenceSuppressionRecord)
                .where(
                    OccurrenceSuppressionRecord.occurrence_id == occurrence_id,
                    OccurrenceSuppressionRecord.status == "ACTIVE",
                )
                .order_by(OccurrenceSuppressionRecord.id.desc())
            )
            if item is None:
                raise LookupError("SUPPRESSION_NOT_FOUND")
            if item.version != expected_version:
                raise FileExistsError("SUPPRESSION_VERSION_CONFLICT")
            item.status = "ENDED"
            item.ended_at = _stored(now)
            item.version += 1
            self._append_timeline(
                session,
                occurrence_id,
                event_type="SUPPRESSION_ENDED",
                summary="已恢复本次事件后续的平台协作通知",
                detail={"suppression_id": item.id, "no_backfill": True},
                now=now,
            )
            return self._suppression_view(item)

    @staticmethod
    def _active_maintenance(
        session: Session,
        occurrence: OperationalOccurrenceRecord,
        incident: IncidentRecord,
        *,
        now: datetime,
    ) -> MaintenanceWindowRecord | None:
        active = tuple(
            session.scalars(
                select(MaintenanceWindowRecord).where(
                    MaintenanceWindowRecord.status == "ACTIVE",
                    MaintenanceWindowRecord.starts_at <= _stored(now),
                    MaintenanceWindowRecord.ends_at > _stored(now),
                )
            )
        )
        return next(
            (
                item
                for item in active
                if (item.scope_kind == "SOURCE" and item.source_id == occurrence.source_id)
                or (item.scope_kind == "SERVICE" and item.service_id == occurrence.service_id)
                or (
                    item.scope_kind == "RULE"
                    and item.aggregation_rule_id == incident.aggregation_rule_id
                )
            ),
            None,
        )

    @staticmethod
    def _active_suppression(
        session: Session, occurrence_id: int, *, now: datetime
    ) -> OccurrenceSuppressionRecord | None:
        return session.scalar(
            select(OccurrenceSuppressionRecord)
            .where(
                OccurrenceSuppressionRecord.occurrence_id == occurrence_id,
                OccurrenceSuppressionRecord.status == "ACTIVE",
                OccurrenceSuppressionRecord.starts_at <= _stored(now),
                OccurrenceSuppressionRecord.ends_at > _stored(now),
            )
            .order_by(OccurrenceSuppressionRecord.id.desc())
        )

    @staticmethod
    def _flapping_for_incident(
        session: Session, incident_id: int
    ) -> AlertFlappingStateRecord | None:
        incident = session.get(IncidentRecord, incident_id)
        if incident is None:
            return None
        fingerprints = select(AlertRecord.upstream_fingerprint).where(
            AlertRecord.incident_id == incident_id,
            AlertRecord.source_id == incident.source_id,
        )
        return session.scalar(
            select(AlertFlappingStateRecord).where(
                AlertFlappingStateRecord.source_id == incident.source_id,
                AlertFlappingStateRecord.fingerprint.in_(fingerprints),
                AlertFlappingStateRecord.flapping_since.is_not(None),
            )
        )

    @staticmethod
    def claim_storm_summary_in_session(
        session: Session,
        *,
        summary_key: str,
        source_id: str,
        policy_revision_id: int,
        target_set: tuple[str, ...],
        route_target_id: int,
        window_started_at: datetime,
        now: datetime,
    ) -> bool:
        if session.get(StormNotificationSummaryRecord, summary_key) is not None:
            return False
        session.add(
            StormNotificationSummaryRecord(
                summary_key=summary_key,
                source_id=source_id,
                policy_revision_id=policy_revision_id,
                target_set_json=_json(target_set),
                route_target_id=route_target_id,
                window_started_at=_stored(window_started_at),
                created_at=_stored(now),
            )
        )
        session.flush()
        return True

    def occurrence_noise_in_session(
        self, session: Session, occurrence_id: int, now: datetime
    ) -> OccurrenceNoiseView:
        occurrence = session.get(OperationalOccurrenceRecord, occurrence_id)
        if occurrence is None:
            raise LookupError("OCCURRENCE_NOT_FOUND")
        incident = session.get(IncidentRecord, occurrence.incident_id)
        if incident is None:
            raise LookupError("INCIDENT_NOT_FOUND")
        maintenance = self._active_maintenance(session, occurrence, incident, now=now)
        if maintenance is not None:
            return OccurrenceNoiseView(
                "MAINTENANCE",
                maintenance.reason,
                maintenance.scope_kind,
                _aware(maintenance.starts_at),
                _aware(maintenance.ends_at),
            )
        suppression = self._active_suppression(session, occurrence.id, now=now)
        if suppression is not None:
            return OccurrenceNoiseView(
                "SUPPRESSED",
                suppression.reason,
                "OCCURRENCE",
                _aware(suppression.starts_at),
                _aware(suppression.ends_at),
                suppression.id,
                suppression.version,
            )
        storm = session.get(SourceStormStateRecord, occurrence.source_id)
        if storm is not None and storm.active_since is not None:
            return OccurrenceNoiseView(
                "STORM",
                (
                    f"最近 5 分钟新增 {storm.new_alert_count} 条告警、"
                    f"{storm.new_occurrence_count} 个事件，平台协作消息已按来源汇总"
                ),
                "SOURCE",
                _aware(storm.active_since),
                None,
            )
        flap = self._flapping_for_incident(session, occurrence.incident_id)
        if flap is not None:
            return OccurrenceNoiseView(
                "FLAPPING", "成员告警在 15 分钟内反复触发与恢复", "ALERT", _aware(flap.flapping_since), None
            )
        rule = (
            None
            if incident.aggregation_rule_id is None
            else session.get(AggregationRuleRecord, incident.aggregation_rule_id)
        )
        grouping = 30 if rule is None else rule.grouping_window_seconds
        detected = _aware(occurrence.detected_at)
        if detected is not None and grouping and now < detected + timedelta(seconds=grouping):
            return OccurrenceNoiseView(
                "GROUPING",
                "正在等待同组告警汇合，事件已经可见",
                "AGGREGATION_RULE",
                detected,
                detected + timedelta(seconds=grouping),
            )
        return OccurrenceNoiseView("NONE", None, None, None, None)

    def occurrence_noise(self, occurrence_id: int, *, now: datetime) -> OccurrenceNoiseView:
        with self._sessions() as session:
            return self.occurrence_noise_in_session(session, occurrence_id, now)

    def resolve_notification_noise_in_session(
        self,
        session: Session,
        fact: IncidentNotificationFact,
        event_type: str,
        now: datetime,
    ) -> NotificationNoiseDecision:
        occurrence = session.scalar(
            select(OperationalOccurrenceRecord).where(
                OperationalOccurrenceRecord.incident_id == fact.incident_id,
                OperationalOccurrenceRecord.occurrence_no == fact.occurrence_no,
            )
        )
        incident = session.get(IncidentRecord, fact.incident_id)
        if occurrence is None or incident is None:
            return decide_notification_noise(
                event_type=event_type,
                severity=fact.severity,
                grouping_window_seconds=0,
                maintenance=False,
                suppression=False,
                storm=False,
                flapping=False,
            )
        rule = (
            None
            if incident.aggregation_rule_id is None
            else session.get(AggregationRuleRecord, incident.aggregation_rule_id)
        )
        control = session.get(SourceNoiseControlRecord, fact.source_id)
        storm = session.get(SourceStormStateRecord, fact.source_id)
        decision = decide_notification_noise(
            event_type=event_type,
            severity=fact.severity,
            grouping_window_seconds=30 if rule is None else rule.grouping_window_seconds,
            maintenance=self._active_maintenance(session, occurrence, incident, now=now) is not None,
            suppression=self._active_suppression(session, occurrence.id, now=now) is not None,
            storm=(
                control is not None
                and control.storm_enabled
                and storm is not None
                and storm.active_since is not None
            ),
            flapping=(
                control is None or control.flapping_enabled
            ) and self._flapping_for_incident(session, fact.incident_id) is not None,
        )
        if decision.state.value == "STORM" and storm is not None:
            return replace(
                decision,
                summary_alert_count=storm.new_alert_count,
                summary_occurrence_count=storm.new_occurrence_count,
            )
        return decision

    def observe_complete_collection_in_session(
        self,
        session: Any,
        *,
        source_id: str,
        previous_alert_states: Mapping[str, str],
        previous_incidents: Mapping[int, IncidentLifecycleSnapshot],
        observed_at: datetime,
    ) -> None:
        controls = self._ensure_controls(session, source_id, now=observed_at)
        current_alerts = tuple(
            session.scalars(select(AlertRecord).where(AlertRecord.source_id == source_id))
        )
        if controls.flapping_enabled:
            for alert in current_alerts:
                current = alert.source_state
                if current not in {"FIRING", "RECOVERED"}:
                    continue
                item = session.get(
                    AlertFlappingStateRecord,
                    {"source_id": source_id, "fingerprint": alert.upstream_fingerprint},
                )
                previous = previous_alert_states.get(alert.upstream_fingerprint)
                transitions: list[datetime] = []
                active_since: datetime | None = None
                if item is not None:
                    raw: Any = json.loads(item.transitions_json)
                    if isinstance(raw, list):
                        transitions = [datetime.fromisoformat(str(value)) for value in raw]
                    active_since = _aware(item.flapping_since)
                    previous = item.last_stable_state
                if previous in {"FIRING", "RECOVERED"} and previous != current:
                    transitions.append(observed_at)
                transitions = [
                    value for value in transitions if observed_at - value <= timedelta(minutes=45)
                ]
                next_active = flapping_state(
                    tuple(transitions), now=observed_at, active_since=active_since
                )
                if item is None:
                    item = AlertFlappingStateRecord(
                        source_id=source_id,
                        fingerprint=alert.upstream_fingerprint,
                        last_stable_state=current,
                        transitions_json="[]",
                        flapping_since=None,
                        last_transition_at=None,
                        updated_at=_stored(observed_at),
                    )
                    session.add(item)
                if active_since is None and next_active is not None:
                    service_key, severity = self._alert_dimensions(session, alert)
                    self._record_lifecycle(
                        session,
                        kind="FLAPPING",
                        transition="ACTIVATED",
                        subject_key=alert.upstream_fingerprint,
                        source_id=source_id,
                        service_key=service_key,
                        signal_severity=severity,
                        occurred_at=observed_at,
                    )
                elif active_since is not None and next_active is None:
                    service_key, severity = self._alert_dimensions(session, alert)
                    self._record_lifecycle(
                        session,
                        kind="FLAPPING",
                        transition="CLEARED",
                        subject_key=alert.upstream_fingerprint,
                        source_id=source_id,
                        service_key=service_key,
                        signal_severity=severity,
                        occurred_at=observed_at,
                    )
                item.last_stable_state = current
                item.transitions_json = _json([value.isoformat() for value in transitions])
                item.flapping_since = None if next_active is None else _stored(next_active)
                item.last_transition_at = None if not transitions else _stored(transitions[-1])
                item.updated_at = _stored(observed_at)

        current_incidents = tuple(
            session.scalars(select(IncidentRecord).where(IncidentRecord.source_id == source_id))
        )
        new_alerts = sum(
            alert.upstream_fingerprint not in previous_alert_states for alert in current_alerts
        )
        new_occurrences = sum(
            incident.id not in previous_incidents
            or incident.occurrence_no > previous_incidents[incident.id].occurrence_no
            for incident in current_incidents
        )
        storm = session.get(SourceStormStateRecord, source_id)
        samples: list[list[object]] = []
        if storm is not None:
            raw_samples: Any = json.loads(storm.samples_json)
            if isinstance(raw_samples, list):
                samples = [list(value) for value in raw_samples if isinstance(value, list)]
        samples.append([observed_at.isoformat(), new_alerts, new_occurrences])
        cutoff = observed_at - STORM_WINDOW
        samples = [
            value
            for value in samples
            if datetime.fromisoformat(str(value[0])) >= cutoff
        ]
        rolling_alerts = sum(int(str(value[1])) for value in samples)
        rolling_occurrences = sum(int(str(value[2])) for value in samples)
        was_active = storm is not None and storm.active_since is not None
        below = 0 if storm is None else storm.below_half_windows
        active, next_below = (
            storm_state(
                new_alerts=rolling_alerts,
                new_occurrences=rolling_occurrences,
                alert_threshold=controls.storm_alert_threshold,
                occurrence_threshold=controls.storm_occurrence_threshold,
                was_active=was_active,
                below_half_windows=below,
            )
            if controls.storm_enabled
            else (False, 0)
        )
        if storm is None:
            storm = SourceStormStateRecord(
                source_id=source_id,
                active_since=None,
                below_half_windows=0,
                window_started_at=_stored(cutoff),
                samples_json="[]",
                new_alert_count=0,
                new_occurrence_count=0,
                updated_at=_stored(observed_at),
            )
            session.add(storm)
        if active and not was_active:
            self._record_lifecycle(
                session,
                kind="STORM",
                transition="ACTIVATED",
                subject_key=source_id,
                source_id=source_id,
                service_key="__ALL__",
                signal_severity="unknown",
                occurred_at=observed_at,
            )
        elif was_active and not active:
            self._record_lifecycle(
                session,
                kind="STORM",
                transition="CLEARED",
                subject_key=source_id,
                source_id=source_id,
                service_key="__ALL__",
                signal_severity="unknown",
                occurred_at=observed_at,
            )
        storm.active_since = (
            _stored(observed_at)
            if active and not was_active
            else storm.active_since if active else None
        )
        storm.below_half_windows = next_below
        storm.window_started_at = _stored(cutoff)
        storm.samples_json = _json(samples)
        storm.new_alert_count = rolling_alerts
        storm.new_occurrence_count = rolling_occurrences
        storm.updated_at = _stored(observed_at)
