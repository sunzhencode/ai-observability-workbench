"""V2 Incident Investigator API; legacy investigation routes remain read-only."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Header, Request, Response

from app.api.v1.contracts import validate_idempotency_key
from app.api.v1.operator_witness import OperatorWitness
from app.api.v1.schemas import (
    ErrorEnvelope,
    InvestigatorActionResponse,
    InvestigatorActivityResponse,
    InvestigatorFindingResponse,
    InvestigationFeedbackInput,
    InvestigationFeedbackResponse,
    InvestigatorMetricObservationResponse,
    InvestigatorReportResponse,
    InvestigatorRunResponse,
    MetricSamplePointResponse,
)
from app.application.unified_investigations import (
    StartUnifiedInvestigation,
    UnifiedInvestigationStore,
    UnifiedInvestigationView,
)
from app.domains.investigations.runtime import MetricObservationV2
from app.platform.errors import SafeApiError
from app.platform.utc import to_utc_iso


def _metric(item: MetricObservationV2) -> InvestigatorMetricObservationResponse:
    return InvestigatorMetricObservationResponse(
        evidence_id=item.evidence_id,
        metric_id=item.metric_id,
        status=item.status,  # type: ignore[arg-type]
        summary=dict(item.summary),
        sample=[
            MetricSamplePointResponse(timestamp=timestamp, value=value)
            for timestamp, value in item.sample
        ],
    )


def _response(item: UnifiedInvestigationView) -> InvestigatorRunResponse:
    combined: dict[str, MetricObservationV2] = {
        value.evidence_id: value for value in item.snapshot.metric_evidence
    }
    combined.update({value.evidence_id: value for value in item.observations})
    report = item.report
    return InvestigatorRunResponse(
        id=item.id,
        occurrence_id=item.occurrence_id,
        status=item.status.value,
        job_id=item.job_id,
        provider_profile_id=item.provider_profile_id,
        model_channel_id=item.model_channel_id,
        model_revision=item.model_revision,
        alert_evidence=[dict(value) for value in item.snapshot.alert_evidence],
        metric_evidence=[_metric(combined[key]) for key in sorted(combined)],
        degraded_domains=list(item.snapshot.degraded_domains),
        available_metric_count=len(item.tool_scope),
        activities=[
            InvestigatorActivityResponse(
                sequence=value.sequence,
                kind=value.kind,
                status=value.status,
                safe_code=value.safe_code,
                evidence_ids=list(value.evidence_ids),
            )
            for value in item.activities
        ],
        report=(
            None
            if report is None
            else InvestigatorReportResponse(
                summary_zh=report.summary_zh,
                verdict=report.verdict.value,
                confidence=report.confidence,
                findings=[
                    InvestigatorFindingResponse(
                        title_zh=value.title_zh,
                        analysis_zh=value.analysis_zh,
                        evidence_ids=list(value.evidence_ids),
                    )
                    for value in report.findings
                ],
                recommended_actions=[
                    InvestigatorActionResponse(
                        title_zh=value.title_zh,
                        rationale_zh=value.rationale_zh,
                        evidence_ids=list(value.evidence_ids),
                    )
                    for value in report.recommended_actions
                ],
                missing_evidence_zh=list(report.missing_evidence_zh),
                degraded_domains=list(report.degraded_domains),
                evidence_gain=report.evidence_gain,
            )
        ),
        request_count=item.request_count,
        tool_call_count=item.tool_call_count,
        input_tokens=item.input_tokens,
        output_tokens=item.output_tokens,
        safe_error_code=item.safe_error_code,
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


def _errors() -> dict[int | str, dict[str, Any]]:
    return {
        400: {"model": ErrorEnvelope},
        404: {"model": ErrorEnvelope},
        409: {"model": ErrorEnvelope},
        422: {"model": ErrorEnvelope},
    }


def create_unified_investigations_router(
    *, store: UnifiedInvestigationStore, starter: StartUnifiedInvestigation
) -> APIRouter:
    router = APIRouter(prefix="/api/v1")

    @router.get(
        "/occurrences/{occurrence_id}/investigator-runs",
        response_model=list[InvestigatorRunResponse],
        responses=_errors(),
    )
    async def list_runs(occurrence_id: int) -> list[InvestigatorRunResponse]:
        return [_response(item) for item in store.list_for_occurrence(occurrence_id)]

    @router.get(
        "/investigator-runs/{investigation_id}",
        response_model=InvestigatorRunResponse,
        responses=_errors(),
    )
    async def get_run(investigation_id: str) -> InvestigatorRunResponse:
        try:
            return _response(store.get(investigation_id))
        except LookupError as exc:
            raise SafeApiError(
                status_code=404,
                code="INVESTIGATION_V2_NOT_FOUND",
                message="未找到指定调查运行",
            ) from exc

    @router.post(
        "/occurrences/{occurrence_id}/investigator-runs",
        response_model=InvestigatorRunResponse,
        responses=_errors(),
    )
    async def start_run(
        request: Request,
        response: Response,
        occurrence_id: int,
        idempotency_key: Annotated[
            str | None, Header(alias="Idempotency-Key")
        ] = None,
    ) -> InvestigatorRunResponse:
        try:
            item = await starter.execute(
                occurrence_id,
                request_key=validate_idempotency_key(idempotency_key),
                actor=OperatorWitness().actor(),
                request_id=str(getattr(request.state, "request_id", "unavailable")),
                source_ip=(
                    request.client.host if request.client is not None else "unavailable"
                ),
            )
        except LookupError as exc:
            raise SafeApiError(
                status_code=404,
                code="INVESTIGATION_SCOPE_NOT_FOUND",
                message="未找到本次事件的调查范围",
            ) from exc
        except FileExistsError as exc:
            raise SafeApiError(
                status_code=409,
                code="INVESTIGATION_ALREADY_ACTIVE",
                message="本次事件已有正在运行的调查",
            ) from exc
        except (RuntimeError, ValueError, PermissionError) as exc:
            raise SafeApiError(
                status_code=422,
                code=str(exc)[:96],
                message="本次调查未启动，事件状态没有改变",
            ) from exc
        response.status_code = 202 if item.status.value == "QUEUED" else 200
        return _response(item)

    @router.post(
        "/investigator-runs/{investigation_id}/cancel",
        response_model=InvestigatorRunResponse,
        responses=_errors(),
    )
    async def cancel_run(investigation_id: str) -> InvestigatorRunResponse:
        try:
            return _response(
                store.cancel(
                    investigation_id,
                    now=datetime.now(timezone.utc),
                )
            )
        except LookupError as exc:
            raise SafeApiError(
                status_code=404,
                code="INVESTIGATION_V2_NOT_FOUND",
                message="未找到指定调查运行",
            ) from exc

    @router.post(
        "/investigator-runs/{investigation_id}/feedback",
        response_model=InvestigationFeedbackResponse,
        status_code=201,
        responses=_errors(),
    )
    async def record_feedback(
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
                code="INVESTIGATION_V2_NOT_FOUND",
                message="未找到指定调查运行",
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


__all__ = ["create_unified_investigations_router"]
