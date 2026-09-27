"""Explicit investigation start and read-only P0 evidence surface."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Header, Request, Response

from app.api.v1.contracts import validate_idempotency_key
from app.api.v1.operator_witness import OperatorWitness
from app.api.v1.schemas import (
    AlertEvidenceResponse,
    AnalystResultResponse,
    ErrorEnvelope,
    InvestigationDegradationResponse,
    InvestigationDailyUsageResponse,
    InvestigationFeedbackInput,
    InvestigationFeedbackResponse,
    InvestigationResponse,
    InvestigationToolActionResponse,
    InvestigationUsageResponse,
    MetricObservationResponse,
    MetricSamplePointResponse,
    PlannerStepResponse,
    StartInvestigationInput,
)
from app.application.investigations import (
    InvestigationDailyUsageView,
    InvestigationStore,
    InvestigationView,
    StartInvestigation,
)
from app.domains.investigations.degradation import (
    degradation_display_domain,
    degradation_display_message,
    degradation_guidance,
)
from app.platform.errors import SafeApiError
from app.platform.utc import to_utc_iso


def _degradation_response(domain: str, code: str, message: str) -> InvestigationDegradationResponse:
    display_domain = degradation_display_domain(domain, code)
    guidance = degradation_guidance(display_domain, code)
    return InvestigationDegradationResponse(
        domain=display_domain,
        code=code,
        message=degradation_display_message(domain, code, message),
        impact=guidance.impact,
        preserved=guidance.preserved,
        next_step=guidance.next_step,
    )


def _response(item: InvestigationView) -> InvestigationResponse:
    return InvestigationResponse(
        id=item.id,
        occurrence_id=item.occurrence_id,
        incident_id=item.incident_id,
        status=item.status,  # type: ignore[arg-type]
        phase=item.phase,
        member_total=item.member_total,
        member_detailed=item.member_detailed,
        query_planned=item.query_planned,
        query_completed=item.query_completed,
        successful_metric_facts=item.successful_metric_facts,
        empty_metric_facts=item.empty_metric_facts,
        foundation_successful_metric_facts=item.foundation_successful_metric_facts,
        foundation_empty_metric_facts=item.foundation_empty_metric_facts,
        degraded_domains=list(item.degraded_domains),
        degradations=[
            _degradation_response(
                degradation.domain,
                degradation.code,
                degradation.message,
            )
            for degradation in item.degradations
        ],
        findings=list(item.findings),
        job_id=item.job_id,
        model_cost_status=item.model_cost_status,  # type: ignore[arg-type]
        snapshot_occurrence_version=item.snapshot_occurrence_version,
        planner_rounds=item.planner_rounds,
        metric_queries_total=item.metric_queries_total,
        accounted_tokens=item.accounted_tokens,
        termination_reason=item.termination_reason,
        cancel_requested_at=(
            None if item.cancel_requested_at is None else to_utc_iso(item.cancel_requested_at)
        ),
        planner_steps=[
            PlannerStepResponse(
                sequence=step.sequence,
                action=step.action,
                outcome=step.outcome,
                metric_name=step.metric_name,
                safe_code=step.safe_code,
                result_metric_names=list(step.result_metric_names),
            )
            for step in item.planner_steps
        ],
        alert_evidence=[
            AlertEvidenceResponse(
                evidence_ref=evidence.evidence_ref,
                alertname=evidence.alertname,
            )
            for evidence in item.alert_evidence
        ],
        metric_observations=[
            MetricObservationResponse(
                evidence_ref=observation.evidence_ref,
                alert_ref=observation.alert_ref,
                metric_name=observation.metric_name,
                series_count=int(observation.l1_summary.get("series_count") or 0),
                point_count=int(observation.l1_summary.get("point_count") or 0),
                minimum=_optional_float(observation.l1_summary.get("minimum")),
                maximum=_optional_float(observation.l1_summary.get("maximum")),
                latest=_optional_float(observation.l1_summary.get("latest")),
                sample=[
                    MetricSamplePointResponse(timestamp=timestamp, value=value)
                    for timestamp, value in observation.l2_sample
                ],
            )
            for observation in item.metric_observations
        ],
        analyst_result=(
            None
            if item.analyst_result is None
            else AnalystResultResponse.model_validate(item.analyst_result.model_dump())
        ),
        analyst_calls=item.analyst_calls,
        analyst_prompt_tokens=item.analyst_prompt_tokens,
        analyst_completion_tokens=item.analyst_completion_tokens,
        prompt_profile_id=item.prompt_profile_id,
        prompt_profile_name=item.prompt_profile_name,
        prompt_profile_revision=item.prompt_profile_revision,
        playbook_id=item.playbook_id,
        playbook_revision=item.playbook_revision,
        usage=InvestigationUsageResponse(
            initiator_kind="INTERACTIVE_OPERATOR",
            request_id=item.usage.request_id,
            source_ip=item.usage.source_ip,
            planner_calls=item.usage.planner_calls,
            analyst_calls=item.usage.analyst_calls,
            metric_queries=item.usage.metric_queries,
            accounted_tokens=item.usage.accounted_tokens,
            model_execution_mode=item.usage.model_execution_mode,  # type: ignore[arg-type]
            model_channel_id=item.usage.model_channel_id,
            model_channel_revision=item.usage.model_channel_revision,
            prompt_profile_revision=item.usage.prompt_profile_revision,
            playbook_revision=item.usage.playbook_revision,
            egress_categories=list(item.usage.egress_categories),
            pricing_revision=item.usage.pricing_revision,
            cost_status=item.usage.cost_status,  # type: ignore[arg-type]
            p0_mtti_ms=item.usage.p0_mtti_ms,
            p1_mtti_ms=item.usage.p1_mtti_ms,
            p2_mtti_ms=item.usage.p2_mtti_ms,
            query_yield=item.usage.query_yield,
            evidence_gain=item.usage.evidence_gain,
            degradation_count=item.usage.degradation_count,
            canceled=item.usage.canceled,
            tool_actions=[
                InvestigationToolActionResponse(
                    sequence=action.sequence,
                    action=action.action,
                    outcome=action.outcome,
                    metric_name=action.metric_name,
                    window=action.window,
                    aggregation=action.aggregation,
                    label_names=list(action.label_names),
                    group_by=list(action.group_by),
                    safe_code=action.safe_code,
                )
                for action in item.usage.tool_actions
            ],
        ),
        feedback=(
            None
            if item.feedback is None
            else InvestigationFeedbackResponse(
                sequence=item.feedback.sequence,
                rating=item.feedback.rating,  # type: ignore[arg-type]
                created_at=to_utc_iso(item.feedback.created_at),
            )
        ),
        created_at=to_utc_iso(item.created_at),
        updated_at=to_utc_iso(item.updated_at),
    )


def _daily_usage_response(
    item: InvestigationDailyUsageView,
) -> InvestigationDailyUsageResponse:
    return InvestigationDailyUsageResponse(
        day_utc=item.day_utc,
        schema_revision=item.schema_revision,
        run_count=item.run_count,
        terminal_run_count=item.terminal_run_count,
        p2_valid_count=item.p2_valid_count,
        evidence_only_count=item.evidence_only_count,
        canceled_count=item.canceled_count,
        contract_rejected_count=item.contract_rejected_count,
        dependency_failed_count=item.dependency_failed_count,
        model_started_run_count=item.model_started_run_count,
        fake_model_started_run_count=item.fake_model_started_run_count,
        external_model_started_run_count=item.external_model_started_run_count,
        unknown_model_started_run_count=item.unknown_model_started_run_count,
        planner_calls=item.planner_calls,
        analyst_calls=item.analyst_calls,
        metric_queries=item.metric_queries,
        accounted_tokens=item.accounted_tokens,
        unknown_cost_run_count=item.unknown_cost_run_count,
        feedback_response_count=item.feedback_response_count,
        feedback_adopted_count=item.feedback_adopted_count,
        degraded_run_count=item.degraded_run_count,
        p0_mtti_count=item.p0_mtti_count,
        p0_mtti_sum_ms=item.p0_mtti_sum_ms,
        p1_mtti_count=item.p1_mtti_count,
        p1_mtti_sum_ms=item.p1_mtti_sum_ms,
        p2_mtti_count=item.p2_mtti_count,
        p2_mtti_sum_ms=item.p2_mtti_sum_ms,
        updated_at=to_utc_iso(item.updated_at),
    )


def _optional_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (int, float, str)):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _errors() -> dict[int | str, dict[str, Any]]:
    return {
        400: {"model": ErrorEnvelope},
        404: {"model": ErrorEnvelope},
        409: {"model": ErrorEnvelope},
        422: {"model": ErrorEnvelope},
        428: {"model": ErrorEnvelope},
    }


def create_investigations_router(
    *, store: InvestigationStore, starter: StartInvestigation
) -> APIRouter:
    router = APIRouter(prefix="/api/v1")

    @router.get(
        "/occurrences/{occurrence_id}/investigations",
        response_model=list[InvestigationResponse],
        responses=_errors(),
    )
    async def list_investigations(occurrence_id: int) -> list[InvestigationResponse]:
        return [_response(item) for item in store.list_for_occurrence(occurrence_id)]

    @router.get(
        "/investigation-usage/daily",
        response_model=list[InvestigationDailyUsageResponse],
        responses=_errors(),
    )
    async def list_daily_investigation_usage(
        from_day: date | None = None,
        to_day: date | None = None,
    ) -> list[InvestigationDailyUsageResponse]:
        current_day = datetime.now(timezone.utc).date()
        end = to_day or current_day
        start = from_day or (end - timedelta(days=29))
        try:
            values = store.list_daily_usage(from_day=start, to_day=end)
        except ValueError as exc:
            raise SafeApiError(
                status_code=422,
                code="INVESTIGATION_USAGE_RANGE_INVALID",
                message="调查用量日期范围无效；最多读取连续 366 个 UTC 日桶",
            ) from exc
        return [_daily_usage_response(item) for item in values]

    @router.get(
        "/investigations/{investigation_id}",
        response_model=InvestigationResponse,
        responses=_errors(),
    )
    async def get_investigation(investigation_id: str) -> InvestigationResponse:
        try:
            return _response(store.get(investigation_id))
        except LookupError as exc:
            raise SafeApiError(status_code=404, code="INVESTIGATION_NOT_FOUND", message="未找到指定证据调查") from exc

    @router.post(
        "/occurrences/{occurrence_id}/investigations",
        response_model=InvestigationResponse,
        responses=_errors(),
    )
    async def start_investigation(
        request: Request,
        response: Response,
        occurrence_id: int,
        payload: StartInvestigationInput | None = None,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> InvestigationResponse:
        key = validate_idempotency_key(idempotency_key)
        try:
            item = await starter.execute(
                occurrence_id,
                request_key=key,
                actor=OperatorWitness().actor(),
                request_id=str(getattr(request.state, "request_id", "unavailable")),
                source_ip=request.client.host if request.client is not None else "unavailable",
                prompt_profile_id=None if payload is None else payload.prompt_profile_id,
            )
        except LookupError as exc:
            raise SafeApiError(status_code=404, code="INVESTIGATION_SCOPE_NOT_FOUND", message="未找到本次事件的调查范围") from exc
        except FileExistsError as exc:
            raise SafeApiError(status_code=409, code="INVESTIGATION_ALREADY_ACTIVE", message="本次事件已有正在进行的证据调查") from exc
        except (RuntimeError, ValueError, PermissionError) as exc:
            raise SafeApiError(status_code=422, code=str(exc), message="本次证据调查未启动，事件状态没有改变") from exc
        response.status_code = 202 if item.status == "QUEUED" else 200
        return _response(item)

    @router.post(
        "/investigations/{investigation_id}/cancel",
        response_model=InvestigationResponse,
        responses=_errors(),
    )
    async def cancel_investigation(investigation_id: str) -> InvestigationResponse:
        try:
            return _response(store.request_cancel(
                investigation_id,
                actor=OperatorWitness().actor(),
                now=datetime.now(timezone.utc),
            ))
        except LookupError as exc:
            raise SafeApiError(
                status_code=404,
                code="INVESTIGATION_NOT_FOUND",
                message="未找到指定证据调查",
            ) from exc
        except PermissionError as exc:
            raise SafeApiError(
                status_code=422,
                code="INTERACTIVE_OPERATOR_REQUIRED",
                message="本次取消未提交，已有证据保持不变",
            ) from exc

    @router.post(
        "/investigations/{investigation_id}/feedback",
        response_model=InvestigationFeedbackResponse,
        status_code=201,
        responses=_errors(),
    )
    async def record_investigation_feedback(
        investigation_id: str, payload: InvestigationFeedbackInput
    ) -> InvestigationFeedbackResponse:
        try:
            value = store.record_feedback(
                investigation_id,
                rating=payload.rating,
                actor=OperatorWitness().actor(),
                now=datetime.now(timezone.utc),
            )
        except LookupError as exc:
            raise SafeApiError(
                status_code=404,
                code="INVESTIGATION_NOT_FOUND",
                message="未找到指定证据调查",
            ) from exc
        except (RuntimeError, ValueError, PermissionError) as exc:
            raise SafeApiError(
                status_code=422,
                code=str(exc)[:96],
                message="本次反馈未记录，已有调查结果保持不变",
            ) from exc
        return InvestigationFeedbackResponse(
            sequence=value.sequence,
            rating=value.rating,  # type: ignore[arg-type]
            created_at=to_utc_iso(value.created_at),
        )

    return router
