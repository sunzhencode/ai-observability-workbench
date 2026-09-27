"""Explicit-start, bounded P0 investigation orchestration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Protocol

from app.application.incidents import IncidentStore
from app.application.observability import ObservabilityPort, ThanosReadPort
from app.domains.incidents.actors import InteractiveOperatorActor, require_interactive_operator
from app.domains.investigations.analyst import AnalystResultV1
from app.domains.investigations.models import (
    AlertScopeRef,
    EvidenceBriefV1,
    InvestigationDegradationV1,
    InitialInvestigationSnapshotV1,
    InvestigationScopeResolutionV1,
    InvestigationStatus,
    MAX_DETAILED_ALERTS,
    MAX_ZERO_HOP_CONCURRENCY,
    MAX_ZERO_HOP_QUERIES,
    MAX_ZERO_HOP_WALL_CLOCK_SECONDS,
    MetricEmptyObservationV1,
    MetricObservationV1,
    SimilarHistoryObservationV1,
    evidence_ref,
    stable_alert_ref,
    select_playbook,
    summarize_series,
)
from app.domains.investigations.planner import (
    PlannerStepSummaryV1,
    freeze_metric_catalog,
    metric_catalog_revision,
)
from app.domains.metrics.models import (
    MetricReadStatus,
    QueryWindow,
    classify_read,
    expr_from_generator_url,
    render_metric_template,
    strip_numeric_comparison,
)

UTC = timezone.utc


@dataclass(frozen=True, slots=True)
class MetricObservationView:
    evidence_ref: str
    alert_ref: str
    metric_name: str
    l1_summary: dict[str, float | int | str | None]
    l2_sample: tuple[tuple[float, str], ...]


@dataclass(frozen=True, slots=True)
class AlertEvidenceView:
    evidence_ref: str
    alertname: str


@dataclass(frozen=True, slots=True)
class InvestigationToolActionView:
    sequence: int
    action: str
    outcome: str
    metric_name: str | None
    window: str | None
    aggregation: str | None
    label_names: tuple[str, ...]
    group_by: tuple[str, ...]
    safe_code: str | None


@dataclass(frozen=True, slots=True)
class InvestigationUsageView:
    initiator_kind: str
    request_id: str
    source_ip: str
    planner_calls: int
    analyst_calls: int
    metric_queries: int
    accounted_tokens: int
    model_execution_mode: str
    model_channel_id: str | None
    model_channel_revision: int | None
    prompt_profile_revision: int | None
    playbook_revision: int | None
    egress_categories: tuple[str, ...]
    pricing_revision: int | None
    cost_status: str
    p0_mtti_ms: int | None
    p1_mtti_ms: int | None
    p2_mtti_ms: int | None
    query_yield: float | None
    evidence_gain: int
    degradation_count: int
    canceled: bool
    tool_actions: tuple[InvestigationToolActionView, ...]


@dataclass(frozen=True, slots=True)
class InvestigationDailyUsageView:
    day_utc: date
    schema_revision: int
    run_count: int
    terminal_run_count: int
    p2_valid_count: int
    evidence_only_count: int
    canceled_count: int
    contract_rejected_count: int
    dependency_failed_count: int
    model_started_run_count: int
    fake_model_started_run_count: int
    external_model_started_run_count: int
    unknown_model_started_run_count: int
    planner_calls: int
    analyst_calls: int
    metric_queries: int
    accounted_tokens: int
    unknown_cost_run_count: int
    feedback_response_count: int
    feedback_adopted_count: int
    degraded_run_count: int
    p0_mtti_count: int
    p0_mtti_sum_ms: int
    p1_mtti_count: int
    p1_mtti_sum_ms: int
    p2_mtti_count: int
    p2_mtti_sum_ms: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class InvestigationFeedbackView:
    sequence: int
    rating: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class InvestigationView:
    id: str
    occurrence_id: int
    incident_id: int
    status: str
    phase: str
    request_key: str
    member_total: int
    member_detailed: int
    query_planned: int
    query_completed: int
    successful_metric_facts: int
    empty_metric_facts: int
    foundation_successful_metric_facts: int
    foundation_empty_metric_facts: int
    degraded_domains: tuple[str, ...]
    degradations: tuple[InvestigationDegradationV1, ...]
    findings: tuple[str, ...]
    job_id: str | None
    model_cost_status: str
    snapshot_occurrence_version: int | None
    planner_rounds: int
    accounted_tokens: int
    metric_queries_total: int
    termination_reason: str | None
    cancel_requested_at: datetime | None
    planner_steps: tuple[PlannerStepSummaryV1, ...]
    alert_evidence: tuple[AlertEvidenceView, ...]
    metric_observations: tuple[MetricObservationView, ...]
    analyst_result: AnalystResultV1 | None
    analyst_calls: int
    analyst_prompt_tokens: int
    analyst_completion_tokens: int
    prompt_profile_id: str | None
    prompt_profile_name: str | None
    prompt_profile_revision: int | None
    playbook_id: str | None
    playbook_revision: int | None
    usage: InvestigationUsageView
    feedback: InvestigationFeedbackView | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class InvestigationClaim:
    investigation: InvestigationView
    replayed: bool


class InvestigationStore(Protocol):
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
        model_execution_mode: str,
        now: datetime,
    ) -> InvestigationClaim: ...

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
    ) -> InvestigationView: ...

    def get(self, investigation_id: str) -> InvestigationView: ...
    def list_for_occurrence(self, occurrence_id: int) -> tuple[InvestigationView, ...]: ...
    def list_daily_usage(
        self, *, from_day: date, to_day: date
    ) -> tuple[InvestigationDailyUsageView, ...]: ...
    def request_cancel(
        self,
        investigation_id: str,
        *,
        actor: InteractiveOperatorActor,
        now: datetime,
    ) -> InvestigationView: ...

    def record_feedback(
        self,
        investigation_id: str,
        *,
        rating: str,
        actor: InteractiveOperatorActor,
        now: datetime,
    ) -> InvestigationFeedbackView: ...


@dataclass(frozen=True, slots=True)
class _MetricCandidate:
    alert_ref: str
    name: str
    expression: str


class StartInvestigation:
    def __init__(
        self,
        *,
        store: InvestigationStore,
        incidents: IncidentStore,
        observability: ObservabilityPort,
        thanos_factory: Callable[[str, str], ThanosReadPort],
        model_execution_mode: str = "EXTERNAL",
    ) -> None:
        if model_execution_mode not in {"FAKE", "EXTERNAL"}:
            raise ValueError("MODEL_EXECUTION_MODE_INVALID")
        self._store = store
        self._incidents = incidents
        self._observability = observability
        self._thanos_factory = thanos_factory
        self._model_execution_mode = model_execution_mode

    async def execute(
        self,
        occurrence_id: int,
        *,
        request_key: str,
        actor: InteractiveOperatorActor,
        request_id: str,
        source_ip: str,
        prompt_profile_id: str | None = None,
        now: datetime | None = None,
    ) -> InvestigationView:
        require_interactive_operator(actor)
        current = now or datetime.now(UTC)
        occurrence = self._incidents.get_operational_occurrence(occurrence_id, now=current)
        incident = self._incidents.get_incident(occurrence.incident_id)
        if incident.occurrence_no != occurrence.occurrence_no:
            raise RuntimeError("INVESTIGATION_OCCURRENCE_SCOPE_STALE")
        prompt_profile = self._observability.resolve_prompt_profile(
            profile_id=prompt_profile_id,
            service_id=occurrence.service_id,
        )
        severity_rank = {"critical": 0, "warning": 1, "info": 2}
        ordered_members = tuple(sorted(
            incident.members,
            key=lambda item: (
                0 if item.source_state == "FIRING" else 1,
                severity_rank.get(item.severity.lower(), 3),
                -item.last_seen_at.timestamp(),
                item.id,
            ),
        ))
        refs = tuple(
            AlertScopeRef(
                stable_alert_ref(item.source_id, item.upstream_fingerprint),
                item.id,
                item.alertname,
                item.severity,
                item.source_state,
            )
            for item in ordered_members
        )
        claim = self._store.claim(
            occurrence_id=occurrence_id,
            incident_id=occurrence.incident_id,
            request_key=request_key,
            member_scope=refs,
            occurrence_version=occurrence.version,
            actor=actor,
            request_id=request_id,
            source_ip=source_ip,
            model_execution_mode=self._model_execution_mode,
            now=current,
        )
        if claim.replayed:
            return claim.investigation

        detailed = refs[:MAX_DETAILED_ALERTS]
        members_by_id = {item.id: item for item in ordered_members}
        candidates: list[_MetricCandidate] = []
        degradations: list[InvestigationDegradationV1] = []
        for position, member in enumerate(detailed):
            context = self._observability.get_alert_metric_context(member.alert_id)
            expression = expr_from_generator_url(context.generator_url)
            if expression is None:
                continue
            candidates.append(
                _MetricCandidate(
                    member.alert_ref,
                    f"{context.alertname} 主曲线",
                    strip_numeric_comparison(expression) or expression,
                )
            )
            if len(candidates) == MAX_ZERO_HOP_QUERIES:
                break

        if len(candidates) < MAX_ZERO_HOP_QUERIES:
            templates = tuple(
                item
                for item in sorted(
                    self._observability.list_metric_templates(),
                    key=lambda item: (item.priority, item.id),
                )
                if item.enabled
                and (not item.source_ids or occurrence.source_id in item.source_ids)
            )
            contexts = {
                member.alert_ref: self._observability.get_alert_metric_context(member.alert_id)
                for member in detailed
            }
            for template in templates:
                for member in detailed:
                    rendered = render_metric_template(
                        template.promql,
                        contexts[member.alert_ref].labels,
                        required_labels=template.required_labels,
                    )
                    if rendered is None:
                        continue
                    candidates.append(
                        _MetricCandidate(member.alert_ref, template.name, rendered)
                    )
                    if len(candidates) == MAX_ZERO_HOP_QUERIES:
                        break
                if len(candidates) == MAX_ZERO_HOP_QUERIES:
                    break

        observations: list[MetricObservationV1] = []
        empty: list[MetricEmptyObservationV1] = []
        source_catalog_names: tuple[str, ...] = ()
        connection = None
        try:
            connection = self._observability.load_monitoring_secret(
                occurrence.source_id, "THANOS", require_active=True
            )
        except (LookupError, RuntimeError):
            degradations.append(
                InvestigationDegradationV1(
                    "metrics",
                    "SOURCE_UNAVAILABLE",
                    "指标源未配置或尚未启用，本次仍交付告警范围证据",
                )
            )

        completed = 0
        if connection is not None and candidates:
            reader = self._thanos_factory(connection.view.base_url, connection.secret)
            window = QueryWindow(current - timedelta(hours=1), current, 60)
            window.validate(query_count=len(candidates))
            semaphore = asyncio.Semaphore(MAX_ZERO_HOP_CONCURRENCY)

            finished_positions: set[int] = set()

            async def read_catalog() -> None:
                nonlocal source_catalog_names
                try:
                    source_catalog_names = tuple(
                        await reader.metric_names(window.start, window.end)
                    )
                except Exception:  # noqa: BLE001 - P0 evidence remains usable with a reduced catalog
                    source_catalog_names = ()

            async def read(position: int, candidate: _MetricCandidate) -> None:
                nonlocal completed
                async with semaphore:
                    try:
                        raw = await reader.query_range(
                            candidate.expression, window.start, window.end, window.step_seconds
                        )
                        result = classify_read(raw)
                    except Exception as exc:  # noqa: BLE001 - converted to a typed safe degradation
                        degradations.append(
                            InvestigationDegradationV1(
                                "metrics",
                                "SOURCE_UNAVAILABLE",
                                f"指标源读取未完成（{str(getattr(exc, 'code', 'METRIC_SOURCE_UNAVAILABLE'))[:96]}）",
                            )
                        )
                        finished_positions.add(position)
                        completed += 1
                        return
                    completed += 1
                    finished_positions.add(position)
                    ref = evidence_ref(candidate.alert_ref, position)
                    if result.status is MetricReadStatus.EMPTY_NO_DATA:
                        empty.append(MetricEmptyObservationV1(ref, candidate.alert_ref, candidate.name))
                    elif result.status is MetricReadStatus.SUCCESS:
                        summary, sample = summarize_series(result.series)
                        observations.append(
                            MetricObservationV1(
                                ref,
                                candidate.alert_ref,
                                candidate.name,
                                summary,
                                sample,
                                result.series,
                            )
                        )

            try:
                async with asyncio.timeout(MAX_ZERO_HOP_WALL_CLOCK_SECONDS):
                    async with asyncio.TaskGroup() as group:
                        group.create_task(read_catalog())
                        for position, candidate in enumerate(candidates):
                            group.create_task(read(position, candidate))
            except TimeoutError:
                for position, candidate in enumerate(candidates):
                    if position not in finished_positions:
                        degradations.append(
                            InvestigationDegradationV1(
                                "metrics",
                                "ZERO_HOP_DEADLINE_REACHED",
                                f"{candidate.name} 未在 20 秒上限内完成，已按现有证据生成简报",
                            )
                        )

        if not candidates:
            degradations.append(
                InvestigationDegradationV1(
                    "metrics",
                    "METRIC_READ_PLAN_EMPTY",
                    "当前成员没有可安全派生的主曲线，未执行指标查询",
                )
            )
        if occurrence.service_id is None:
            degradations.append(
                InvestigationDegradationV1(
                    "service",
                    "SERVICE_UNMAPPED",
                    "本次事件尚未映射服务，证据范围仍保留来源与全部告警成员",
                )
            )
        try:
            similar_views = self._incidents.list_similar_history(occurrence.id)
            similar_history = tuple(
                SimilarHistoryObservationV1(
                    rank=rank,
                    occurrence_id=item.occurrence_id,
                    score=item.score,
                    match_reasons=tuple(asdict(reason) for reason in item.match_reasons),
                    resolution_code=item.resolution_code,
                    operator_conclusion=item.operator_conclusion,
                    task_outcome=item.task_outcome,
                    handling_duration_seconds=item.handling_duration_seconds,
                    resolved_at=item.resolved_at,
                )
                for rank, item in enumerate(similar_views, start=1)
            )
            history_snapshot = {
                "revision": 1,
                "status": "AVAILABLE",
                "matches": [
                    {
                        "occurrence_id": item.occurrence_id,
                        "score": item.score,
                        "resolution": item.resolution_code,
                        "operator_outcome": item.operator_conclusion,
                        "task_outcome": item.task_outcome,
                        "duration_seconds": item.handling_duration_seconds,
                        "match_reason": "；".join(
                            f"{reason['kind']} +{reason['points']}"
                            for reason in item.match_reasons
                        ),
                    }
                    for item in similar_history
                ],
            }
        except Exception:
            similar_history = ()
            history_snapshot = {
                "revision": 1,
                "status": "HISTORY_NOT_AVAILABLE",
                "matches": [],
            }
            degradations.append(
                InvestigationDegradationV1(
                    "history",
                    "HISTORY_NOT_AVAILABLE",
                    "本次无法读取相似历史处置；当前告警、指标与人工事实仍可独立使用",
                )
            )
        degraded_domains = tuple(sorted({item.domain for item in degradations}))
        findings = (
            f"本次调查记录 {len(refs)} 条告警，其中 {len(detailed)} 条保留详细信息",
            f"取得 {len(observations)} 条非空指标事实，{len(empty)} 条窗口内无数据事实",
        )
        brief = EvidenceBriefV1(
            len(refs),
            len(detailed),
            len(candidates),
            completed,
            len(observations),
            len(empty),
            degraded_domains,
            findings,
        )
        all_templates = tuple(sorted(
            self._observability.list_metric_templates(), key=lambda item: item.id
        ))
        template_revision = ",".join(
            f"{item.id}:{item.version}" for item in all_templates
        ) or "empty"
        catalog_names = freeze_metric_catalog(
            tuple(item.expression for item in candidates)
            + tuple(item.promql for item in all_templates if item.enabled),
            source_catalog_names,
        )
        catalog_revision = metric_catalog_revision(
            catalog_names, template_revision=template_revision
        )
        model_channel = self._observability.active_model_channel()
        scope = InvestigationScopeResolutionV1(
            occurrence.source_id,
            occurrence.id,
            occurrence.incident_id,
            occurrence.service_id,
            tuple(item.alert_ref for item in refs),
            catalog_revision,
        )
        playbook = select_playbook(service_id=occurrence.service_id)
        summarized: dict[str, int] = {}
        for item in ordered_members[MAX_DETAILED_ALERTS:]:
            key = f"{item.alertname}|{item.severity}|{item.source_state}"
            summarized[key] = summarized.get(key, 0) + 1
        detailed_alerts = []
        for ref in detailed:
            member_view = members_by_id[ref.alert_id]
            detailed_alerts.append({
                "alert_ref": ref.alert_ref,
                "alertname": member_view.alertname,
                "severity": member_view.severity,
                "source_state": member_view.source_state,
                "labels": dict(sorted(member_view.labels.items())),
                "annotations": {
                    key: str(value)[:2000]
                    for key, value in member_view.annotations.items()
                    if key in {"summary", "description"}
                },
                "starts_at": (
                    None if member_view.starts_at is None else member_view.starts_at.isoformat()
                ),
                "last_seen_at": member_view.last_seen_at.isoformat(),
            })
        note_candidates = [
            str(item.detail.get("text") or "")[:1_000]
            for item in self._incidents.list_timeline(
                occurrence.id, after_sequence=0, limit=200
            )
            if item.event_type == "MANUAL_NOTE"
            and item.detail.get("redacted") is not True
            and str(item.detail.get("text") or "")
        ][-10:]
        notes: list[str] = []
        note_chars = 0
        for note_text in note_candidates:
            remaining = 8_000 - note_chars
            if remaining <= 0:
                break
            value = note_text[:remaining]
            if value:
                notes.append(value)
                note_chars += len(value)
        snapshot = InitialInvestigationSnapshotV1(
            schema_revision=1,
            occurrence={
                "revision": 1,
                "id": occurrence.id,
                "incident_id": occurrence.incident_id,
                "occurrence_no": occurrence.occurrence_no,
                "source_id": occurrence.source_id,
                "service_id": occurrence.service_id,
                "signal_state": occurrence.signal_state,
                "response_state": occurrence.response_state,
                "version": occurrence.version,
            },
            alerts={
                "revision": 1,
                "total": len(refs),
                "detailed": detailed_alerts,
                "all_refs": [item.alert_ref for item in refs],
                "summarized_count": max(0, len(refs) - len(detailed)),
                "summaries": [
                    {"group": key, "count": count}
                    for key, count in sorted(summarized.items())
                ],
            },
            metrics={"revision": 1, "evidence_refs": [item.evidence_ref for item in observations + empty]},
            grafana={"revision": 1, "status": "NOT_REQUIRED_AT_INVESTIGATION_RUNTIME"},
            history=history_snapshot,
            context={
                "revision": 1,
                "scope": asdict(scope),
                "playbook": asdict(playbook),
                "trigger": "INTERACTIVE_OPERATOR",
                "snapshot_at": current.isoformat(),
                "metric_catalog": list(catalog_names),
                "notes": notes,
                "prompt_profile": {
                    "id": prompt_profile.id,
                    "revision": prompt_profile.revision,
                    "name": prompt_profile.name,
                    "guidance": prompt_profile.guidance.compact(),
                },
                "model_channel": (
                    None
                    if model_channel is None
                    else {
                        "id": model_channel.id,
                        "revision_no": model_channel.revision_no,
                        "kind": model_channel.kind,
                        "model": model_channel.model,
                    }
                ),
            },
        )
        return self._store.finalize(
            claim.investigation.id,
            snapshot=snapshot,
            observations=tuple(observations),
            empty_observations=tuple(empty),
            similar_history=similar_history,
            degradations=tuple(degradations),
            brief=brief,
            model_channel_active=model_channel is not None,
            now=datetime.now(UTC),
        )
