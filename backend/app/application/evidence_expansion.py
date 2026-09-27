"""Durable, bounded Planner loop over frozen investigation evidence."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Protocol
from typing import Mapping

from app.application.observability import ObservabilityPort, ThanosReadPort
from app.domains.investigations.analyst import (
    AnalystAlertSummaryV1,
    AnalystAlertV1,
    AnalystContractError,
    AnalystEmptyEvidenceV1,
    AnalystHistoryV1,
    AnalystMetricEvidenceV1,
    AnalystModelRequest,
    AnalystNoteV1,
    AnalystResultV1,
    analyst_messages,
    analyst_response_format,
    available_evidence_ids,
    validate_analyst_result,
)
from app.domains.investigations.models import (
    InvestigationDegradationV1,
    MetricEmptyObservationV1,
    MetricObservationV1,
    evidence_ref,
    summarize_series,
)
from app.domains.investigations.planner import (
    BudgetRefusal,
    InvestigationBudgetV1,
    InvestigationScopeV1,
    LabelRejectionV1,
    MetricDomainGateV1,
    MetricDescriptorV1,
    MetricReadPlanCompiler,
    PlannerAlertV1,
    PlannerEmptyFactV1,
    PlannerMetricFactV1,
    PlannerModelRequest,
    PlannerReplyV1,
    PlannerStepSummaryV1,
    PlannerStepV1,
    estimate_message_tokens,
    list_metrics,
    parse_planner_decision,
    planner_messages,
    planner_tool_schemas,
    sanitize_planner_labels,
)
from app.domains.metrics.models import MetricReadStatus, classify_read

UTC = timezone.utc
MAX_DESCRIBED_LABEL_VALUES = 8


@dataclass(frozen=True, slots=True)
class EvidenceExpansionContext:
    investigation_id: str
    source_id: str
    occurrence_id: int
    catalog_revision: str
    playbook_revision: int
    model_channel_id: str
    model_channel_revision: int
    model_kind: str
    model_name: str
    available_metric_names: tuple[str, ...]
    alerts: tuple[PlannerAlertV1, ...]
    metric_facts: tuple[PlannerMetricFactV1, ...]
    empty_facts: tuple[PlannerEmptyFactV1, ...]
    described_metrics: tuple[MetricDescriptorV1, ...]
    previous_steps: tuple[PlannerStepSummaryV1, ...]
    compiled_fingerprints: tuple[str, ...]
    budget: InvestigationBudgetV1
    cancel_requested_at: datetime | None
    prompt_profile_guidance: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AnalystExecutionContext:
    investigation_id: str
    snapshot_revision: int
    prompt_profile_revision: int
    playbook_revision: int
    model_channel_id: str
    model_channel_revision: int
    model_kind: str
    model_name: str
    alerts: tuple[AnalystAlertV1, ...]
    alert_summaries: tuple[AnalystAlertSummaryV1, ...]
    metric_evidence: tuple[AnalystMetricEvidenceV1, ...]
    empty_evidence: tuple[AnalystEmptyEvidenceV1, ...]
    similar_history: tuple[AnalystHistoryV1, ...]
    notes: tuple[AnalystNoteV1, ...]
    degraded_domains: tuple[str, ...]
    budget: InvestigationBudgetV1
    prompt_profile_guidance: Mapping[str, str] = field(default_factory=dict)


class EvidenceExpansionStore(Protocol):
    def load_expansion_context(self, investigation_id: str) -> EvidenceExpansionContext: ...

    def record_label_rejections(
        self,
        investigation_id: str,
        rejections: tuple[LabelRejectionV1, ...],
        *,
        now: datetime,
    ) -> None: ...

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
    ) -> None: ...

    def is_cancel_requested(self, investigation_id: str) -> bool: ...

    def finish_expansion(
        self, investigation_id: str, *, reason: str, now: datetime
    ) -> None: ...

    def finish_canceled(
        self,
        investigation_id: str,
        *,
        canceled_before: str,
        budget: InvestigationBudgetV1,
        now: datetime,
    ) -> None: ...

    def load_analyst_context(self, investigation_id: str) -> AnalystExecutionContext: ...

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
    ) -> None: ...

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
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class PlannerModelTarget:
    base_url: str
    model: str
    api_key: str = field(repr=False, default="")


class PlannerModelClient(Protocol):
    async def complete(
        self,
        *,
        messages: tuple[dict[str, object], ...],
        tools: tuple[dict[str, object], ...] = (),
        response_format: dict[str, object] | None = None,
    ) -> PlannerReplyV1: ...


class PlannerModelCallError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code[:96]
        super().__init__(self.code)


ModelClientFactory = Callable[[str, PlannerModelTarget], PlannerModelClient]


def _safe_model_code(exc: PlannerModelCallError) -> str:
    return f"MODEL_{exc.code}"[:96]


class RunEvidenceExpansion:
    """Expand typed evidence, then run one evidence-bound no-tool Analyst."""

    def __init__(
        self,
        *,
        store: EvidenceExpansionStore,
        observability: ObservabilityPort,
        thanos_factory: Callable[[str, str], ThanosReadPort],
        model_client_factory: ModelClientFactory,
    ) -> None:
        self._store = store
        self._observability = observability
        self._thanos_factory = thanos_factory
        self._model_client_factory = model_client_factory

    def _cancel_if_requested(
        self,
        investigation_id: str,
        *,
        before: str,
        budget: InvestigationBudgetV1,
        now: datetime,
    ) -> bool:
        if not self._store.is_cancel_requested(investigation_id):
            return False
        self._store.finish_canceled(
            investigation_id, canceled_before=before, budget=budget, now=now
        )
        return True

    async def _run_analyst(
        self,
        investigation_id: str,
        *,
        model: PlannerModelClient,
        budget: InvestigationBudgetV1,
        now: datetime,
    ) -> None:
        if self._cancel_if_requested(
            investigation_id, before="ANALYST_MODEL_CALL", budget=budget, now=now
        ):
            return
        context = self._store.load_analyst_context(investigation_id)
        request = AnalystModelRequest(
            investigation_id=context.investigation_id,
            snapshot_revision=context.snapshot_revision,
            prompt_profile_revision=context.prompt_profile_revision,
            playbook_revision=context.playbook_revision,
            alerts=context.alerts,
            alert_summaries=context.alert_summaries,
            metric_evidence=context.metric_evidence,
            empty_evidence=context.empty_evidence,
            similar_history=context.similar_history,
            notes=context.notes,
            degraded_domains=context.degraded_domains,
            prompt_profile_guidance=context.prompt_profile_guidance,
        )
        messages = analyst_messages(request)
        evidence_ids = available_evidence_ids(request)
        analyst_calls = 0
        prompt_tokens = 0
        completion_tokens = 0
        invalid_raw: str | None = None
        for attempt in range(2):
            call_time = datetime.now(UTC)
            if self._cancel_if_requested(
                investigation_id,
                before="ANALYST_MODEL_CALL",
                budget=budget,
                now=call_time,
            ):
                return
            estimated_tokens = estimate_message_tokens(messages)
            try:
                budget.before_analyst_call(
                    estimated_prompt_tokens=estimated_tokens, now=call_time
                )
            except BudgetRefusal as exc:
                self._store.finish_analyst_failure(
                    investigation_id,
                    reason=str(exc),
                    invalid_raw=invalid_raw,
                    budget=budget,
                    analyst_calls=analyst_calls,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    now=call_time,
                )
                return
            try:
                reply = await model.complete(
                    messages=messages,
                    tools=(),
                    response_format=analyst_response_format(),
                )
            except PlannerModelCallError as exc:
                self._store.finish_analyst_failure(
                    investigation_id,
                    reason=_safe_model_code(exc),
                    invalid_raw=None,
                    budget=budget,
                    analyst_calls=analyst_calls + 1,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    now=datetime.now(UTC),
                )
                return
            analyst_calls += 1
            prompt_tokens += max(0, reply.prompt_tokens)
            completion_tokens += max(0, reply.completion_tokens)
            budget = budget.after_analyst_call(
                estimated_prompt_tokens=estimated_tokens,
                prompt_tokens=reply.prompt_tokens,
                completion_tokens=reply.completion_tokens,
            )
            if self._cancel_if_requested(
                investigation_id,
                before="ANALYST_RESULT_PERSIST",
                budget=budget,
                now=datetime.now(UTC),
            ):
                return
            try:
                result = validate_analyst_result(
                    reply.text, available_evidence_ids=evidence_ids
                )
            except AnalystContractError:
                invalid_raw = reply.text
                if attempt == 0:
                    messages = messages + (
                        {"role": "assistant", "content": reply.text},
                        {
                            "role": "user",
                            "content": (
                                "Return the same analysis once more using exactly the supplied "
                                "JSON schema and only supplied evidence references. Write every "
                                "human-readable field in Simplified Chinese, preserve alert versus "
                                "metric evidence ownership, and return at most three distinct actions."
                            ),
                        },
                    )
                    continue
                self._store.finish_analyst_failure(
                    investigation_id,
                    reason="ANALYST_CONTRACT_REJECTED",
                    invalid_raw=invalid_raw,
                    budget=budget,
                    analyst_calls=analyst_calls,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    now=datetime.now(UTC),
                )
                return
            self._store.record_analyst_result(
                investigation_id,
                result=result,
                prompt_profile_revision=context.prompt_profile_revision,
                budget=budget,
                analyst_calls=analyst_calls,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                now=datetime.now(UTC),
            )
            return

    async def execute(
        self, investigation_id: str, *, now: datetime | None = None
    ) -> None:
        started = now or datetime.now(UTC)
        context = self._store.load_expansion_context(investigation_id)
        budget = replace(context.budget, started_at=started)
        if context.previous_steps and context.previous_steps[-1].action == "FINISH":
            self._store.finish_expansion(
                investigation_id, reason="PLANNER_FINISHED", now=started
            )
            return
        if self._cancel_if_requested(
            investigation_id, before="PLANNER_MODEL_CALL", budget=budget, now=started
        ):
            return

        allowed_keys = set(self._observability.planner_label_keys(context.source_id))
        sanitized_alerts: list[PlannerAlertV1] = []
        rejections: list[LabelRejectionV1] = []
        for alert in context.alerts:
            labels, rejected = sanitize_planner_labels(
                alert.labels,
                allowed_keys=allowed_keys,
                alert_ref=alert.alert_ref,
            )
            sanitized_alerts.append(replace(alert, labels=labels))
            rejections.extend(rejected)
        self._store.record_label_rejections(
            investigation_id, tuple(rejections), now=started
        )

        try:
            model_secret = self._observability.load_model_secret_revision(
                context.model_channel_id, context.model_channel_revision
            )
            model = self._model_client_factory(
                context.model_kind,
                PlannerModelTarget(
                    base_url=model_secret.view.base_url,
                    model=context.model_name,
                    api_key=model_secret.api_key,
                ),
            )
        except (LookupError, RuntimeError, ValueError):
            self._store.finish_expansion(
                investigation_id, reason="MODEL_CHANNEL_UNAVAILABLE", now=started
            )
            return
        try:
            monitoring = self._observability.load_monitoring_secret(
                context.source_id, "THANOS", require_active=True
            )
        except (LookupError, RuntimeError):
            self._store.finish_expansion(
                investigation_id, reason="METRIC_SOURCE_UNAVAILABLE", now=started
            )
            return
        reader = self._thanos_factory(monitoring.view.base_url, monitoring.secret)
        described = {item.name: item for item in context.described_metrics}
        steps = list(context.previous_steps)
        compiled_fingerprints = set(context.compiled_fingerprints)
        described_attempts = {
            item.metric_name
            for item in context.previous_steps
            if item.action == "DESCRIBE_METRIC" and item.metric_name is not None
        }
        metric_facts = list(context.metric_facts)
        empty_facts = list(context.empty_facts)
        metric_gate = MetricDomainGateV1.restore(context.previous_steps)

        while True:
            call_time = datetime.now(UTC)
            if self._cancel_if_requested(
                investigation_id,
                before="PLANNER_MODEL_CALL",
                budget=budget,
                now=call_time,
            ):
                return
            request = PlannerModelRequest(
                investigation_id=context.investigation_id,
                catalog_revision=context.catalog_revision,
                playbook_revision=context.playbook_revision,
                available_metric_names=context.available_metric_names,
                alerts=tuple(sanitized_alerts),
                metric_facts=tuple(metric_facts),
                empty_facts=tuple(empty_facts),
                described_metrics=tuple(described[name] for name in sorted(described)),
                previous_steps=tuple(steps),
                budget=budget,
                prompt_profile_guidance=context.prompt_profile_guidance,
            )
            messages = planner_messages(request)
            estimated_tokens = estimate_message_tokens(messages)
            try:
                budget.before_planner_call(
                    estimated_prompt_tokens=estimated_tokens, now=call_time
                )
            except BudgetRefusal as exc:
                # Reaching the Planner round ceiling, or the point where one
                # more Planner request would consume the protected Analyst
                # reserve, is a handoff condition once useful metric evidence
                # exists. The reserve exists for exactly this call; ending the
                # whole run here would strand valid P1 evidence without P2.
                if metric_facts and str(exc) in {
                    "PLANNER_ROUND_LIMIT",
                    "ANALYST_RESERVE_REQUIRED",
                }:
                    await self._run_analyst(
                        investigation_id,
                        model=model,
                        budget=budget,
                        now=call_time,
                    )
                    return
                self._store.finish_expansion(
                    investigation_id, reason=str(exc), now=call_time
                )
                return
            try:
                reply = await model.complete(
                    messages=messages, tools=planner_tool_schemas()
                )
            except PlannerModelCallError as exc:
                budget = budget.after_planner_call(
                    estimated_prompt_tokens=estimated_tokens,
                    prompt_tokens=0,
                    completion_tokens=0,
                )
                step = PlannerStepV1(
                    budget.planner_rounds,
                    "MODEL_CALL",
                    "FAILED",
                    None,
                    None,
                    None,
                    None,
                    (),
                    (),
                    None,
                    _safe_model_code(exc),
                    0,
                    0,
                )
                self._store.record_planner_step(
                    investigation_id, step, budget=budget, now=datetime.now(UTC)
                )
                self._store.finish_expansion(
                    investigation_id,
                    reason=_safe_model_code(exc),
                    now=datetime.now(UTC),
                )
                return

            budget = budget.after_planner_call(
                estimated_prompt_tokens=estimated_tokens,
                prompt_tokens=reply.prompt_tokens,
                completion_tokens=reply.completion_tokens,
            )
            after_model = datetime.now(UTC)
            if self._cancel_if_requested(
                investigation_id,
                before="PLANNER_TOOL_CALL",
                budget=budget,
                now=after_model,
            ):
                return
            try:
                decision = parse_planner_decision(reply)
            except ValueError as exc:
                step = PlannerStepV1(
                    budget.planner_rounds,
                    "MODEL_RESPONSE",
                    "REJECTED",
                    None,
                    None,
                    None,
                    None,
                    (),
                    (),
                    None,
                    str(exc)[:96],
                    reply.prompt_tokens,
                    reply.completion_tokens,
                )
                self._store.record_planner_step(
                    investigation_id, step, budget=budget, now=after_model
                )
                self._store.finish_expansion(
                    investigation_id, reason=str(exc)[:96], now=after_model
                )
                return

            sequence = budget.planner_rounds
            if decision.action == "FINISH":
                step = PlannerStepV1(
                    sequence, "FINISH", "COMPLETED", None, None, None, None,
                    (), (), None, None, reply.prompt_tokens, reply.completion_tokens,
                )
                self._store.record_planner_step(
                    investigation_id, step, budget=budget, now=after_model
                )
                await self._run_analyst(
                    investigation_id,
                    model=model,
                    budget=budget,
                    now=after_model,
                )
                return

            if decision.action == "LIST_METRICS":
                try:
                    names = list_metrics(
                        context.available_metric_names,
                        pattern=decision.pattern or "",
                    )
                    outcome, list_code = "COMPLETED", None
                except ValueError as exc:
                    names, outcome, list_code = (), "REJECTED", str(exc)[:96]
                step = PlannerStepV1(
                    sequence, "LIST_METRICS", outcome, None, None, None, None,
                    (), (), None, list_code, reply.prompt_tokens, reply.completion_tokens,
                    result_metric_names=names,
                )

            elif decision.action == "DESCRIBE_METRIC":
                metric_name = decision.metric_name or ""
                descriptor: MetricDescriptorV1 | None = None
                describe_code: str | None = None
                if metric_name not in context.available_metric_names:
                    describe_code = "METRIC_NOT_IN_CATALOG"
                elif metric_name in described_attempts:
                    describe_code = "METRIC_DESCRIPTION_ALREADY_ATTEMPTED"
                else:
                    described_attempts.add(metric_name)
                    try:
                        catalog_time = datetime.now(UTC)
                        budget.before_catalog_read(now=catalog_time)
                        if self._cancel_if_requested(
                            investigation_id,
                            before="METRIC_CATALOG_CALL",
                            budget=budget,
                            now=catalog_time,
                        ):
                            return
                        metadata = await reader.metric_metadata(metric_name)
                        catalog_time = datetime.now(UTC)
                        budget.before_catalog_read(now=catalog_time)
                        if self._cancel_if_requested(
                            investigation_id,
                            before="METRIC_CATALOG_CALL",
                            budget=budget,
                            now=catalog_time,
                        ):
                            return
                        label_names = tuple(sorted(set(await reader.metric_label_names(
                            metric_name, started - timedelta(hours=6), started
                        ))))
                        known_values: dict[str, tuple[str, ...]] = {}
                        for label in label_names:
                            if label not in allowed_keys or len(known_values) >= MAX_DESCRIBED_LABEL_VALUES:
                                continue
                            catalog_time = datetime.now(UTC)
                            budget.before_catalog_read(now=catalog_time)
                            if self._cancel_if_requested(
                                investigation_id,
                                before="METRIC_CATALOG_CALL",
                                budget=budget,
                                now=catalog_time,
                            ):
                                return
                            values = await reader.metric_label_values(
                                metric_name, label, started - timedelta(hours=6), started
                            )
                            known_values[label] = tuple(sorted(set(values)))[:100]
                        descriptor = MetricDescriptorV1(
                            metric_name,
                            str((metadata or {}).get("type") or "untyped").lower(),
                            str((metadata or {}).get("help") or "")[:1024],
                            str((metadata or {}).get("unit") or "")[:64],
                            label_names,
                            known_values,
                        )
                        described[metric_name] = descriptor
                    except BudgetRefusal as exc:
                        describe_code = str(exc)[:96]
                    except ValueError as exc:
                        describe_code = str(exc)[:96]
                    except Exception:  # noqa: BLE001 - only stable code crosses boundary
                        describe_code = "SOURCE_UNAVAILABLE"
                step = PlannerStepV1(
                    sequence, "DESCRIBE_METRIC",
                    "COMPLETED" if descriptor is not None else "REJECTED",
                    None, metric_name or None, None, None, (), (), None, describe_code,
                    reply.prompt_tokens, reply.completion_tokens, descriptor=descriptor,
                )

            else:
                plan = decision.read_plan
                assert plan is not None
                observation: MetricObservationV1 | None = None
                empty_observation: MetricEmptyObservationV1 | None = None
                degradation: InvestigationDegradationV1 | None = None
                compiled_fingerprint: str | None = None
                query_code: str | None = None
                outcome = "REJECTED"
                if metric_gate.closed:
                    query_code = "METRIC_DOMAIN_CLOSED"
                else:
                    try:
                        query_time = datetime.now(UTC)
                        budget.before_metric_query(now=query_time)
                        scope = InvestigationScopeV1(
                            context.source_id,
                            context.occurrence_id,
                            tuple(item.alert_ref for item in sanitized_alerts),
                            context.catalog_revision,
                            {item.alert_ref: item.labels for item in sanitized_alerts},
                        )
                        compiled = MetricReadPlanCompiler(tuple(described.values())).compile(
                            plan, scope, now=query_time
                        )
                        compiled_fingerprint = compiled.fingerprint
                        if compiled.fingerprint in compiled_fingerprints:
                            raise ValueError("DUPLICATE_READ_PLAN")
                        compiled_fingerprints.add(compiled.fingerprint)
                        if self._cancel_if_requested(
                            investigation_id,
                            before="METRIC_QUERY",
                            budget=budget,
                            now=datetime.now(UTC),
                        ):
                            return
                        raw = await reader.query_range(
                            compiled.expression,
                            compiled.window_start,
                            compiled.window_end,
                            compiled.step_seconds,
                        )
                        budget = budget.after_metric_query()
                        result = classify_read(raw)
                        ref = evidence_ref(plan.alert_ref, 100 + sequence)
                        if result.status is MetricReadStatus.EMPTY_NO_DATA:
                            empty_observation = MetricEmptyObservationV1(
                                ref, plan.alert_ref, plan.metric_name
                            )
                            empty_facts.append(PlannerEmptyFactV1(
                                ref, plan.alert_ref, plan.metric_name
                            ))
                            outcome = "EMPTY_NO_DATA"
                        else:
                            summary, sample = summarize_series(result.series)
                            observation = MetricObservationV1(
                                ref, plan.alert_ref, plan.metric_name,
                                summary, sample, result.series,
                            )
                            metric_facts.append(PlannerMetricFactV1(
                                ref, plan.alert_ref, plan.metric_name, summary
                            ))
                            outcome = "COMPLETED"
                            metric_gate = metric_gate.successful_read()
                    except BudgetRefusal as exc:
                        query_code = str(exc)[:96]
                    except ValueError as exc:
                        query_code = str(exc)[:96]
                    except Exception:  # noqa: BLE001 - external detail never persisted
                        budget = budget.after_metric_query()
                        query_code = "SOURCE_UNAVAILABLE"
                        metric_gate = metric_gate.source_unavailable()
                        degradation = InvestigationDegradationV1(
                            "metrics",
                            "SOURCE_UNAVAILABLE",
                            "指标源读取未完成；已保留当前证据并停止自动重试",
                        )
                step = PlannerStepV1(
                    sequence, "QUERY_METRIC", outcome, plan.alert_ref,
                    plan.metric_name, plan.window, plan.aggregation,
                    plan.label_filters, plan.group_by, compiled_fingerprint, query_code,
                    reply.prompt_tokens, reply.completion_tokens,
                )
                self._store.record_planner_step(
                    investigation_id,
                    step,
                    budget=budget,
                    observation=observation,
                    empty_observation=empty_observation,
                    degradation=degradation,
                    now=datetime.now(UTC),
                )
                steps.append(PlannerStepSummaryV1(
                    step.sequence, step.action, step.outcome,
                    step.metric_name, step.safe_code, step.result_metric_names,
                ))
                continue

            self._store.record_planner_step(
                investigation_id, step, budget=budget, now=datetime.now(UTC)
            )
            steps.append(PlannerStepSummaryV1(
                step.sequence, step.action, step.outcome,
                step.metric_name, step.safe_code, step.result_metric_names,
            ))


__all__ = [
    "AnalystExecutionContext", "EvidenceExpansionContext",
    "EvidenceExpansionStore",
    "ModelClientFactory",
    "PlannerModelCallError",
    "PlannerModelClient",
    "PlannerModelTarget",
    "RunEvidenceExpansion",
]
