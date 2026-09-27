"""SQLAlchemy adapter for frozen investigation scope and typed evidence."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, time, timedelta, timezone
import json
from collections.abc import Callable
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import DateTime, Index, Integer, String, Text, UniqueConstraint, delete, func, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app.application.investigations import (
    AlertEvidenceView,
    InvestigationClaim,
    InvestigationDailyUsageView,
    InvestigationFeedbackView,
    InvestigationToolActionView,
    InvestigationUsageView,
    InvestigationView,
    MetricObservationView,
)
from app.application.evidence_expansion import AnalystExecutionContext, EvidenceExpansionContext
from app.platform.persistence.codecs import (
    aware_utc as _aware,
    canonical_json as _json,
    stored_utc as _stored,
)
from app.domains.incidents.actors import InteractiveOperatorActor, require_interactive_operator
from app.domains.investigations.models import (
    AlertScopeRef,
    EvidenceBriefV1,
    InvestigationDegradationV1,
    InvestigationPhase,
    InvestigationStatus,
    InitialInvestigationSnapshotV1,
    MetricEmptyObservationV1,
    MetricObservationV1,
    SimilarHistoryObservationV1,
    snapshot_hash,
)

from app.domains.investigations.analyst import (
    AnalystAlertSummaryV1,
    AnalystAlertV1,
    AnalystEmptyEvidenceV1,
    AnalystHistoryV1,
    AnalystMetricEvidenceV1,
    AnalystNoteV1,
    AnalystResultV1,
)
from app.domains.investigations.planner import (
    InvestigationBudgetV1,
    LabelRejectionV1,
    MetricDescriptorV1,
    PlannerAlertV1,
    PlannerEmptyFactV1,
    PlannerMetricFactV1,
    PlannerStepSummaryV1,
    PlannerStepV1,
)
from app.domains.investigations.prompt_profiles import EGRESS_CATEGORIES
from app.domains.operations.jobs import JobPool, JobSpec
from app.adapters.persistence.jobs import JobRepository
from app.platform.persistence.database import SessionFactory

UTC = timezone.utc
_BUDGET_TERMINATION_REASONS = frozenset(
    {
        "INVESTIGATION_WALL_CLOCK_LIMIT",
        "PLANNER_ROUND_LIMIT",
        "ANALYST_RESERVE_REQUIRED",
        "METRIC_QUERY_LIMIT",
        "ANALYST_REQUEST_EXCEEDS_RESERVE",
        "ANALYST_RESERVE_EXHAUSTED",
    }
)


class Base(DeclarativeBase):
    pass


class InvestigationRecord(Base):
    __tablename__ = "investigation"
    __table_args__ = (
        UniqueConstraint("occurrence_id", "request_key", name="uq_investigation_request"),
        Index("ix_investigation_occurrence_created", "occurrence_id", "created_at", "id"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    occurrence_id: Mapped[int] = mapped_column(Integer, nullable=False)
    incident_id: Mapped[int] = mapped_column(Integer, nullable=False)
    request_key: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    phase: Mapped[str] = mapped_column(String(48), nullable=False)
    member_scope_json: Mapped[str] = mapped_column(Text, nullable=False)
    member_total: Mapped[int] = mapped_column(Integer, nullable=False)
    member_detailed: Mapped[int] = mapped_column(Integer, nullable=False)
    query_planned: Mapped[int] = mapped_column(Integer, nullable=False)
    query_completed: Mapped[int] = mapped_column(Integer, nullable=False)
    successful_metric_facts: Mapped[int] = mapped_column(Integer, nullable=False)
    empty_metric_facts: Mapped[int] = mapped_column(Integer, nullable=False)
    degraded_domains_json: Mapped[str] = mapped_column(Text, nullable=False)
    findings_json: Mapped[str] = mapped_column(Text, nullable=False)
    job_id: Mapped[str | None] = mapped_column(String(36))
    model_cost_status: Mapped[str] = mapped_column(String(16), nullable=False)
    model_execution_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    claim_owner: Mapped[str | None] = mapped_column(String(128))
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_ip: Mapped[str] = mapped_column(String(128), nullable=False)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime)
    snapshot_occurrence_version: Mapped[int | None] = mapped_column(Integer)
    planner_rounds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    accounted_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    metric_queries_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    termination_reason: Mapped[str | None] = mapped_column(String(96))
    analyst_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    analyst_prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    analyst_completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class InvestigationSnapshotRecord(Base):
    __tablename__ = "initial_investigation_snapshot"
    investigation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    schema_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class InvestigationPhaseTransitionRecord(Base):
    __tablename__ = "investigation_phase_transition_v1"
    __table_args__ = (
        UniqueConstraint("investigation_id", "sequence", name="uq_investigation_phase_sequence"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    investigation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    from_phase: Mapped[str | None] = mapped_column(String(48))
    to_phase: Mapped[str] = mapped_column(String(48), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class InvestigationTrajectoryHeadRecord(Base):
    __tablename__ = "investigation_trajectory_head"
    investigation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    schema_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    latest_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class EvidenceBriefRecord(Base):
    __tablename__ = "evidence_brief_v1"
    investigation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    member_total: Mapped[int] = mapped_column(Integer, nullable=False)
    member_detailed: Mapped[int] = mapped_column(Integer, nullable=False)
    query_planned: Mapped[int] = mapped_column(Integer, nullable=False)
    query_completed: Mapped[int] = mapped_column(Integer, nullable=False)
    successful_metric_facts: Mapped[int] = mapped_column(Integer, nullable=False)
    empty_metric_facts: Mapped[int] = mapped_column(Integer, nullable=False)
    degraded_domains_json: Mapped[str] = mapped_column(Text, nullable=False)
    findings_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class MetricObservationRecord(Base):
    __tablename__ = "metric_observation_v1"
    evidence_ref: Mapped[str] = mapped_column(String(32), primary_key=True)
    investigation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    alert_ref: Mapped[str] = mapped_column(String(32), nullable=False)
    metric_name: Mapped[str] = mapped_column(String(256), nullable=False)
    l1_summary_json: Mapped[str] = mapped_column(Text, nullable=False)
    l2_sample_json: Mapped[str] = mapped_column(Text, nullable=False)
    l3_series_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class MetricEmptyObservationRecord(Base):
    __tablename__ = "metric_empty_observation_v1"
    evidence_ref: Mapped[str] = mapped_column(String(32), primary_key=True)
    investigation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    alert_ref: Mapped[str] = mapped_column(String(32), nullable=False)
    metric_name: Mapped[str] = mapped_column(String(256), nullable=False)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class SimilarHistoryObservationRecord(Base):
    __tablename__ = "similar_history_observation_v1"
    __table_args__ = (
        UniqueConstraint(
            "investigation_id", "rank", name="uq_similar_history_observation_rank"
        ),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    investigation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    occurrence_id: Mapped[int] = mapped_column(Integer, nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    match_reasons_json: Mapped[str] = mapped_column(Text, nullable=False)
    resolution_code: Mapped[str] = mapped_column(String(24), nullable=False)
    operator_conclusion: Mapped[str | None] = mapped_column(Text)
    task_outcome: Mapped[str | None] = mapped_column(Text)
    handling_duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    resolved_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class InvestigationDegradationRecord(Base):
    __tablename__ = "investigation_degradation_v1"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    investigation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    domain: Mapped[str] = mapped_column(String(64), nullable=False)
    code: Mapped[str] = mapped_column(String(96), nullable=False)
    message: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class PlannerStepRecord(Base):
    __tablename__ = "planner_step_v1"
    __table_args__ = (
        UniqueConstraint("investigation_id", "sequence", name="uq_planner_step_sequence"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    investigation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    alert_ref: Mapped[str | None] = mapped_column(String(32))
    metric_name: Mapped[str | None] = mapped_column(String(256))
    window: Mapped[str | None] = mapped_column(String(8))
    aggregation: Mapped[str | None] = mapped_column(String(16))
    label_filters_json: Mapped[str] = mapped_column(Text, nullable=False)
    group_by_json: Mapped[str] = mapped_column(Text, nullable=False)
    compiled_fingerprint: Mapped[str | None] = mapped_column(String(64))
    safe_code: Mapped[str | None] = mapped_column(String(96))
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    result_metric_names_json: Mapped[str] = mapped_column(Text, nullable=False)
    descriptor_type: Mapped[str | None] = mapped_column(String(32))
    descriptor_help: Mapped[str | None] = mapped_column(String(1024))
    descriptor_unit: Mapped[str | None] = mapped_column(String(64))
    descriptor_label_names_json: Mapped[str] = mapped_column(Text, nullable=False)
    descriptor_known_values_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class PlannerLabelRejectionRecord(Base):
    __tablename__ = "planner_label_rejection_v1"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    investigation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    alert_ref: Mapped[str] = mapped_column(String(32), nullable=False)
    label_name: Mapped[str] = mapped_column(String(128), nullable=False)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class InvestigationCanceledRecord(Base):
    __tablename__ = "investigation_canceled_v1"
    investigation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    reason: Mapped[str] = mapped_column(String(96), nullable=False)
    canceled_before: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class AnalystResultRecord(Base):
    __tablename__ = "analyst_result_v1"
    investigation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    contract_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt_profile_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    result_json: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class InvalidAnalystResponseRecord(Base):
    __tablename__ = "invalid_analyst_response"
    investigation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    envelope_json: Mapped[str] = mapped_column(Text, nullable=False)
    purge_after: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class InvestigationFeedbackRecord(Base):
    __tablename__ = "investigation_feedback_v1"
    __table_args__ = (
        UniqueConstraint("investigation_id", "sequence", name="uq_investigation_feedback_sequence"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    investigation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    rating: Mapped[str] = mapped_column(String(16), nullable=False)
    initiator_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class InvestigationDailyUsageRecord(Base):
    __tablename__ = "investigation_daily_usage_v1"
    day_utc: Mapped[str] = mapped_column(String(10), primary_key=True)
    schema_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    run_count: Mapped[int] = mapped_column(Integer, nullable=False)
    terminal_run_count: Mapped[int] = mapped_column(Integer, nullable=False)
    p2_valid_count: Mapped[int] = mapped_column(Integer, nullable=False)
    evidence_only_count: Mapped[int] = mapped_column(Integer, nullable=False)
    canceled_count: Mapped[int] = mapped_column(Integer, nullable=False)
    contract_rejected_count: Mapped[int] = mapped_column(Integer, nullable=False)
    dependency_failed_count: Mapped[int] = mapped_column(Integer, nullable=False)
    model_started_run_count: Mapped[int] = mapped_column(Integer, nullable=False)
    fake_model_started_run_count: Mapped[int] = mapped_column(Integer, nullable=False)
    external_model_started_run_count: Mapped[int] = mapped_column(Integer, nullable=False)
    unknown_model_started_run_count: Mapped[int] = mapped_column(Integer, nullable=False)
    planner_calls: Mapped[int] = mapped_column(Integer, nullable=False)
    analyst_calls: Mapped[int] = mapped_column(Integer, nullable=False)
    metric_queries: Mapped[int] = mapped_column(Integer, nullable=False)
    accounted_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    unknown_cost_run_count: Mapped[int] = mapped_column(Integer, nullable=False)
    feedback_response_count: Mapped[int] = mapped_column(Integer, nullable=False)
    feedback_adopted_count: Mapped[int] = mapped_column(Integer, nullable=False)
    degraded_run_count: Mapped[int] = mapped_column(Integer, nullable=False)
    p0_mtti_count: Mapped[int] = mapped_column(Integer, nullable=False)
    p0_mtti_sum_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    p1_mtti_count: Mapped[int] = mapped_column(Integer, nullable=False)
    p1_mtti_sum_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    p2_mtti_count: Mapped[int] = mapped_column(Integer, nullable=False)
    p2_mtti_sum_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class SqlAlchemyInvestigationStore:
    def __init__(
        self,
        sessions: SessionFactory,
        *,
        jobs: JobRepository,
        encrypt_invalid_raw: Callable[[str], str] | None = None,
    ) -> None:
        self._sessions = sessions
        self._jobs = jobs
        self._encrypt_invalid_raw = encrypt_invalid_raw

    @staticmethod
    def _daily_view(row: InvestigationDailyUsageRecord) -> InvestigationDailyUsageView:
        return InvestigationDailyUsageView(
            day_utc=date.fromisoformat(row.day_utc),
            schema_revision=row.schema_revision,
            run_count=row.run_count,
            terminal_run_count=row.terminal_run_count,
            p2_valid_count=row.p2_valid_count,
            evidence_only_count=row.evidence_only_count,
            canceled_count=row.canceled_count,
            contract_rejected_count=row.contract_rejected_count,
            dependency_failed_count=row.dependency_failed_count,
            model_started_run_count=row.model_started_run_count,
            fake_model_started_run_count=row.fake_model_started_run_count,
            external_model_started_run_count=row.external_model_started_run_count,
            unknown_model_started_run_count=row.unknown_model_started_run_count,
            planner_calls=row.planner_calls,
            analyst_calls=row.analyst_calls,
            metric_queries=row.metric_queries,
            accounted_tokens=row.accounted_tokens,
            unknown_cost_run_count=row.unknown_cost_run_count,
            feedback_response_count=row.feedback_response_count,
            feedback_adopted_count=row.feedback_adopted_count,
            degraded_run_count=row.degraded_run_count,
            p0_mtti_count=row.p0_mtti_count,
            p0_mtti_sum_ms=row.p0_mtti_sum_ms,
            p1_mtti_count=row.p1_mtti_count,
            p1_mtti_sum_ms=row.p1_mtti_sum_ms,
            p2_mtti_count=row.p2_mtti_count,
            p2_mtti_sum_ms=row.p2_mtti_sum_ms,
            updated_at=_aware(row.updated_at),
        )

    @staticmethod
    def _view(session: Session, row: InvestigationRecord) -> InvestigationView:
        foundation = session.get(EvidenceBriefRecord, row.id)
        degradations = tuple(
            InvestigationDegradationV1(item.domain, item.code, item.message)
            for item in session.scalars(
                select(InvestigationDegradationRecord)
                .where(InvestigationDegradationRecord.investigation_id == row.id)
                .order_by(InvestigationDegradationRecord.id)
            )
        )
        raw_planner_steps = tuple(
            session.scalars(
                select(PlannerStepRecord)
                .where(PlannerStepRecord.investigation_id == row.id)
                .order_by(PlannerStepRecord.sequence)
            )
        )
        planner_steps = tuple(
            PlannerStepSummaryV1(
                item.sequence,
                item.action,
                item.outcome,
                item.metric_name,
                item.safe_code,
                tuple(str(value) for value in json.loads(item.result_metric_names_json)),
            )
            for item in raw_planner_steps
        )
        alert_evidence = tuple(
            AlertEvidenceView(
                evidence_ref=str(item.get("alert_ref") or ""),
                alertname=str(item.get("alertname") or ""),
            )
            for item in json.loads(row.member_scope_json)
            if isinstance(item, dict) and item.get("alert_ref")
        )
        metric_observations = tuple(
            MetricObservationView(
                item.evidence_ref,
                item.alert_ref,
                item.metric_name,
                {
                    str(key): value
                    for key, value in json.loads(item.l1_summary_json).items()
                    if value is None or isinstance(value, (float, int, str))
                },
                tuple(
                    (float(point[0]), str(point[1]))
                    for point in json.loads(item.l2_sample_json)
                    if isinstance(point, list) and len(point) >= 2
                ),
            )
            for item in session.scalars(
                select(MetricObservationRecord)
                .where(MetricObservationRecord.investigation_id == row.id)
                .order_by(MetricObservationRecord.evidence_ref)
            )
        )
        analyst_row = session.get(AnalystResultRecord, row.id)
        analyst_result = (
            None
            if analyst_row is None
            else AnalystResultV1.model_validate_json(analyst_row.result_json)
        )
        snapshot = session.get(InvestigationSnapshotRecord, row.id)
        prompt_profile_id: str | None = None
        prompt_profile_name: str | None = None
        prompt_profile_revision: int | None = None
        playbook_id: str | None = None
        playbook_revision: int | None = None
        model_channel_id: str | None = None
        model_channel_revision: int | None = None
        if snapshot is not None:
            raw_snapshot = json.loads(snapshot.snapshot_json)
            raw_context = raw_snapshot.get("context") if isinstance(raw_snapshot, dict) else None
            if isinstance(raw_context, dict):
                raw_profile = raw_context.get("prompt_profile")
                if isinstance(raw_profile, dict):
                    prompt_profile_id = str(raw_profile.get("id") or "") or None
                    prompt_profile_name = str(raw_profile.get("name") or "") or None
                    raw_profile_revision = raw_profile.get("revision")
                    prompt_profile_revision = (
                        int(raw_profile_revision)
                        if isinstance(raw_profile_revision, int)
                        else None
                    )
                raw_playbook = raw_context.get("playbook")
                if isinstance(raw_playbook, dict):
                    playbook_id = str(raw_playbook.get("playbook_id") or "") or None
                    raw_playbook_revision = raw_playbook.get("revision")
                    playbook_revision = (
                        int(raw_playbook_revision)
                        if isinstance(raw_playbook_revision, int)
                        else None
                    )
                raw_model = raw_context.get("model_channel")
                if isinstance(raw_model, dict):
                    model_channel_id = str(raw_model.get("id") or "") or None
                    raw_model_revision = raw_model.get("revision_no")
                    model_channel_revision = (
                        int(raw_model_revision)
                        if isinstance(raw_model_revision, int)
                        else None
                    )
        latest_feedback = session.scalar(
            select(InvestigationFeedbackRecord)
            .where(InvestigationFeedbackRecord.investigation_id == row.id)
            .order_by(InvestigationFeedbackRecord.sequence.desc())
            .limit(1)
        )
        feedback = (
            None
            if latest_feedback is None
            else InvestigationFeedbackView(
                sequence=latest_feedback.sequence,
                rating=latest_feedback.rating,
                created_at=_aware(latest_feedback.created_at),
            )
        )
        tool_actions = tuple(
            InvestigationToolActionView(
                sequence=item.sequence,
                action=item.action,
                outcome=item.outcome,
                metric_name=item.metric_name,
                window=item.window,
                aggregation=item.aggregation,
                label_names=tuple(
                    sorted(
                        str(value.get("name"))
                        for value in json.loads(item.label_filters_json)
                        if isinstance(value, dict) and value.get("name")
                    )
                ),
                group_by=tuple(str(value) for value in json.loads(item.group_by_json)),
                safe_code=item.safe_code,
            )
            for item in raw_planner_steps
        )

        def mtti_ms(value: datetime | None) -> int | None:
            if value is None:
                return None
            return max(0, int((_aware(value) - _aware(row.created_at)).total_seconds() * 1000))

        p1_at = next(
            (item.created_at for item in raw_planner_steps if item.action == "QUERY_METRIC"),
            None,
        )
        usage = InvestigationUsageView(
            initiator_kind="INTERACTIVE_OPERATOR",
            request_id=row.request_id,
            source_ip=row.source_ip,
            planner_calls=row.planner_rounds,
            analyst_calls=row.analyst_calls,
            metric_queries=row.metric_queries_total,
            accounted_tokens=row.accounted_tokens,
            model_execution_mode=row.model_execution_mode,
            model_channel_id=model_channel_id,
            model_channel_revision=model_channel_revision,
            prompt_profile_revision=prompt_profile_revision,
            playbook_revision=playbook_revision,
            egress_categories=(
                EGRESS_CATEGORIES
                if row.planner_rounds > 0 or row.analyst_calls > 0
                else ()
            ),
            pricing_revision=None,
            cost_status=row.model_cost_status,
            p0_mtti_ms=mtti_ms(None if foundation is None else foundation.created_at),
            p1_mtti_ms=mtti_ms(p1_at),
            p2_mtti_ms=mtti_ms(None if analyst_row is None else analyst_row.created_at),
            query_yield=(
                None
                if row.metric_queries_total == 0
                else round(
                    min(
                        1.0,
                        (row.successful_metric_facts + row.empty_metric_facts)
                        / row.metric_queries_total,
                    ),
                    4,
                )
            ),
            evidence_gain=max(
                0,
                row.successful_metric_facts
                + row.empty_metric_facts
                - (0 if foundation is None else foundation.successful_metric_facts)
                - (0 if foundation is None else foundation.empty_metric_facts),
            ),
            degradation_count=len(degradations),
            canceled=row.cancel_requested_at is not None,
            tool_actions=tool_actions,
        )
        return InvestigationView(
            row.id,
            row.occurrence_id,
            row.incident_id,
            row.status,
            row.phase,
            row.request_key,
            row.member_total,
            row.member_detailed,
            row.query_planned,
            row.query_completed,
            row.successful_metric_facts,
            row.empty_metric_facts,
            0 if foundation is None else foundation.successful_metric_facts,
            0 if foundation is None else foundation.empty_metric_facts,
            tuple(json.loads(row.degraded_domains_json)),
            degradations,
            tuple(json.loads(row.findings_json)),
            row.job_id,
            row.model_cost_status,
            row.snapshot_occurrence_version,
            row.planner_rounds,
            row.accounted_tokens,
            row.metric_queries_total,
            row.termination_reason,
            None if row.cancel_requested_at is None else _aware(row.cancel_requested_at),
            planner_steps,
            alert_evidence,
            metric_observations,
            analyst_result,
            row.analyst_calls,
            row.analyst_prompt_tokens,
            row.analyst_completion_tokens,
            prompt_profile_id,
            prompt_profile_name,
            prompt_profile_revision,
            playbook_id,
            playbook_revision,
            usage,
            feedback,
            _aware(row.created_at),
            _aware(row.updated_at),
        )

    def record_feedback(
        self,
        investigation_id: str,
        *,
        rating: str,
        actor: InteractiveOperatorActor,
        now: datetime,
    ) -> InvestigationFeedbackView:
        require_interactive_operator(actor)
        if rating not in {"USEFUL", "NOT_USEFUL", "ADOPTED"}:
            raise ValueError("INVESTIGATION_FEEDBACK_INVALID")
        with self._sessions.begin() as session:
            if session.get(InvestigationRecord, investigation_id) is None:
                raise LookupError("INVESTIGATION_NOT_FOUND")
            if session.get(AnalystResultRecord, investigation_id) is None:
                raise RuntimeError("INVESTIGATION_FEEDBACK_REQUIRES_ANALYST_RESULT")
            sequence = int(
                session.scalar(
                    select(func.max(InvestigationFeedbackRecord.sequence)).where(
                        InvestigationFeedbackRecord.investigation_id == investigation_id
                    )
                )
                or 0
            ) + 1
            session.add(
                InvestigationFeedbackRecord(
                    investigation_id=investigation_id,
                    sequence=sequence,
                    rating=rating,
                    initiator_kind="INTERACTIVE_OPERATOR",
                    created_at=_stored(now),
                )
            )
            self._jobs.append_event(
                session,
                event_type="investigation.feedback-recorded",
                subject_type="investigation",
                subject_id=investigation_id,
                now=now,
            )
            return InvestigationFeedbackView(sequence, rating, now)

    def claim(
        self,
        *,
        occurrence_id: int,
        incident_id: int,
        request_key: str,
        member_scope: tuple[AlertScopeRef, ...],
        occurrence_version: int,
        actor: InteractiveOperatorActor,
        request_id: str,
        source_ip: str,
        model_execution_mode: str = "EXTERNAL",
        now: datetime,
    ) -> InvestigationClaim:
        require_interactive_operator(actor)
        if model_execution_mode not in {"FAKE", "EXTERNAL"}:
            raise ValueError("MODEL_EXECUTION_MODE_INVALID")
        with self._sessions.begin() as session:
            existing = session.scalar(
                select(InvestigationRecord).where(
                    InvestigationRecord.occurrence_id == occurrence_id,
                    InvestigationRecord.request_key == request_key,
                )
            )
            if existing is not None:
                return InvestigationClaim(self._view(session, existing), True)
            active = session.scalar(
                select(InvestigationRecord).where(
                    InvestigationRecord.occurrence_id == occurrence_id,
                    InvestigationRecord.status.in_(("PREPARING", "QUEUED")),
                )
            )
            if active is not None:
                if active.status == "PREPARING" and active.claim_expires_at is not None and _aware(active.claim_expires_at) <= now:
                    active.status = InvestigationStatus.FAILED.value
                    active.phase = InvestigationPhase.TERMINAL_EVIDENCE_ONLY.value
                    active.claim_owner = None
                    active.claim_expires_at = None
                    active.degraded_domains_json = _json(("claim",))
                    active.findings_json = _json(("上一次证据准备租约已过期，可重新发起调查",))
                    active.updated_at = _stored(now)
                    session.add(InvestigationDegradationRecord(
                        investigation_id=active.id,
                        domain="claim",
                        code="CLAIM_LEASE_EXPIRED",
                        message="上一次证据准备租约已过期；已保留原记录，可重新发起调查",
                        created_at=_stored(now),
                    ))
                else:
                    raise FileExistsError("INVESTIGATION_ALREADY_ACTIVE")
            investigation_id = str(uuid4())
            row = InvestigationRecord(
                id=investigation_id,
                occurrence_id=occurrence_id,
                incident_id=incident_id,
                request_key=request_key,
                status=InvestigationStatus.PREPARING.value,
                phase=InvestigationPhase.CLAIMED.value,
                member_scope_json=_json([{"alert_ref": item.alert_ref, "alert_id": item.alert_id, "alertname": item.alertname, "severity": item.severity, "source_state": item.source_state} for item in member_scope]),
                member_total=len(member_scope),
                member_detailed=min(len(member_scope), 20),
                query_planned=0,
                query_completed=0,
                successful_metric_facts=0,
                empty_metric_facts=0,
                degraded_domains_json="[]",
                findings_json="[]",
                job_id=None,
                model_cost_status="NOT_INCURRED",
                model_execution_mode=model_execution_mode,
                claim_owner="interactive-operator",
                claim_expires_at=_stored(now + timedelta(seconds=30)),
                request_id=request_id[:128],
                source_ip=source_ip[:128],
                cancel_requested_at=None,
                snapshot_occurrence_version=occurrence_version,
                planner_rounds=0,
                accounted_tokens=0,
                metric_queries_total=0,
                termination_reason=None,
                analyst_calls=0,
                analyst_prompt_tokens=0,
                analyst_completion_tokens=0,
                created_at=_stored(now),
                updated_at=_stored(now),
            )
            session.add(row)
            session.flush()
            session.add(InvestigationPhaseTransitionRecord(
                investigation_id=investigation_id,
                sequence=1,
                from_phase=None,
                to_phase=InvestigationPhase.CLAIMED.value,
                created_at=_stored(now),
            ))
            session.add(InvestigationTrajectoryHeadRecord(
                investigation_id=investigation_id,
                schema_revision=1,
                latest_sequence=1,
                created_at=_stored(now),
                updated_at=_stored(now),
            ))
            self._jobs.append_event(
                session,
                event_type="investigation.claimed",
                subject_type="investigation",
                subject_id=investigation_id,
                now=now,
            )
            session.flush()
            return InvestigationClaim(self._view(session, row), False)

    def finalize(
        self,
        investigation_id: str,
        *,
        snapshot: InitialInvestigationSnapshotV1,
        observations: tuple[MetricObservationV1, ...],
        empty_observations: tuple[MetricEmptyObservationV1, ...],
        similar_history: tuple[SimilarHistoryObservationV1, ...],
        degradations: tuple[InvestigationDegradationV1, ...],
        brief: EvidenceBriefV1,
        model_channel_active: bool,
        now: datetime,
    ) -> InvestigationView:
        with self._sessions.begin() as session:
            row = session.get(InvestigationRecord, investigation_id)
            if row is None:
                raise LookupError("INVESTIGATION_NOT_FOUND")
            if row.status != InvestigationStatus.PREPARING.value:
                return self._view(session, row)
            snapshot_payload = asdict(snapshot)
            session.add(InvestigationSnapshotRecord(
                investigation_id=investigation_id,
                schema_revision=snapshot.schema_revision,
                snapshot_json=_json(snapshot_payload),
                content_hash=snapshot_hash(snapshot_payload),
                created_at=_stored(now),
            ))
            for metric_observation in observations:
                session.add(MetricObservationRecord(
                    evidence_ref=metric_observation.evidence_ref,
                    investigation_id=investigation_id,
                    alert_ref=metric_observation.alert_ref,
                    metric_name=metric_observation.metric_name,
                    l1_summary_json=_json(metric_observation.l1_summary),
                    l2_sample_json=_json(metric_observation.l2_sample),
                    l3_series_json=_json(metric_observation.l3_series),
                    created_at=_stored(now),
                ))
            for empty_observation in empty_observations:
                session.add(MetricEmptyObservationRecord(
                    evidence_ref=empty_observation.evidence_ref,
                    investigation_id=investigation_id,
                    alert_ref=empty_observation.alert_ref,
                    metric_name=empty_observation.metric_name,
                    code=empty_observation.code,
                    message=empty_observation.message,
                    created_at=_stored(now),
                ))
            for history_observation in similar_history:
                session.add(
                    SimilarHistoryObservationRecord(
                        investigation_id=investigation_id,
                        rank=history_observation.rank,
                        occurrence_id=history_observation.occurrence_id,
                        score=history_observation.score,
                        match_reasons_json=_json(history_observation.match_reasons),
                        resolution_code=history_observation.resolution_code,
                        operator_conclusion=history_observation.operator_conclusion,
                        task_outcome=history_observation.task_outcome,
                        handling_duration_seconds=history_observation.handling_duration_seconds,
                        resolved_at=_stored(history_observation.resolved_at),
                        created_at=_stored(now),
                    )
                )
            for degradation in degradations:
                session.add(InvestigationDegradationRecord(
                    investigation_id=investigation_id,
                    domain=degradation.domain,
                    code=degradation.code,
                    message=degradation.message,
                    created_at=_stored(now),
                ))
            session.add(EvidenceBriefRecord(
                investigation_id=investigation_id,
                member_total=brief.member_total,
                member_detailed=brief.member_detailed,
                query_planned=brief.query_planned,
                query_completed=brief.query_completed,
                successful_metric_facts=brief.successful_metric_facts,
                empty_metric_facts=brief.empty_metric_facts,
                degraded_domains_json=_json(brief.degraded_domains),
                findings_json=_json(brief.findings),
                created_at=_stored(now),
            ))
            canceled = row.cancel_requested_at is not None
            should_queue = (
                not canceled
                and model_channel_active
                and brief.successful_metric_facts > 0
            )
            next_phase = (
                InvestigationPhase.EVIDENCE_EXPANSION_QUEUED
                if should_queue
                else (
                    InvestigationPhase.CANCELED
                    if canceled
                    else InvestigationPhase.TERMINAL_EVIDENCE_ONLY
                )
            )
            job_id: str | None = None
            if should_queue:
                job = self._jobs.enqueue(
                    session,
                    JobSpec(
                        kind="investigation.expand-evidence",
                        pool=JobPool.AI,
                        subject_type="investigation",
                        subject_id=investigation_id,
                        payload={"investigation_id": investigation_id},
                        payload_revision=1,
                        idempotency_key=f"expand:{investigation_id}",
                    ),
                    now=now,
                )
                job_id = job.id
            row.status = (
                InvestigationStatus.QUEUED.value
                if should_queue
                else InvestigationStatus.EVIDENCE_ONLY.value
            )
            row.phase = next_phase.value
            row.query_planned = brief.query_planned
            row.query_completed = brief.query_completed
            row.successful_metric_facts = brief.successful_metric_facts
            row.empty_metric_facts = brief.empty_metric_facts
            row.metric_queries_total = brief.query_planned
            row.degraded_domains_json = _json(brief.degraded_domains)
            row.findings_json = _json(brief.findings)
            row.job_id = job_id
            row.model_cost_status = "UNKNOWN" if should_queue else "NOT_INCURRED"
            if canceled:
                cancel_reason = row.termination_reason or "OPERATOR_REQUESTED"
                row.termination_reason = cancel_reason
                session.add(InvestigationCanceledRecord(
                    investigation_id=investigation_id,
                    reason=cancel_reason,
                    canceled_before="EVIDENCE_EXPANSION_QUEUE",
                    created_at=_stored(now),
                ))
            row.claim_owner = None
            row.claim_expires_at = None
            row.updated_at = _stored(now)
            session.add(InvestigationPhaseTransitionRecord(
                investigation_id=investigation_id,
                sequence=2,
                from_phase=InvestigationPhase.CLAIMED.value,
                to_phase=next_phase.value,
                created_at=_stored(now),
            ))
            head = session.get(InvestigationTrajectoryHeadRecord, investigation_id)
            if head is None:
                raise RuntimeError("INVESTIGATION_TRAJECTORY_HEAD_MISSING")
            head.latest_sequence = 2
            head.updated_at = _stored(now)
            self._jobs.append_event(
                session,
                event_type="investigation.p0-ready",
                subject_type="investigation",
                subject_id=investigation_id,
                now=now,
            )
            session.flush()
            return self._view(session, row)

    def get(self, investigation_id: str) -> InvestigationView:
        with self._sessions() as session:
            row = session.get(InvestigationRecord, investigation_id)
            if row is None:
                raise LookupError("INVESTIGATION_NOT_FOUND")
            return self._view(session, row)

    def list_for_occurrence(self, occurrence_id: int) -> tuple[InvestigationView, ...]:
        with self._sessions() as session:
            rows = session.scalars(
                select(InvestigationRecord)
                .where(InvestigationRecord.occurrence_id == occurrence_id)
                .order_by(InvestigationRecord.created_at.desc(), InvestigationRecord.id.desc())
            )
            return tuple(self._view(session, row) for row in rows)

    @staticmethod
    def _descriptor(row: PlannerStepRecord) -> MetricDescriptorV1 | None:
        if row.metric_name is None or row.descriptor_type is None:
            return None
        raw_values = json.loads(row.descriptor_known_values_json)
        if not isinstance(raw_values, dict):
            raise RuntimeError("PLANNER_DESCRIPTOR_INVALID")
        return MetricDescriptorV1(
            row.metric_name,
            row.descriptor_type,
            row.descriptor_help or "",
            row.descriptor_unit or "",
            tuple(str(item) for item in json.loads(row.descriptor_label_names_json)),
            {
                str(key): tuple(str(item) for item in value)
                for key, value in raw_values.items()
                if isinstance(value, list)
            },
        )

    def load_expansion_context(self, investigation_id: str) -> EvidenceExpansionContext:
        with self._sessions() as session:
            row = session.get(InvestigationRecord, investigation_id)
            snapshot = session.get(InvestigationSnapshotRecord, investigation_id)
            if row is None or snapshot is None:
                raise LookupError("INVESTIGATION_NOT_FOUND")
            if row.status != InvestigationStatus.QUEUED.value:
                raise RuntimeError("INVESTIGATION_NOT_QUEUED")
            raw_snapshot = json.loads(snapshot.snapshot_json)
            raw_context = raw_snapshot.get("context")
            raw_alerts = raw_snapshot.get("alerts")
            if not isinstance(raw_context, dict) or not isinstance(raw_alerts, dict):
                raise RuntimeError("INVESTIGATION_SNAPSHOT_INVALID")
            raw_scope = raw_context.get("scope")
            raw_playbook = raw_context.get("playbook")
            raw_model = raw_context.get("model_channel")
            raw_profile = raw_context.get("prompt_profile")
            if not all(isinstance(item, dict) for item in (raw_scope, raw_playbook, raw_model)):
                raise RuntimeError("INVESTIGATION_EXPANSION_CONTEXT_MISSING")
            scope_payload = cast(dict[str, Any], raw_scope)
            playbook_payload = cast(dict[str, Any], raw_playbook)
            model_payload = cast(dict[str, Any], raw_model)
            profile_guidance = (
                raw_profile.get("guidance", {}) if isinstance(raw_profile, dict) else {}
            )

            detailed_by_ref = {
                str(item.get("alert_ref")): item
                for item in raw_alerts.get("detailed") or ()
                if isinstance(item, dict)
            }
            alerts: list[PlannerAlertV1] = []
            for item in json.loads(row.member_scope_json):
                if not isinstance(item, dict):
                    continue
                alert_ref = str(item.get("alert_ref") or "")
                detailed = detailed_by_ref.get(alert_ref, {})
                labels = detailed.get("labels") if isinstance(detailed, dict) else {}
                alerts.append(PlannerAlertV1(
                    alert_ref,
                    str(item.get("alertname") or ""),
                    str(item.get("severity") or ""),
                    str(item.get("source_state") or ""),
                    {
                        str(key): str(value)
                        for key, value in (labels.items() if isinstance(labels, dict) else ())
                    },
                ))

            metric_facts = tuple(
                PlannerMetricFactV1(
                    item.evidence_ref,
                    item.alert_ref,
                    item.metric_name,
                    json.loads(item.l1_summary_json),
                )
                for item in session.scalars(
                    select(MetricObservationRecord)
                    .where(MetricObservationRecord.investigation_id == investigation_id)
                    .order_by(MetricObservationRecord.evidence_ref)
                )
            )
            empty_facts = tuple(
                PlannerEmptyFactV1(
                    item.evidence_ref, item.alert_ref, item.metric_name, item.code
                )
                for item in session.scalars(
                    select(MetricEmptyObservationRecord)
                    .where(MetricEmptyObservationRecord.investigation_id == investigation_id)
                    .order_by(MetricEmptyObservationRecord.evidence_ref)
                )
            )
            step_rows = tuple(session.scalars(
                select(PlannerStepRecord)
                .where(PlannerStepRecord.investigation_id == investigation_id)
                .order_by(PlannerStepRecord.sequence)
            ))
            descriptors = tuple(
                descriptor
                for descriptor in (self._descriptor(item) for item in step_rows)
                if descriptor is not None
            )
            steps = tuple(
                PlannerStepSummaryV1(
                    item.sequence,
                    item.action,
                    item.outcome,
                    item.metric_name,
                    item.safe_code,
                    tuple(str(value) for value in json.loads(item.result_metric_names_json)),
                )
                for item in step_rows
            )
            return EvidenceExpansionContext(
                investigation_id,
                str(scope_payload["source_id"]),
                int(scope_payload["occurrence_id"]),
                str(scope_payload["catalog_revision"]),
                int(playbook_payload["revision"]),
                str(model_payload["id"]),
                int(model_payload["revision_no"]),
                str(model_payload["kind"]),
                str(model_payload["model"]),
                tuple(str(item) for item in raw_context.get("metric_catalog") or ()),
                tuple(alerts),
                metric_facts,
                empty_facts,
                descriptors,
                steps,
                tuple(
                    item.compiled_fingerprint
                    for item in step_rows
                    if item.compiled_fingerprint is not None
                ),
                InvestigationBudgetV1(
                    planner_rounds=row.planner_rounds,
                    metric_queries=row.metric_queries_total,
                    accounted_tokens=row.accounted_tokens,
                    started_at=_aware(row.created_at),
                ),
                None if row.cancel_requested_at is None else _aware(row.cancel_requested_at),
                {
                    str(key): str(value)
                    for key, value in (
                        profile_guidance.items() if isinstance(profile_guidance, dict) else ()
                    )
                },
            )

    def record_label_rejections(
        self,
        investigation_id: str,
        rejections: tuple[LabelRejectionV1, ...],
        *,
        now: datetime,
    ) -> None:
        if not rejections:
            return
        with self._sessions.begin() as session:
            if session.scalar(select(PlannerLabelRejectionRecord.id).where(
                PlannerLabelRejectionRecord.investigation_id == investigation_id
            ).limit(1)) is not None:
                return
            for item in rejections:
                session.add(PlannerLabelRejectionRecord(
                    investigation_id=investigation_id,
                    alert_ref=item.alert_ref,
                    label_name=item.label_name,
                    code=item.code,
                    created_at=_stored(now),
                ))

    def record_planner_step(
        self,
        investigation_id: str,
        step: PlannerStepV1,
        *,
        budget: InvestigationBudgetV1,
        observation: MetricObservationV1 | None = None,
        empty_observation: MetricEmptyObservationV1 | None = None,
        degradation: InvestigationDegradationV1 | None = None,
        now: datetime,
    ) -> None:
        with self._sessions.begin() as session:
            row = session.get(InvestigationRecord, investigation_id)
            if row is None or row.status != InvestigationStatus.QUEUED.value:
                return
            descriptor = step.descriptor
            session.add(PlannerStepRecord(
                investigation_id=investigation_id,
                sequence=step.sequence,
                action=step.action,
                outcome=step.outcome,
                alert_ref=step.alert_ref,
                metric_name=step.metric_name,
                window=step.window,
                aggregation=step.aggregation,
                label_filters_json=_json([asdict(item) for item in step.label_filters]),
                group_by_json=_json(step.group_by),
                compiled_fingerprint=step.compiled_fingerprint,
                safe_code=step.safe_code,
                prompt_tokens=step.prompt_tokens,
                completion_tokens=step.completion_tokens,
                result_metric_names_json=_json(step.result_metric_names),
                descriptor_type=None if descriptor is None else descriptor.metric_type,
                descriptor_help=None if descriptor is None else descriptor.help,
                descriptor_unit=None if descriptor is None else descriptor.unit,
                descriptor_label_names_json=_json(() if descriptor is None else descriptor.label_names),
                descriptor_known_values_json=_json({} if descriptor is None else descriptor.known_label_values),
                created_at=_stored(now),
            ))
            if observation is not None:
                session.add(MetricObservationRecord(
                    evidence_ref=observation.evidence_ref,
                    investigation_id=investigation_id,
                    alert_ref=observation.alert_ref,
                    metric_name=observation.metric_name,
                    l1_summary_json=_json(observation.l1_summary),
                    l2_sample_json=_json(observation.l2_sample),
                    l3_series_json=_json(observation.l3_series),
                    created_at=_stored(now),
                ))
                row.successful_metric_facts += 1
            if empty_observation is not None:
                session.add(MetricEmptyObservationRecord(
                    evidence_ref=empty_observation.evidence_ref,
                    investigation_id=investigation_id,
                    alert_ref=empty_observation.alert_ref,
                    metric_name=empty_observation.metric_name,
                    code=empty_observation.code,
                    message=empty_observation.message,
                    created_at=_stored(now),
                ))
                row.empty_metric_facts += 1
            if degradation is not None:
                session.add(InvestigationDegradationRecord(
                    investigation_id=investigation_id,
                    domain=degradation.domain,
                    code=degradation.code,
                    message=degradation.message,
                    created_at=_stored(now),
                ))
                domains = set(json.loads(row.degraded_domains_json))
                domains.add(degradation.domain)
                row.degraded_domains_json = _json(tuple(sorted(domains)))
            row.planner_rounds = budget.planner_rounds
            row.accounted_tokens = budget.accounted_tokens
            row.metric_queries_total = budget.metric_queries
            row.model_cost_status = "UNKNOWN"
            row.updated_at = _stored(now)
            self._jobs.append_event(
                session,
                event_type="investigation.p1-step",
                subject_type="investigation",
                subject_id=investigation_id,
                now=now,
            )

    def is_cancel_requested(self, investigation_id: str) -> bool:
        with self._sessions() as session:
            value = session.scalar(select(InvestigationRecord.cancel_requested_at).where(
                InvestigationRecord.id == investigation_id
            ))
            return value is not None

    def load_analyst_context(self, investigation_id: str) -> AnalystExecutionContext:
        """Project the frozen Analyst input without selecting the L3 column."""

        with self._sessions() as session:
            row = session.get(InvestigationRecord, investigation_id)
            snapshot = session.get(InvestigationSnapshotRecord, investigation_id)
            if row is None or snapshot is None:
                raise LookupError("INVESTIGATION_NOT_FOUND")
            if row.status != InvestigationStatus.QUEUED.value:
                raise RuntimeError("INVESTIGATION_NOT_QUEUED")
            raw_snapshot = json.loads(snapshot.snapshot_json)
            raw_context = raw_snapshot.get("context")
            raw_alerts = raw_snapshot.get("alerts")
            raw_history = raw_snapshot.get("history")
            if not isinstance(raw_context, dict) or not isinstance(raw_alerts, dict):
                raise RuntimeError("INVESTIGATION_SNAPSHOT_INVALID")
            raw_playbook = raw_context.get("playbook")
            raw_model = raw_context.get("model_channel")
            if not isinstance(raw_playbook, dict) or not isinstance(raw_model, dict):
                raise RuntimeError("INVESTIGATION_ANALYST_CONTEXT_MISSING")

            alerts = tuple(
                AnalystAlertV1(
                    alert_ref=str(item.get("alert_ref") or ""),
                    alertname=str(item.get("alertname") or ""),
                    severity=str(item.get("severity") or ""),
                    source_state=str(item.get("source_state") or ""),
                    labels={
                        str(key): str(value)
                        for key, value in (
                            item.get("labels", {}).items()
                            if isinstance(item.get("labels"), dict)
                            else ()
                        )
                    },
                    annotations={
                        str(key): str(value)
                        for key, value in (
                            item.get("annotations", {}).items()
                            if isinstance(item.get("annotations"), dict)
                            else ()
                        )
                    },
                    starts_at=(
                        None if item.get("starts_at") is None else str(item.get("starts_at"))
                    ),
                    last_seen_at=str(item.get("last_seen_at") or ""),
                )
                for item in (raw_alerts.get("detailed") or ())
                if isinstance(item, dict)
            )
            alert_summaries = tuple(
                AnalystAlertSummaryV1(str(item.get("group") or ""), int(item.get("count") or 0))
                for item in (raw_alerts.get("summaries") or ())
                if isinstance(item, dict)
            )
            metric_rows = session.execute(
                select(
                    MetricObservationRecord.evidence_ref,
                    MetricObservationRecord.alert_ref,
                    MetricObservationRecord.metric_name,
                    MetricObservationRecord.l1_summary_json,
                    MetricObservationRecord.l2_sample_json,
                )
                .where(MetricObservationRecord.investigation_id == investigation_id)
                .order_by(MetricObservationRecord.evidence_ref)
            )
            metric_evidence = tuple(
                AnalystMetricEvidenceV1(
                    evidence_ref=item.evidence_ref,
                    alert_ref=item.alert_ref,
                    metric_name=item.metric_name,
                    l1_summary=json.loads(item.l1_summary_json),
                    l2_sample=tuple(
                        (float(point[0]), str(point[1]))
                        for point in json.loads(item.l2_sample_json)
                        if isinstance(point, list) and len(point) >= 2
                    ),
                )
                for item in metric_rows
            )
            empty_evidence = tuple(
                AnalystEmptyEvidenceV1(
                    item.evidence_ref, item.alert_ref, item.metric_name, item.code
                )
                for item in session.scalars(
                    select(MetricEmptyObservationRecord)
                    .where(MetricEmptyObservationRecord.investigation_id == investigation_id)
                    .order_by(MetricEmptyObservationRecord.evidence_ref)
                )
            )
            history_matches = (
                raw_history.get("matches", ()) if isinstance(raw_history, dict) else ()
            )
            similar_history = tuple(
                AnalystHistoryV1(
                    resolution=str(item.get("resolution") or ""),
                    operator_outcome=str(item.get("operator_outcome") or ""),
                    task_outcome=str(item.get("task_outcome") or ""),
                    duration_seconds=(
                        int(item["duration_seconds"])
                        if isinstance(item.get("duration_seconds"), int)
                        else None
                    ),
                    match_reason=str(item.get("match_reason") or ""),
                )
                for item in history_matches
                if isinstance(item, dict)
            )[:10]
            notes = tuple(
                AnalystNoteV1(str(item))
                for item in (raw_context.get("notes") or ())
                if isinstance(item, str) and item
            )[:10]
            prompt_profile = raw_context.get("prompt_profile")
            prompt_profile_revision = (
                int(prompt_profile.get("revision") or 1)
                if isinstance(prompt_profile, dict)
                else 1
            )
            return AnalystExecutionContext(
                investigation_id=investigation_id,
                snapshot_revision=int(raw_snapshot.get("schema_revision") or 1),
                prompt_profile_revision=prompt_profile_revision,
                playbook_revision=int(raw_playbook.get("revision") or 1),
                model_channel_id=str(raw_model.get("id") or ""),
                model_channel_revision=int(raw_model.get("revision_no") or 0),
                model_kind=str(raw_model.get("kind") or ""),
                model_name=str(raw_model.get("model") or ""),
                alerts=alerts,
                alert_summaries=alert_summaries,
                metric_evidence=metric_evidence,
                empty_evidence=empty_evidence,
                similar_history=similar_history,
                notes=notes,
                degraded_domains=tuple(str(item) for item in json.loads(row.degraded_domains_json)),
                budget=InvestigationBudgetV1(
                    planner_rounds=row.planner_rounds,
                    metric_queries=row.metric_queries_total,
                    accounted_tokens=row.accounted_tokens,
                    started_at=_aware(row.created_at),
                ),
                prompt_profile_guidance={
                    str(key): str(value)
                    for key, value in (
                        prompt_profile.get("guidance", {}).items()
                        if isinstance(prompt_profile, dict)
                        and isinstance(prompt_profile.get("guidance"), dict)
                        else ()
                    )
                },
            )

    def record_analyst_result(
        self,
        investigation_id: str,
        *,
        result: AnalystResultV1,
        prompt_profile_revision: int,
        budget: InvestigationBudgetV1,
        analyst_calls: int,
        prompt_tokens: int,
        completion_tokens: int,
        now: datetime,
    ) -> None:
        with self._sessions.begin() as session:
            row = session.get(InvestigationRecord, investigation_id)
            if row is None or row.status != InvestigationStatus.QUEUED.value:
                return
            if session.get(AnalystResultRecord, investigation_id) is not None:
                return
            session.add(AnalystResultRecord(
                investigation_id=investigation_id,
                contract_revision=1,
                prompt_profile_revision=prompt_profile_revision,
                result_json=result.model_dump_json(),
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                created_at=_stored(now),
            ))
            previous_phase = row.phase
            row.status = InvestigationStatus.EVIDENCE_ONLY.value
            row.phase = InvestigationPhase.ANALYST_RESULT_READY.value
            row.termination_reason = "P2_VALID"
            row.accounted_tokens = budget.accounted_tokens
            row.analyst_calls = analyst_calls
            row.analyst_prompt_tokens = prompt_tokens
            row.analyst_completion_tokens = completion_tokens
            findings = list(json.loads(row.findings_json))
            findings.append("AI 调查结论已生成；建议仍需由人工判断并执行")
            row.findings_json = _json(tuple(findings))
            row.updated_at = _stored(now)
            head = session.get(InvestigationTrajectoryHeadRecord, investigation_id)
            sequence = 3 if head is None else head.latest_sequence + 1
            session.add(InvestigationPhaseTransitionRecord(
                investigation_id=investigation_id,
                sequence=sequence,
                from_phase=previous_phase,
                to_phase=InvestigationPhase.ANALYST_RESULT_READY.value,
                created_at=_stored(now),
            ))
            if head is not None:
                head.latest_sequence = sequence
                head.updated_at = _stored(now)
            self._jobs.append_event(
                session,
                event_type="investigation.p2-ready",
                subject_type="investigation",
                subject_id=investigation_id,
                now=now,
            )

    def finish_analyst_failure(
        self,
        investigation_id: str,
        *,
        reason: str,
        invalid_raw: str | None,
        budget: InvestigationBudgetV1,
        analyst_calls: int,
        prompt_tokens: int,
        completion_tokens: int,
        now: datetime,
    ) -> None:
        with self._sessions.begin() as session:
            row = session.get(InvestigationRecord, investigation_id)
            if row is None or row.status != InvestigationStatus.QUEUED.value:
                return
            if invalid_raw is not None:
                if self._encrypt_invalid_raw is None:
                    raise RuntimeError("INVALID_ANALYST_RESPONSE_ENCRYPTION_UNAVAILABLE")
                session.add(InvalidAnalystResponseRecord(
                    investigation_id=investigation_id,
                    envelope_json=self._encrypt_invalid_raw(invalid_raw),
                    purge_after=_stored(now + timedelta(days=7)),
                    created_at=_stored(now),
                ))
            previous_phase = row.phase
            row.status = InvestigationStatus.EVIDENCE_ONLY.value
            row.phase = InvestigationPhase.EVIDENCE_EXPANDED.value
            row.termination_reason = reason[:96]
            row.accounted_tokens = budget.accounted_tokens
            row.analyst_calls = analyst_calls
            row.analyst_prompt_tokens = prompt_tokens
            row.analyst_completion_tokens = completion_tokens
            row.model_cost_status = "UNKNOWN" if analyst_calls else row.model_cost_status
            findings = list(json.loads(row.findings_json))
            findings.append("AI 扩展调查已保留；模型未产出可验证的结构化结论")
            row.findings_json = _json(tuple(findings))
            session.add(InvestigationDegradationRecord(
                investigation_id=investigation_id,
                domain="model",
                code=reason[:96],
                message="模型未返回可验证的结构化结论；基础证据与扩展事实仍可使用",
                created_at=_stored(now),
            ))
            domains = set(json.loads(row.degraded_domains_json))
            domains.add("model")
            row.degraded_domains_json = _json(tuple(sorted(domains)))
            row.updated_at = _stored(now)
            head = session.get(InvestigationTrajectoryHeadRecord, investigation_id)
            sequence = 3 if head is None else head.latest_sequence + 1
            session.add(InvestigationPhaseTransitionRecord(
                investigation_id=investigation_id,
                sequence=sequence,
                from_phase=previous_phase,
                to_phase=InvestigationPhase.EVIDENCE_EXPANDED.value,
                created_at=_stored(now),
            ))
            if head is not None:
                head.latest_sequence = sequence
                head.updated_at = _stored(now)
            self._jobs.append_event(
                session,
                event_type="investigation.analyst-unavailable",
                subject_type="investigation",
                subject_id=investigation_id,
                now=now,
            )

    def finish_expansion(
        self, investigation_id: str, *, reason: str, now: datetime
    ) -> None:
        with self._sessions.begin() as session:
            row = session.get(InvestigationRecord, investigation_id)
            if row is None or row.status != InvestigationStatus.QUEUED.value:
                return
            step_count = int(session.scalar(select(func.count(PlannerStepRecord.id)).where(
                PlannerStepRecord.investigation_id == investigation_id
            )) or 0)
            next_phase = (
                InvestigationPhase.EVIDENCE_EXPANDED
                if step_count > 0
                else InvestigationPhase.TERMINAL_EVIDENCE_ONLY
            )
            row.status = InvestigationStatus.EVIDENCE_ONLY.value
            row.phase = next_phase.value
            row.termination_reason = reason[:96]
            findings = list(json.loads(row.findings_json))
            findings.append(
                "AI 扩展调查已记录；当前尚未生成调查结论"
                if step_count > 0
                else "基础证据已保留；AI 扩展调查未启动"
            )
            row.findings_json = _json(tuple(findings))
            if row.planner_rounds == 0:
                row.model_cost_status = "NOT_INCURRED"
            degradation: tuple[str, str, str] | None = None
            if reason == "MODEL_CHANNEL_UNAVAILABLE":
                degradation = (
                    "model", reason,
                    "冻结的模型服务已停用或版本不可用，未发起模型调用",
                )
            elif reason == "METRIC_SOURCE_UNAVAILABLE":
                degradation = (
                    "metrics", "SOURCE_UNAVAILABLE",
                    "指标源已不可用，本次保留已取得证据且不自动重试",
                )
            elif reason == "MODEL_RATE_LIMITED":
                degradation = (
                    "model", reason,
                    "模型服务限流或用量受限；已有证据已保留，平台未自动重试",
                )
            elif reason in _BUDGET_TERMINATION_REASONS:
                degradation = (
                    "budget", reason,
                    "调查已到达代码预算边界，未继续发起外部调用",
                )
            elif reason.startswith("MODEL_"):
                degradation = (
                    "model", reason,
                    "模型调用未返回可用结果；已有证据已保留且不会自动重试",
                )
            elif reason.startswith("PLANNER_"):
                degradation = (
                    "model", reason,
                    "规划模型未返回可采用的只读决策；已有证据已保留且不会自动重试",
                )
            if degradation is not None:
                domain, code, message = degradation
                session.add(InvestigationDegradationRecord(
                    investigation_id=investigation_id,
                    domain=domain,
                    code=code,
                    message=message,
                    created_at=_stored(now),
                ))
                domains = set(json.loads(row.degraded_domains_json))
                domains.add(domain)
                row.degraded_domains_json = _json(tuple(sorted(domains)))
            row.updated_at = _stored(now)
            head = session.get(InvestigationTrajectoryHeadRecord, investigation_id)
            sequence = 3 if head is None else head.latest_sequence + 1
            session.add(InvestigationPhaseTransitionRecord(
                investigation_id=investigation_id,
                sequence=sequence,
                from_phase=InvestigationPhase.EVIDENCE_EXPANSION_QUEUED.value,
                to_phase=next_phase.value,
                created_at=_stored(now),
            ))
            if head is not None:
                head.latest_sequence = sequence
                head.updated_at = _stored(now)
            self._jobs.append_event(
                session,
                event_type="investigation.p1-ready",
                subject_type="investigation",
                subject_id=investigation_id,
                now=now,
            )

    def finish_canceled(
        self,
        investigation_id: str,
        *,
        canceled_before: str,
        budget: InvestigationBudgetV1,
        now: datetime,
    ) -> None:
        with self._sessions.begin() as session:
            row = session.get(InvestigationRecord, investigation_id)
            if row is None or row.status not in {
                InvestigationStatus.PREPARING.value,
                InvestigationStatus.QUEUED.value,
            }:
                return
            cancel_reason = row.termination_reason or "OPERATOR_REQUESTED"
            if session.get(InvestigationCanceledRecord, investigation_id) is None:
                session.add(InvestigationCanceledRecord(
                    investigation_id=investigation_id,
                    reason=cancel_reason,
                    canceled_before=canceled_before[:64],
                    created_at=_stored(now),
                ))
            previous_phase = row.phase
            row.status = InvestigationStatus.EVIDENCE_ONLY.value
            row.phase = InvestigationPhase.CANCELED.value
            row.termination_reason = cancel_reason
            row.planner_rounds = budget.planner_rounds
            row.accounted_tokens = budget.accounted_tokens
            row.metric_queries_total = budget.metric_queries
            row.claim_owner = None
            row.claim_expires_at = None
            row.updated_at = _stored(now)
            head = session.get(InvestigationTrajectoryHeadRecord, investigation_id)
            sequence = 2 if head is None else head.latest_sequence + 1
            session.add(InvestigationPhaseTransitionRecord(
                investigation_id=investigation_id,
                sequence=sequence,
                from_phase=previous_phase,
                to_phase=InvestigationPhase.CANCELED.value,
                created_at=_stored(now),
            ))
            if head is not None:
                head.latest_sequence = sequence
                head.updated_at = _stored(now)

    def request_cancel_in_session(
        self, session: Session, occurrence_id: int, now: datetime
    ) -> None:
        for row in session.scalars(select(InvestigationRecord).where(
            InvestigationRecord.occurrence_id == occurrence_id,
            InvestigationRecord.status.in_((
                InvestigationStatus.PREPARING.value,
                InvestigationStatus.QUEUED.value,
            )),
        )):
            if row.cancel_requested_at is None:
                row.cancel_requested_at = _stored(now)
                row.termination_reason = "OCCURRENCE_RESOLVED"
                row.updated_at = _stored(now)

    def request_cancel(
        self,
        investigation_id: str,
        *,
        actor: InteractiveOperatorActor,
        now: datetime,
    ) -> InvestigationView:
        require_interactive_operator(actor)
        with self._sessions.begin() as session:
            row = session.get(InvestigationRecord, investigation_id)
            if row is None:
                raise LookupError("INVESTIGATION_NOT_FOUND")
            if row.status not in {
                InvestigationStatus.PREPARING.value,
                InvestigationStatus.QUEUED.value,
            }:
                return self._view(session, row)
            if row.cancel_requested_at is None:
                row.cancel_requested_at = _stored(now)
                row.termination_reason = "OPERATOR_REQUESTED"
                row.updated_at = _stored(now)
                self._jobs.append_event(
                    session,
                    event_type="investigation.cancel-requested",
                    subject_type="investigation",
                    subject_id=investigation_id,
                    now=now,
                )
                session.flush()
            return self._view(session, row)

    @staticmethod
    def _rollup_range_in_session(
        session: Session,
        *,
        from_day: date,
        to_day: date,
        now: datetime,
        preserve_existing_first_day: bool,
    ) -> None:
        if to_day < from_day:
            return
        protected_first_day = (
            preserve_existing_first_day
            and session.get(InvestigationDailyUsageRecord, from_day.isoformat()) is not None
        )
        from_timestamp = datetime.combine(from_day, time.min)
        to_timestamp = datetime.combine(to_day + timedelta(days=1), time.min)
        rows = session.execute(
            text(
                """
                WITH first_query AS (
                  SELECT investigation_id, MIN(created_at) AS p1_at
                  FROM planner_step_v1
                  WHERE action='QUERY_METRIC' AND outcome='COMPLETED'
                  GROUP BY investigation_id
                ), degradation AS (
                  SELECT investigation_id,
                         COUNT(*) AS degradation_count,
                         MAX(CASE
                           WHEN code IN (
                             'SOURCE_UNAVAILABLE','ZERO_HOP_DEADLINE_REACHED',
                             'MODEL_CHANNEL_UNAVAILABLE','METRIC_SOURCE_UNAVAILABLE',
                             'CLAIM_LEASE_EXPIRED'
                           ) THEN 1 ELSE 0 END) AS dependency_failed
                  FROM investigation_degradation_v1
                  GROUP BY investigation_id
                ), latest_feedback_sequence AS (
                  SELECT investigation_id, MAX(sequence) AS sequence
                  FROM investigation_feedback_v1
                  GROUP BY investigation_id
                ), latest_feedback AS (
                  SELECT feedback.investigation_id, feedback.rating
                  FROM investigation_feedback_v1 AS feedback
                  JOIN latest_feedback_sequence AS latest
                    ON latest.investigation_id=feedback.investigation_id
                   AND latest.sequence=feedback.sequence
                ), facts AS (
                  SELECT investigation.*,
                         brief.created_at AS p0_at,
                         first_query.p1_at AS p1_at,
                         analyst.created_at AS p2_at,
                         CASE WHEN canceled.investigation_id IS NOT NULL
                                   OR investigation.phase='CANCELED'
                              THEN 1 ELSE 0 END AS is_canceled,
                         CASE WHEN analyst.investigation_id IS NOT NULL
                              THEN 1 ELSE 0 END AS is_p2_valid,
                         CASE WHEN investigation.termination_reason LIKE '%CONTRACT_REJECTED%'
                              THEN 1 ELSE 0 END AS is_contract_rejected,
                         CASE WHEN investigation.status='FAILED'
                                   OR COALESCE(degradation.dependency_failed,0)=1
                                   OR investigation.termination_reason IN (
                                     'MODEL_CHANNEL_UNAVAILABLE','METRIC_SOURCE_UNAVAILABLE'
                                   )
                              THEN 1 ELSE 0 END AS is_dependency_failed,
                         COALESCE(degradation.degradation_count,0) AS degradation_count,
                         latest_feedback.rating AS feedback_rating
                  FROM investigation
                  LEFT JOIN evidence_brief_v1 AS brief
                    ON brief.investigation_id=investigation.id
                  LEFT JOIN first_query
                    ON first_query.investigation_id=investigation.id
                  LEFT JOIN analyst_result_v1 AS analyst
                    ON analyst.investigation_id=investigation.id
                  LEFT JOIN investigation_canceled_v1 AS canceled
                    ON canceled.investigation_id=investigation.id
                  LEFT JOIN degradation
                    ON degradation.investigation_id=investigation.id
                  LEFT JOIN latest_feedback
                    ON latest_feedback.investigation_id=investigation.id
                  WHERE investigation.created_at>=:from_timestamp
                    AND investigation.created_at<:to_timestamp
                )
                SELECT
                  DATE(created_at) AS day_utc,
                  COUNT(*) AS run_count,
                  SUM(CASE WHEN status NOT IN ('PREPARING','QUEUED') THEN 1 ELSE 0 END)
                    AS terminal_run_count,
                  SUM(is_p2_valid) AS p2_valid_count,
                  SUM(CASE WHEN status NOT IN ('PREPARING','QUEUED')
                                AND is_p2_valid=0 AND is_canceled=0
                                AND is_contract_rejected=0 AND is_dependency_failed=0
                           THEN 1 ELSE 0 END) AS evidence_only_count,
                  SUM(is_canceled) AS canceled_count,
                  SUM(is_contract_rejected) AS contract_rejected_count,
                  SUM(CASE WHEN is_contract_rejected=0 THEN is_dependency_failed ELSE 0 END)
                    AS dependency_failed_count,
                  SUM(CASE WHEN planner_rounds>0 OR analyst_calls>0 THEN 1 ELSE 0 END)
                    AS model_started_run_count,
                  SUM(CASE WHEN (planner_rounds>0 OR analyst_calls>0)
                                AND model_execution_mode='FAKE' THEN 1 ELSE 0 END)
                    AS fake_model_started_run_count,
                  SUM(CASE WHEN (planner_rounds>0 OR analyst_calls>0)
                                AND model_execution_mode='EXTERNAL' THEN 1 ELSE 0 END)
                    AS external_model_started_run_count,
                  SUM(CASE WHEN (planner_rounds>0 OR analyst_calls>0)
                                AND model_execution_mode NOT IN ('FAKE','EXTERNAL') THEN 1 ELSE 0 END)
                    AS unknown_model_started_run_count,
                  SUM(planner_rounds) AS planner_calls,
                  SUM(analyst_calls) AS analyst_calls,
                  SUM(metric_queries_total) AS metric_queries,
                  SUM(accounted_tokens) AS accounted_tokens,
                  SUM(CASE WHEN (planner_rounds>0 OR analyst_calls>0)
                                AND model_cost_status='UNKNOWN' THEN 1 ELSE 0 END)
                    AS unknown_cost_run_count,
                  SUM(CASE WHEN feedback_rating IS NOT NULL THEN 1 ELSE 0 END)
                    AS feedback_response_count,
                  SUM(CASE WHEN feedback_rating='ADOPTED' THEN 1 ELSE 0 END)
                    AS feedback_adopted_count,
                  SUM(CASE WHEN degradation_count>0 THEN 1 ELSE 0 END)
                    AS degraded_run_count,
                  SUM(CASE WHEN p0_at IS NOT NULL THEN 1 ELSE 0 END) AS p0_mtti_count,
                  COALESCE(SUM(CASE WHEN p0_at IS NOT NULL THEN
                    CAST(MAX(0,ROUND((JULIANDAY(p0_at)-JULIANDAY(created_at))*86400000)) AS INTEGER)
                    ELSE 0 END),0) AS p0_mtti_sum_ms,
                  SUM(CASE WHEN p1_at IS NOT NULL THEN 1 ELSE 0 END) AS p1_mtti_count,
                  COALESCE(SUM(CASE WHEN p1_at IS NOT NULL THEN
                    CAST(MAX(0,ROUND((JULIANDAY(p1_at)-JULIANDAY(created_at))*86400000)) AS INTEGER)
                    ELSE 0 END),0) AS p1_mtti_sum_ms,
                  SUM(CASE WHEN p2_at IS NOT NULL THEN 1 ELSE 0 END) AS p2_mtti_count,
                  COALESCE(SUM(CASE WHEN p2_at IS NOT NULL THEN
                    CAST(MAX(0,ROUND((JULIANDAY(p2_at)-JULIANDAY(created_at))*86400000)) AS INTEGER)
                    ELSE 0 END),0) AS p2_mtti_sum_ms
                FROM facts
                GROUP BY DATE(created_at)
                ORDER BY DATE(created_at)
                """
            ),
            {
                "from_timestamp": _stored(from_timestamp),
                "to_timestamp": _stored(to_timestamp),
            },
        ).mappings()
        for value in rows:
            day_utc = str(value["day_utc"])
            if protected_first_day and day_utc == from_day.isoformat():
                continue

            def count(name: str) -> int:
                return int(value[name] or 0)

            session.merge(InvestigationDailyUsageRecord(
                day_utc=day_utc,
                schema_revision=1,
                run_count=count("run_count"),
                terminal_run_count=count("terminal_run_count"),
                p2_valid_count=count("p2_valid_count"),
                evidence_only_count=count("evidence_only_count"),
                canceled_count=count("canceled_count"),
                contract_rejected_count=count("contract_rejected_count"),
                dependency_failed_count=count("dependency_failed_count"),
                model_started_run_count=count("model_started_run_count"),
                fake_model_started_run_count=count("fake_model_started_run_count"),
                external_model_started_run_count=count("external_model_started_run_count"),
                unknown_model_started_run_count=count("unknown_model_started_run_count"),
                planner_calls=count("planner_calls"),
                analyst_calls=count("analyst_calls"),
                metric_queries=count("metric_queries"),
                accounted_tokens=count("accounted_tokens"),
                unknown_cost_run_count=count("unknown_cost_run_count"),
                feedback_response_count=count("feedback_response_count"),
                feedback_adopted_count=count("feedback_adopted_count"),
                degraded_run_count=count("degraded_run_count"),
                p0_mtti_count=count("p0_mtti_count"),
                p0_mtti_sum_ms=count("p0_mtti_sum_ms"),
                p1_mtti_count=count("p1_mtti_count"),
                p1_mtti_sum_ms=count("p1_mtti_sum_ms"),
                p2_mtti_count=count("p2_mtti_count"),
                p2_mtti_sum_ms=count("p2_mtti_sum_ms"),
                updated_at=_stored(now),
            ))

    @staticmethod
    def _purge_daily_usage_in_session(
        session: Session, *, now: datetime, retention_days: int
    ) -> None:
        cutoff_day = (now.astimezone(UTC) - timedelta(days=retention_days)).date()
        session.execute(
            delete(InvestigationDailyUsageRecord).where(
                InvestigationDailyUsageRecord.day_utc < cutoff_day.isoformat()
            )
        )

    def refresh_daily_usage(
        self,
        *,
        now: datetime,
        raw_retention_days: int = 90,
        retention_days: int = 365,
    ) -> tuple[InvestigationDailyUsageView, ...]:
        current = now.astimezone(UTC)
        from_day = (current - timedelta(days=raw_retention_days)).date()
        with self._sessions.begin() as session:
            self._rollup_range_in_session(
                session,
                from_day=from_day,
                to_day=current.date(),
                now=current,
                preserve_existing_first_day=True,
            )
            self._purge_daily_usage_in_session(
                session, now=current, retention_days=retention_days
            )
        return self.list_daily_usage(from_day=from_day, to_day=current.date())

    def finalize_retention_rollups(
        self,
        *,
        now: datetime,
        raw_retention_days: int = 90,
    ) -> None:
        """Freeze the UTC usage horizon before bounded raw-row retention.

        Daily usage deletion belongs to the shared retention coordinator; this
        method intentionally performs no cleanup of its own.
        """
        current = now.astimezone(UTC)
        with self._sessions.begin() as session:
            self._rollup_range_in_session(
                session,
                from_day=(current - timedelta(days=raw_retention_days)).date(),
                to_day=current.date(),
                now=current,
                preserve_existing_first_day=True,
            )

    def list_daily_usage(
        self, *, from_day: date, to_day: date
    ) -> tuple[InvestigationDailyUsageView, ...]:
        if to_day < from_day or (to_day - from_day).days > 365:
            raise ValueError("INVESTIGATION_USAGE_RANGE_INVALID")
        with self._sessions() as session:
            rows = session.scalars(
                select(InvestigationDailyUsageRecord)
                .where(
                    InvestigationDailyUsageRecord.day_utc >= from_day.isoformat(),
                    InvestigationDailyUsageRecord.day_utc <= to_day.isoformat(),
                )
                .order_by(InvestigationDailyUsageRecord.day_utc)
            )
            return tuple(self._daily_view(row) for row in rows)

    def cleanup(self, *, now: datetime, retention_days: int = 90) -> int:
        """Delete terminal runs only after the fixed retention horizon."""
        cutoff = _stored(now - timedelta(days=retention_days))
        with self._sessions.begin() as session:
            current = now.astimezone(UTC)
            self._rollup_range_in_session(
                session,
                from_day=(current - timedelta(days=retention_days)).date(),
                to_day=current.date(),
                now=current,
                preserve_existing_first_day=True,
            )
            self._purge_daily_usage_in_session(
                session, now=current, retention_days=365
            )
            expired_invalid = int(session.scalar(
                select(func.count(InvalidAnalystResponseRecord.investigation_id)).where(
                    InvalidAnalystResponseRecord.purge_after <= _stored(now)
                )
            ) or 0)
            identifiers = tuple(session.scalars(
                select(InvestigationRecord.id).where(
                    InvestigationRecord.created_at < cutoff,
                    InvestigationRecord.status.in_(("EVIDENCE_ONLY", "FAILED")),
                )
            ))
            if identifiers:
                bounds = session.execute(
                    select(
                        func.min(InvestigationRecord.created_at),
                        func.max(InvestigationRecord.created_at),
                    ).where(InvestigationRecord.id.in_(identifiers))
                ).one()
                if bounds[0] is not None and bounds[1] is not None:
                    self._rollup_range_in_session(
                        session,
                        from_day=_aware(bounds[0]).date(),
                        to_day=_aware(bounds[1]).date(),
                        now=current,
                        preserve_existing_first_day=False,
                    )
                    self._purge_daily_usage_in_session(
                        session, now=current, retention_days=365
                    )
            if not identifiers and expired_invalid == 0:
                return 0
            session.execute(text("UPDATE investigation_retention_gate SET enabled=1 WHERE id=1"))
            session.execute(delete(InvalidAnalystResponseRecord).where(
                InvalidAnalystResponseRecord.purge_after <= _stored(now)
            ))
            for model in (
                InvestigationFeedbackRecord,
                InvalidAnalystResponseRecord,
                AnalystResultRecord,
                InvestigationCanceledRecord,
                PlannerLabelRejectionRecord,
                PlannerStepRecord,
                InvestigationDegradationRecord,
                MetricEmptyObservationRecord,
                MetricObservationRecord,
                SimilarHistoryObservationRecord,
                EvidenceBriefRecord,
                InvestigationPhaseTransitionRecord,
                InvestigationSnapshotRecord,
                InvestigationTrajectoryHeadRecord,
            ):
                session.execute(delete(model).where(model.investigation_id.in_(identifiers)))
            session.execute(delete(InvestigationRecord).where(InvestigationRecord.id.in_(identifiers)))
            session.execute(text("UPDATE investigation_retention_gate SET enabled=0 WHERE id=1"))
            return len(identifiers)
