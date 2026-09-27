"""Metric template configuration and evidence-read routes."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Header, status

from app.api.v1.idempotency import execute_idempotent_create
from app.api.v1.observability_routes.common import (
    ObservabilityApiDependencies,
    error_responses,
    metric_template_response,
    safe_error,
)
from app.api.v1.schemas import (
    MetricEvidenceResponse,
    MetricTemplateInput,
    MetricTemplateResponse,
    MetricTemplateUpdateInput,
    VersionInput,
)
from app.application.observability import MetricTemplateDraft, ReadMetricEvidence
from app.platform.utc import to_utc_iso


def register_metric_template_routes(
    router: APIRouter,
    dependencies: ObservabilityApiDependencies,
) -> None:
    port = dependencies.port
    errors = error_responses()

    @router.get(
        "/metric-templates",
        response_model=list[MetricTemplateResponse],
        responses=errors,
    )
    async def list_templates() -> list[MetricTemplateResponse]:
        return [metric_template_response(item) for item in port.list_metric_templates()]

    @router.post(
        "/metric-templates",
        response_model=MetricTemplateResponse,
        status_code=status.HTTP_201_CREATED,
        responses=errors,
    )
    async def create_template(
        payload: MetricTemplateInput,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> MetricTemplateResponse:
        try:
            rendered = execute_idempotent_create(
                dependencies.commands,
                scope="metric-template.create",
                key=idempotency_key,
                payload=payload.model_dump(mode="json"),
                action=lambda: metric_template_response(
                    port.create_metric_template(
                        MetricTemplateDraft(
                            payload.name,
                            payload.promql,
                            payload.enabled,
                            payload.priority,
                            tuple(payload.source_ids),
                            payload.description,
                            tuple(payload.required_labels),
                            payload.legend_format,
                            payload.unit,
                        ),
                        origin_kind="MANUAL",
                        now=datetime.now(timezone.utc),
                    )
                ).model_dump(mode="json"),
            )
        except Exception as exc:
            raise safe_error(exc) from exc
        return MetricTemplateResponse.model_validate(rendered)

    @router.put(
        "/metric-templates/{template_id}",
        response_model=MetricTemplateResponse,
        responses=errors,
    )
    async def update_template(
        template_id: int,
        payload: MetricTemplateUpdateInput,
    ) -> MetricTemplateResponse:
        try:
            value = port.update_metric_template(
                template_id,
                MetricTemplateDraft(
                    payload.name,
                    payload.promql,
                    payload.enabled,
                    payload.priority,
                    tuple(payload.source_ids),
                    payload.description,
                    tuple(payload.required_labels),
                    payload.legend_format,
                    payload.unit,
                ),
                expected_version=payload.expected_version,
                now=datetime.now(timezone.utc),
            )
        except Exception as exc:
            raise safe_error(exc) from exc
        return metric_template_response(value)

    @router.delete(
        "/metric-templates/{template_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        responses=errors,
    )
    async def delete_template(template_id: int, payload: VersionInput) -> None:
        try:
            port.delete_metric_template(
                template_id,
                expected_version=payload.expected_version,
            )
        except Exception as exc:
            raise safe_error(exc) from exc

    @router.get(
        "/alerts/{alert_id}/metric-evidence",
        response_model=MetricEvidenceResponse,
        responses=errors,
    )
    async def metric_evidence(alert_id: int) -> MetricEvidenceResponse:
        try:
            context = port.get_alert_metric_context(alert_id)
        except Exception as exc:
            raise safe_error(exc) from exc
        try:
            configured = port.load_monitoring_secret(
                context.source_id,
                "THANOS",
                require_active=True,
            )
        except (LookupError, RuntimeError):
            end = context.last_seen_at
            start = context.starts_at or end - timedelta(hours=2)
            start = max(start, end - timedelta(hours=24))
            if start >= end:
                start = end - timedelta(minutes=5)
            return MetricEvidenceResponse(
                alert_id=alert_id,
                alert_starts_at=(
                    None if context.starts_at is None else to_utc_iso(context.starts_at)
                ),
                window_start=to_utc_iso(start),
                window_end=to_utc_iso(end),
                step_seconds=60,
                curves=[],
                failures=["SOURCE_UNAVAILABLE"],
            )
        try:
            value = await ReadMetricEvidence(port).execute(
                alert_id,
                dependencies.thanos_factory(configured.view.base_url, configured.secret),
            )
        except Exception as exc:
            raise safe_error(exc) from exc
        payload = asdict(value)
        payload["alert_starts_at"] = (
            None if value.alert_starts_at is None else to_utc_iso(value.alert_starts_at)
        )
        payload["window_start"] = to_utc_iso(value.window_start)
        payload["window_end"] = to_utc_iso(value.window_end)
        return MetricEvidenceResponse.model_validate(payload)


__all__ = ["register_metric_template_routes"]
