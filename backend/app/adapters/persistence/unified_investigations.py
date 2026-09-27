"""SQLAlchemy adapter for V2 unified Investigator runs and append-only facts."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta
import hashlib
import json
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint, delete, func, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app.adapters.persistence.jobs import JobRepository
from app.platform.persistence.codecs import (
    aware_utc as _aware,
    stored_utc as _stored,
    unicode_json as _json,
)
from app.application.unified_investigations import (
    InvestigatorFeedbackV2,
    MetricToolScopeV2,
    UnifiedInvestigationView,
)
from app.application.investigator_runtime import InvestigatorUsageDelta
from app.domains.incidents.actors import InteractiveOperatorActor, require_interactive_operator
from app.domains.investigations.runtime import (
    EvidenceFindingV2,
    EvidenceSnapshotV2,
    InvestigationActionV2,
    InvestigationActivityV2,
    InvestigationReportV2,
    InvestigationRunState,
    InvestigationVerdict,
    MetricObservationV2,
)
from app.domains.operations.jobs import JobPool, JobSpec
from app.platform.persistence.database import SessionFactory


class Base(DeclarativeBase):
    pass


class InvestigationRunV2Record(Base):
    __tablename__ = "investigation_run_v2"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    occurrence_id: Mapped[int] = mapped_column(Integer, nullable=False)
    request_key: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_profile_id: Mapped[str | None] = mapped_column(String(64))
    model_channel_id: Mapped[str | None] = mapped_column(String(128))
    model_revision: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    job_id: Mapped[str | None] = mapped_column(String(36))
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_ip: Mapped[str] = mapped_column(String(128), nullable=False)
    request_count: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_call_count: Mapped[int] = mapped_column(Integer, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    safe_error_code: Mapped[str | None] = mapped_column(String(96))
    model_execution_mode: Mapped[str] = mapped_column(
        String(24), nullable=False, default="UNKNOWN_LEGACY"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class EvidenceSnapshotV2Record(Base):
    __tablename__ = "evidence_snapshot_v2"

    investigation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    schema_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    alert_evidence_json: Mapped[str] = mapped_column(Text, nullable=False)
    metric_evidence_json: Mapped[str] = mapped_column(Text, nullable=False)
    degraded_domains_json: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class InvestigationActivityV2Record(Base):
    __tablename__ = "investigation_activity_v2"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    investigation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    safe_code: Mapped[str] = mapped_column(String(96), nullable=False)
    evidence_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class InvestigationReportV2Record(Base):
    __tablename__ = "investigation_report_v2"

    investigation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    schema_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    report_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class InvestigationToolScopeV2Record(Base):
    __tablename__ = "investigation_tool_scope_v2"

    investigation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    connection_version: Mapped[int | None] = mapped_column(Integer)
    catalog_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class InvestigationMetricObservationV2Record(Base):
    __tablename__ = "investigation_metric_observation_v2"

    evidence_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    investigation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    metric_id: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    summary_json: Mapped[str] = mapped_column(Text, nullable=False)
    sample_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class InvestigationFeedbackV2Record(Base):
    __tablename__ = "investigation_feedback_v2"
    __table_args__ = (
        UniqueConstraint(
            "investigation_id",
            "sequence",
            name="uq_investigation_feedback_v2_sequence",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    investigation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    rating: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


def _metric(payload: dict[str, Any]) -> MetricObservationV2:
    return MetricObservationV2(
        evidence_id=str(payload["evidence_id"]),
        metric_id=str(payload["metric_id"]),
        status=str(payload["status"]),
        summary=cast(dict[str, float | int | str | None], payload["summary"]),
        sample=tuple((float(item[0]), str(item[1])) for item in payload["sample"]),
    )


def _report(payload: dict[str, Any]) -> InvestigationReportV2:
    return InvestigationReportV2(
        summary_zh=str(payload["summary_zh"]),
        verdict=InvestigationVerdict(str(payload["verdict"])),
        confidence=float(payload["confidence"]),
        findings=tuple(
            EvidenceFindingV2(
                str(item["title_zh"]),
                str(item["analysis_zh"]),
                tuple(str(value) for value in item["evidence_ids"]),
            )
            for item in payload["findings"]
        ),
        recommended_actions=tuple(
            InvestigationActionV2(
                str(item["title_zh"]),
                str(item["rationale_zh"]),
                tuple(str(value) for value in item["evidence_ids"]),
            )
            for item in payload["recommended_actions"]
        ),
        missing_evidence_zh=tuple(str(value) for value in payload["missing_evidence_zh"]),
        degraded_domains=tuple(str(value) for value in payload["degraded_domains"]),
        evidence_gain=int(payload["evidence_gain"]),
    )


class SqlAlchemyUnifiedInvestigationStore:
    """Owns V2 facts; legacy P0/P1/P2 tables are intentionally never touched."""

    def __init__(self, sessions: SessionFactory, *, jobs: JobRepository) -> None:
        self._sessions = sessions
        self._jobs = jobs

    def _view(
        self, session: Any, row: InvestigationRunV2Record
    ) -> UnifiedInvestigationView:
        snapshot_row = session.get(EvidenceSnapshotV2Record, row.id)
        if snapshot_row is None:
            raise RuntimeError("INVESTIGATION_V2_SNAPSHOT_MISSING")
        raw_alerts = json.loads(snapshot_row.alert_evidence_json)
        raw_metrics = json.loads(snapshot_row.metric_evidence_json)
        raw_degraded = json.loads(snapshot_row.degraded_domains_json)
        if isinstance(raw_alerts, dict):
            alert_details = raw_alerts.get("detailed", [])
            member_alert_refs = raw_alerts.get("all_refs", [])
            alert_coverage = raw_alerts.get("coverage", [])
        else:
            alert_details = raw_alerts
            member_alert_refs = []
            alert_coverage = []
        snapshot = EvidenceSnapshotV2(
            investigation_id=row.id,
            occurrence_id=row.occurrence_id,
            alert_evidence=tuple(cast(list[dict[str, object]], alert_details)),
            metric_evidence=tuple(_metric(item) for item in raw_metrics),
            degraded_domains=tuple(str(value) for value in raw_degraded),
            member_alert_refs=tuple(str(value) for value in member_alert_refs),
            alert_coverage=tuple(
                cast(list[dict[str, object]], alert_coverage)
            ),
        )
        tool_scope_row = session.get(InvestigationToolScopeV2Record, row.id)
        if tool_scope_row is None:
            raise RuntimeError("INVESTIGATION_V2_TOOL_SCOPE_MISSING")
        raw_scope = json.loads(tool_scope_row.catalog_json)
        tool_scope = tuple(
            MetricToolScopeV2(
                metric_id=str(item["metric_id"]),
                display_name=str(item["display_name"]),
                description=str(item["description"]),
                unit=str(item["unit"]),
                expression=str(item["expression"]),
                scope_id=str(item.get("scope_id") or item["metric_id"]),
                alert_ref=(
                    None if item.get("alert_ref") is None else str(item["alert_ref"])
                ),
            )
            for item in raw_scope
        )
        tool_observations = tuple(
            MetricObservationV2(
                evidence_id=item.evidence_id,
                metric_id=item.metric_id,
                status=item.status,
                summary=cast(
                    dict[str, float | int | str | None],
                    json.loads(item.summary_json),
                ),
                sample=tuple(
                    (float(value[0]), str(value[1]))
                    for value in json.loads(item.sample_json)
                ),
            )
            for item in session.scalars(
                select(InvestigationMetricObservationV2Record)
                .where(
                    InvestigationMetricObservationV2Record.investigation_id == row.id
                )
                .order_by(InvestigationMetricObservationV2Record.evidence_id)
            )
        )
        activity_rows = tuple(
            session.scalars(
                select(InvestigationActivityV2Record)
                .where(InvestigationActivityV2Record.investigation_id == row.id)
                .order_by(InvestigationActivityV2Record.sequence)
            )
        )
        activities = tuple(
            InvestigationActivityV2(
                item.sequence,
                item.kind,
                item.status,
                item.safe_code,
                tuple(str(value) for value in json.loads(item.evidence_ids_json)),
            )
            for item in activity_rows
        )
        report_row = session.get(InvestigationReportV2Record, row.id)
        report = None
        if report_row is not None:
            raw_report = json.loads(report_row.report_json)
            if not isinstance(raw_report, dict):
                raise RuntimeError("INVESTIGATION_V2_REPORT_INVALID")
            report = _report(raw_report)
        feedback_row = session.scalar(
            select(InvestigationFeedbackV2Record)
            .where(InvestigationFeedbackV2Record.investigation_id == row.id)
            .order_by(InvestigationFeedbackV2Record.sequence.desc())
            .limit(1)
        )
        return UnifiedInvestigationView(
            id=row.id,
            occurrence_id=row.occurrence_id,
            status=InvestigationRunState(row.status),
            job_id=row.job_id,
            provider_profile_id=row.provider_profile_id,
            model_channel_id=row.model_channel_id,
            model_revision=row.model_revision,
            snapshot=snapshot,
            tool_scope_source_id=tool_scope_row.source_id,
            tool_scope_connection_version=tool_scope_row.connection_version,
            tool_scope=tool_scope,
            observations=tool_observations,
            activities=activities,
            report=report,
            request_count=row.request_count,
            tool_call_count=row.tool_call_count,
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            safe_error_code=row.safe_error_code,
            feedback=(
                None
                if feedback_row is None
                else InvestigatorFeedbackV2(
                    feedback_row.sequence,
                    feedback_row.rating,
                    _aware(feedback_row.created_at),
                )
            ),
            created_at=_aware(row.created_at),
            updated_at=_aware(row.updated_at),
        )

    def create(
        self,
        *,
        occurrence_id: int,
        request_key: str,
        provider_profile_id: str | None,
        model_channel_id: str | None,
        model_revision: int | None,
        request_id: str,
        source_ip: str,
        snapshot: EvidenceSnapshotV2,
        source_id: str,
        connection_version: int | None,
        tool_scope: tuple[MetricToolScopeV2, ...],
        queue_model: bool,
        model_execution_mode: str = "UNKNOWN_LEGACY",
        now: datetime,
    ) -> UnifiedInvestigationView:
        with self._sessions.begin() as session:
            existing = session.scalar(
                select(InvestigationRunV2Record).where(
                    InvestigationRunV2Record.occurrence_id == occurrence_id,
                    InvestigationRunV2Record.request_key == request_key,
                )
            )
            if existing is not None:
                return self._view(session, existing)
            active = session.scalar(
                select(InvestigationRunV2Record.id).where(
                    InvestigationRunV2Record.occurrence_id == occurrence_id,
                    InvestigationRunV2Record.status.in_(
                        (
                            InvestigationRunState.QUEUED.value,
                            InvestigationRunState.RUNNING.value,
                        )
                    ),
                )
            )
            if active is not None:
                raise FileExistsError("INVESTIGATION_ALREADY_ACTIVE")
            investigation_id = str(uuid4())
            state = (
                InvestigationRunState.QUEUED
                if queue_model
                else InvestigationRunState.DEGRADED
            )
            row = InvestigationRunV2Record(
                id=investigation_id,
                occurrence_id=occurrence_id,
                request_key=request_key,
                provider_profile_id=provider_profile_id,
                model_channel_id=model_channel_id,
                model_revision=model_revision,
                status=state.value,
                job_id=None,
                request_id=request_id,
                source_ip=source_ip,
                request_count=0,
                tool_call_count=0,
                input_tokens=0,
                output_tokens=0,
                safe_error_code=None if queue_model else "MODEL_OR_METRIC_UNAVAILABLE",
                model_execution_mode=model_execution_mode,
                created_at=_stored(now),
                updated_at=_stored(now),
            )
            session.add(row)
            session.flush()
            alert_json = _json(
                {
                    "detailed": snapshot.alert_evidence,
                    "all_refs": snapshot.member_alert_refs,
                    "coverage": snapshot.alert_coverage,
                }
            )
            metric_json = _json(tuple(asdict(item) for item in snapshot.metric_evidence))
            degraded_json = _json(snapshot.degraded_domains)
            digest = hashlib.sha256(
                (alert_json + metric_json + degraded_json).encode("utf-8")
            ).hexdigest()
            session.add(
                EvidenceSnapshotV2Record(
                    investigation_id=investigation_id,
                    schema_revision=2,
                    alert_evidence_json=alert_json,
                    metric_evidence_json=metric_json,
                    degraded_domains_json=degraded_json,
                    content_hash=digest,
                    created_at=_stored(now),
                )
            )
            session.add(
                InvestigationToolScopeV2Record(
                    investigation_id=investigation_id,
                    source_id=source_id,
                    connection_version=connection_version,
                    catalog_json=_json(tuple(asdict(item) for item in tool_scope)),
                    created_at=_stored(now),
                )
            )
            if queue_model:
                job = self._jobs.enqueue(
                    session,
                    JobSpec(
                        kind="investigation.run-v2",
                        pool=JobPool.AI,
                        subject_type="investigation-v2",
                        subject_id=investigation_id,
                        payload={"investigation_id": investigation_id},
                        payload_revision=2,
                        idempotency_key=f"investigator-v2:{investigation_id}",
                    ),
                    now=now,
                )
                row.job_id = job.id
            session.flush()
            return self._view(session, row)

    def find_by_request(
        self, occurrence_id: int, request_key: str
    ) -> UnifiedInvestigationView | None:
        with self._sessions() as session:
            row = session.scalar(
                select(InvestigationRunV2Record).where(
                    InvestigationRunV2Record.occurrence_id == occurrence_id,
                    InvestigationRunV2Record.request_key == request_key,
                )
            )
            return None if row is None else self._view(session, row)

    def get(self, investigation_id: str) -> UnifiedInvestigationView:
        with self._sessions() as session:
            row = session.get(InvestigationRunV2Record, investigation_id)
            if row is None:
                raise LookupError("INVESTIGATION_V2_NOT_FOUND")
            return self._view(session, row)

    def is_cancel_requested(self, investigation_id: str) -> bool:
        with self._sessions() as session:
            status = session.scalar(
                select(InvestigationRunV2Record.status).where(
                    InvestigationRunV2Record.id == investigation_id
                )
            )
            if status is None:
                raise LookupError("INVESTIGATION_V2_NOT_FOUND")
            return status == InvestigationRunState.CANCELED.value

    def list_for_occurrence(self, occurrence_id: int) -> tuple[UnifiedInvestigationView, ...]:
        with self._sessions() as session:
            rows = tuple(
                session.scalars(
                    select(InvestigationRunV2Record)
                    .where(InvestigationRunV2Record.occurrence_id == occurrence_id)
                    .order_by(
                        InvestigationRunV2Record.created_at.desc(),
                        InvestigationRunV2Record.id.desc(),
                    )
                )
            )
            return tuple(self._view(session, row) for row in rows)

    def mark_running(self, investigation_id: str, *, now: datetime) -> None:
        with self._sessions.begin() as session:
            row = session.get(InvestigationRunV2Record, investigation_id)
            if row is None:
                raise LookupError("INVESTIGATION_V2_NOT_FOUND")
            if row.status == InvestigationRunState.QUEUED.value:
                row.status = InvestigationRunState.RUNNING.value
                row.updated_at = _stored(now)

    def record_observation(
        self,
        investigation_id: str,
        observation: MetricObservationV2,
        *,
        now: datetime,
    ) -> None:
        """Durably checkpoint each read-only tool result before the next model call."""

        with self._sessions.begin() as session:
            if session.get(InvestigationRunV2Record, investigation_id) is None:
                raise LookupError("INVESTIGATION_V2_NOT_FOUND")
            if session.get(
                InvestigationMetricObservationV2Record, observation.evidence_id
            ) is not None:
                return
            session.add(
                InvestigationMetricObservationV2Record(
                    evidence_id=observation.evidence_id,
                    investigation_id=investigation_id,
                    metric_id=observation.metric_id,
                    status=observation.status,
                    summary_json=_json(observation.summary),
                    sample_json=_json(observation.sample),
                    created_at=_stored(now),
                )
            )

    def record_usage(
        self,
        investigation_id: str,
        delta: InvestigatorUsageDelta,
        *,
        now: datetime,
    ) -> None:
        """Checkpoint a provider response before cancellation can hide its cost."""

        with self._sessions.begin() as session:
            row = session.get(InvestigationRunV2Record, investigation_id)
            if row is None:
                raise LookupError("INVESTIGATION_V2_NOT_FOUND")
            row.request_count += max(0, delta.request_count)
            row.tool_call_count += max(0, delta.tool_call_count)
            row.input_tokens += max(0, delta.input_tokens)
            row.output_tokens += max(0, delta.output_tokens)
            row.updated_at = _stored(now)

    def complete(
        self,
        investigation_id: str,
        *,
        observations: tuple[MetricObservationV2, ...],
        activities: tuple[InvestigationActivityV2, ...],
        report: InvestigationReportV2,
        request_count: int,
        tool_call_count: int,
        input_tokens: int,
        output_tokens: int,
        now: datetime,
    ) -> None:
        with self._sessions.begin() as session:
            row = session.get(InvestigationRunV2Record, investigation_id)
            if row is None:
                raise LookupError("INVESTIGATION_V2_NOT_FOUND")
            if row.status == InvestigationRunState.CANCELED.value:
                row.request_count = max(row.request_count, request_count)
                row.tool_call_count = max(row.tool_call_count, tool_call_count)
                row.input_tokens = max(row.input_tokens, input_tokens)
                row.output_tokens = max(row.output_tokens, output_tokens)
                row.updated_at = _stored(now)
                return
            if session.get(InvestigationReportV2Record, investigation_id) is not None:
                return
            for observation in observations:
                if session.get(
                    InvestigationMetricObservationV2Record, observation.evidence_id
                ) is not None:
                    continue
                session.add(
                    InvestigationMetricObservationV2Record(
                        evidence_id=observation.evidence_id,
                        investigation_id=investigation_id,
                        metric_id=observation.metric_id,
                        status=observation.status,
                        summary_json=_json(observation.summary),
                        sample_json=_json(observation.sample),
                        created_at=_stored(now),
                    )
                )
            for index, activity in enumerate(activities, start=1):
                session.add(
                    InvestigationActivityV2Record(
                        investigation_id=investigation_id,
                        sequence=index,
                        kind=activity.kind[:48],
                        status=activity.status[:24],
                        safe_code=activity.safe_code[:96],
                        evidence_ids_json=_json(activity.evidence_ids),
                        created_at=_stored(now),
                    )
                )
            session.add(
                InvestigationReportV2Record(
                    investigation_id=investigation_id,
                    schema_revision=2,
                    report_json=_json(asdict(report)),
                    created_at=_stored(now),
                )
            )
            row.status = InvestigationRunState.COMPLETED.value
            row.request_count = request_count
            row.tool_call_count = tool_call_count
            row.input_tokens = input_tokens
            row.output_tokens = output_tokens
            row.safe_error_code = None
            row.updated_at = _stored(now)

    def fail(
        self,
        investigation_id: str,
        *,
        safe_error_code: str,
        now: datetime,
    ) -> None:
        with self._sessions.begin() as session:
            row = session.get(InvestigationRunV2Record, investigation_id)
            if row is None:
                raise LookupError("INVESTIGATION_V2_NOT_FOUND")
            if row.status in {
                InvestigationRunState.COMPLETED.value,
                InvestigationRunState.CANCELED.value,
            }:
                return
            row.status = InvestigationRunState.FAILED.value
            row.safe_error_code = safe_error_code[:96]
            row.updated_at = _stored(now)

    def cancel(
        self, investigation_id: str, *, now: datetime
    ) -> UnifiedInvestigationView:
        with self._sessions.begin() as session:
            row = session.get(InvestigationRunV2Record, investigation_id)
            if row is None:
                raise LookupError("INVESTIGATION_V2_NOT_FOUND")
            if row.status in {
                InvestigationRunState.QUEUED.value,
                InvestigationRunState.RUNNING.value,
            }:
                row.status = InvestigationRunState.CANCELED.value
                row.safe_error_code = "OPERATOR_CANCELED"
                row.updated_at = _stored(now)
            session.flush()
            return self._view(session, row)

    def request_cancel_for_occurrence_in_session(
        self,
        session: Session,
        occurrence_id: int,
        now: datetime,
    ) -> None:
        rows = session.scalars(
            select(InvestigationRunV2Record).where(
                InvestigationRunV2Record.occurrence_id == occurrence_id,
                InvestigationRunV2Record.status.in_(
                    (
                        InvestigationRunState.QUEUED.value,
                        InvestigationRunState.RUNNING.value,
                    )
                ),
            )
        )
        for row in rows:
            row.status = InvestigationRunState.CANCELED.value
            row.safe_error_code = "OCCURRENCE_RESOLVED"
            row.updated_at = _stored(now)

    def record_feedback(
        self,
        investigation_id: str,
        *,
        rating: str,
        actor: InteractiveOperatorActor,
        now: datetime,
    ) -> InvestigatorFeedbackV2:
        require_interactive_operator(actor)
        if rating not in {"USEFUL", "NOT_USEFUL", "ADOPTED"}:
            raise ValueError("INVESTIGATION_FEEDBACK_INVALID")
        with self._sessions.begin() as session:
            row = session.get(InvestigationRunV2Record, investigation_id)
            if row is None:
                raise LookupError("INVESTIGATION_V2_NOT_FOUND")
            if session.get(InvestigationReportV2Record, investigation_id) is None:
                raise RuntimeError("INVESTIGATION_FEEDBACK_REQUIRES_REPORT")
            sequence = int(
                session.scalar(
                    select(func.max(InvestigationFeedbackV2Record.sequence)).where(
                        InvestigationFeedbackV2Record.investigation_id
                        == investigation_id
                    )
                )
                or 0
            ) + 1
            session.add(
                InvestigationFeedbackV2Record(
                    investigation_id=investigation_id,
                    sequence=sequence,
                    rating=rating,
                    created_at=_stored(now),
                )
            )
            self._jobs.append_event(
                session,
                event_type="investigator-v2.feedback-recorded",
                subject_type="investigation-v2",
                subject_id=investigation_id,
                now=now,
            )
            return InvestigatorFeedbackV2(sequence, rating, now)

    def cleanup(self, *, now: datetime, retention_days: int = 90) -> int:
        """Delete terminal V2 runs after Analytics has finalized their facts."""

        cutoff = _stored(now - timedelta(days=retention_days))
        with self._sessions.begin() as session:
            session.execute(
                text("UPDATE investigation_retention_gate SET enabled=1 WHERE id=1")
            )
            expired_ids = select(InvestigationRunV2Record.id).where(
                InvestigationRunV2Record.created_at < cutoff,
                InvestigationRunV2Record.status.in_(
                    (
                        InvestigationRunState.COMPLETED.value,
                        InvestigationRunState.DEGRADED.value,
                        InvestigationRunState.FAILED.value,
                        InvestigationRunState.CANCELED.value,
                    )
                ),
            )
            for model in (
                InvestigationFeedbackV2Record,
                InvestigationMetricObservationV2Record,
                InvestigationActivityV2Record,
                InvestigationReportV2Record,
                InvestigationToolScopeV2Record,
                EvidenceSnapshotV2Record,
            ):
                session.execute(
                    delete(model).where(model.investigation_id.in_(expired_ids))
                )
            removed = cast(
                Any,
                session.execute(
                    delete(InvestigationRunV2Record).where(
                        InvestigationRunV2Record.id.in_(expired_ids)
                    )
                ),
            ).rowcount
            session.execute(
                text("UPDATE investigation_retention_gate SET enabled=0 WHERE id=1")
            )
        return int(removed or 0)


__all__ = ["SqlAlchemyUnifiedInvestigationStore", "UnifiedInvestigationView"]
