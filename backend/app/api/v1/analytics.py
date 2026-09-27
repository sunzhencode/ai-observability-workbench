"""Read-only deterministic operational Analytics API."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query

from app.adapters.persistence.analytics import SqlAlchemyAnalyticsStore
from app.api.v1.schemas import (
    AnalyticsAiModeResponse,
    AnalyticsDurationMetricResponse,
    AnalyticsDurationSummaryResponse,
    AnalyticsNotificationModeResponse,
    AnalyticsOverviewResponse,
    AnalyticsRatioResponse,
    AnalyticsResponseSection,
    AnalyticsSignalSection,
    ErrorEnvelope,
)
from app.application.analytics import AnalyticsOverview
from app.platform.utc import to_utc_iso


UTC = timezone.utc


def _ratio(value: Any) -> AnalyticsRatioResponse:
    return AnalyticsRatioResponse(
        numerator=value.numerator,
        denominator=value.denominator,
        ratio=value.ratio,
    )


def _duration(value: Any) -> AnalyticsDurationSummaryResponse:
    return AnalyticsDurationSummaryResponse(
        count=value.count,
        median_ms=value.median_ms,
        p90_ms=value.p90_ms,
    )


def _response(
    value: AnalyticsOverview,
    *,
    source_id: str | None,
    service_id: int | None,
    signal_severity: Literal["critical", "warning", "info", "unknown"] | None,
) -> AnalyticsOverviewResponse:
    return AnalyticsOverviewResponse(
        freshness=value.freshness,
        generated_at=(
            None if value.generated_at is None else to_utc_iso(value.generated_at)
        ),
        range=value.window.range.value,
        from_utc=to_utc_iso(value.window.start),
        to_utc=to_utc_iso(value.window.end),
        source_id=source_id,
        service_id=service_id,
        signal_severity=signal_severity,
        response=AnalyticsResponseSection(
            mtta=AnalyticsDurationMetricResponse(
                completed=_duration(value.response.mtta.completed),
                unfinished=_duration(value.response.mtta.unfinished),
            ),
            resolution=AnalyticsDurationMetricResponse(
                completed=_duration(value.response.resolution.completed),
                unfinished=_duration(value.response.resolution.unfinished),
            ),
            task_outcomes=value.response.task_outcomes,
            resolution_codes=value.response.resolution_codes,
        ),
        signal=AnalyticsSignalSection(
            new_occurrences=value.signal.new_occurrences,
            new_alert_instances=value.signal.new_alert_instances,
            compression=_ratio(value.signal.compression),
            alerts_per_occurrence=_ratio(value.signal.alerts_per_occurrence),
            creation_unmapped=_ratio(value.signal.creation_unmapped),
            creation_assignment_unknown=value.signal.creation_assignment_unknown,
            current_unmapped=value.signal.current_unmapped,
            ack_sla_breach=_ratio(value.signal.ack_sla_breach),
            flapping_activations=value.signal.flapping_activations,
            storm_activations=value.signal.storm_activations,
        ),
        notifications={
            mode: AnalyticsNotificationModeResponse(
                succeeded=item.succeeded,
                permanently_failed=item.permanently_failed,
                pending_backlog=item.pending_backlog,
                noise=item.noise,
                success_rate=_ratio(item.success_rate),
                latency=_duration(item.latency),
            )
            for mode, item in value.notifications.items()
        },
        ai={
            mode: AnalyticsAiModeResponse(
                p2_valid=item.p2_valid,
                evidence_only=item.evidence_only,
                canceled=item.canceled,
                contract_rejected=item.contract_rejected,
                dependency_failed=item.dependency_failed,
                pending=item.pending,
                model_started=item.model_started,
                unknown_cost=item.unknown_cost,
                p2_success=_ratio(item.p2_success),
                feedback_response=_ratio(item.feedback_response),
                feedback_adoption=_ratio(item.feedback_adoption),
                p0_mtti=_duration(item.p0_mtti),
                p1_mtti=_duration(item.p1_mtti),
                p2_mtti=_duration(item.p2_mtti),
            )
            for mode, item in value.ai.items()
        },
    )


def create_analytics_router(
    *,
    store: SqlAlchemyAnalyticsStore,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    router = APIRouter(prefix="/api/v1")
    errors: dict[int | str, dict[str, Any]] = {
        422: {"model": ErrorEnvelope},
        500: {"model": ErrorEnvelope},
    }

    @router.get(
        "/analytics/overview",
        response_model=AnalyticsOverviewResponse,
        responses=errors,
    )
    async def overview(
        range_value: Annotated[
            Literal["24h", "7d", "30d"], Query(alias="range")
        ] = "7d",
        source_id: Annotated[str | None, Query(max_length=128)] = None,
        service_id: Annotated[int | None, Query(ge=1)] = None,
        signal_severity: Literal["critical", "warning", "info", "unknown"]
        | None = None,
    ) -> AnalyticsOverviewResponse:
        value = store.overview(
            range_value=range_value,
            source_id=source_id,
            service_id=service_id,
            signal_severity=signal_severity,
            now=now(),
        )
        return _response(
            value,
            source_id=source_id,
            service_id=service_id,
            signal_severity=signal_severity,
        )

    return router
