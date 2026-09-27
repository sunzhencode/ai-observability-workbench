"""Deterministic noise controls; these never write Alertmanager."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, cast

from fastapi import APIRouter, Header, status

from app.api.v1.idempotency import execute_idempotent_create
from app.api.v1.operator_witness import OperatorWitness
from app.api.v1.schemas import (
    ErrorEnvelope,
    MaintenanceEndInput,
    MaintenanceInput,
    MaintenanceResponse,
    OccurrenceNoiseResponse,
    SourceNoiseControlsInput,
    SourceNoiseControlsResponse,
    SuppressionEndInput,
    SuppressionInput,
    SuppressionResponse,
)
from app.application.commands import IdempotentCommands
from app.application.noise import (
    MaintenanceDraft,
    MaintenanceView,
    NoiseCommands,
    NoisePort,
    OccurrenceNoiseView,
    SourceNoiseControlsView,
    SuppressionView,
)
from app.platform.errors import SafeApiError
from app.platform.utc import to_utc_iso


def _controls(item: SourceNoiseControlsView) -> SourceNoiseControlsResponse:
    return SourceNoiseControlsResponse(
        source_id=item.source_id,
        flapping_enabled=item.flapping_enabled,
        storm_enabled=item.storm_enabled,
        storm_alert_threshold=item.storm_alert_threshold,
        storm_occurrence_threshold=item.storm_occurrence_threshold,
        storm_active=item.storm_active,
        storm_started_at=(
            None if item.storm_started_at is None else to_utc_iso(item.storm_started_at)
        ),
        version=item.version,
    )


def _maintenance(item: MaintenanceView) -> MaintenanceResponse:
    return MaintenanceResponse(
        id=item.id,
        scope_kind=cast(Any, item.scope_kind),
        source_id=item.source_id,
        service_id=item.service_id,
        aggregation_rule_id=item.aggregation_rule_id,
        starts_at=to_utc_iso(item.starts_at),
        ends_at=to_utc_iso(item.ends_at),
        reason=item.reason,
        status=cast(Any, item.status),
        ended_at=None if item.ended_at is None else to_utc_iso(item.ended_at),
        version=item.version,
        created_at=to_utc_iso(item.created_at),
    )


def _suppression(item: SuppressionView) -> SuppressionResponse:
    return SuppressionResponse(
        id=item.id,
        occurrence_id=item.occurrence_id,
        starts_at=to_utc_iso(item.starts_at),
        ends_at=to_utc_iso(item.ends_at),
        reason=item.reason,
        status=cast(Any, item.status),
        ended_at=None if item.ended_at is None else to_utc_iso(item.ended_at),
        version=item.version,
        created_at=to_utc_iso(item.created_at),
    )


def _noise(item: OccurrenceNoiseView) -> OccurrenceNoiseResponse:
    return OccurrenceNoiseResponse(
        state=cast(Any, item.state),
        reason=item.reason,
        scope=item.scope,
        starts_at=None if item.starts_at is None else to_utc_iso(item.starts_at),
        ends_at=None if item.ends_at is None else to_utc_iso(item.ends_at),
        suppression_id=item.suppression_id,
        suppression_version=item.suppression_version,
    )


def _safe(exc: Exception) -> SafeApiError:
    if isinstance(exc, LookupError):
        return SafeApiError(
            status_code=404,
            code=str(exc)[:96],
            message="未找到指定的来源、事件或降噪窗口",
        )
    if isinstance(exc, (FileExistsError, RuntimeError)):
        return SafeApiError(
            status_code=409,
            code=str(exc)[:96],
            message="降噪配置已变化或当前事件状态不允许该操作",
        )
    return SafeApiError(
        status_code=422,
        code=str(exc)[:96],
        message="降噪范围、时长或原因不符合约束",
    )


def create_noise_router(
    *, port: NoisePort, commands: IdempotentCommands
) -> APIRouter:
    router = APIRouter(prefix="/api/v1")
    errors: dict[int | str, dict[str, Any]] = {
        404: {"model": ErrorEnvelope},
        409: {"model": ErrorEnvelope},
        422: {"model": ErrorEnvelope},
    }
    noise_commands = NoiseCommands(port)

    @router.get(
        "/sources/{source_id}/noise-controls",
        response_model=SourceNoiseControlsResponse,
        responses=errors,
    )
    async def source_controls(source_id: str) -> SourceNoiseControlsResponse:
        try:
            return _controls(port.get_source_controls(source_id))
        except Exception as exc:
            raise _safe(exc) from exc

    @router.put(
        "/sources/{source_id}/noise-controls",
        response_model=SourceNoiseControlsResponse,
        responses=errors,
    )
    async def update_source_controls(
        source_id: str, payload: SourceNoiseControlsInput
    ) -> SourceNoiseControlsResponse:
        try:
            return _controls(
                port.update_source_controls(
                    source_id,
                    flapping_enabled=payload.flapping_enabled,
                    storm_enabled=payload.storm_enabled,
                    storm_alert_threshold=payload.storm_alert_threshold,
                    storm_occurrence_threshold=payload.storm_occurrence_threshold,
                    expected_version=payload.expected_version,
                    actor=OperatorWitness().actor(),
                    now=datetime.now(timezone.utc),
                )
            )
        except Exception as exc:
            raise _safe(exc) from exc

    @router.get(
        "/maintenance-windows",
        response_model=list[MaintenanceResponse],
        responses=errors,
    )
    async def maintenance_windows(include_ended: bool = False) -> list[MaintenanceResponse]:
        return [
            _maintenance(item)
            for item in port.list_maintenance(include_ended=include_ended)
        ]

    @router.post(
        "/maintenance-windows",
        response_model=MaintenanceResponse,
        status_code=status.HTTP_201_CREATED,
        responses=errors,
    )
    async def create_maintenance(
        payload: MaintenanceInput,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> MaintenanceResponse:
        try:
            rendered = execute_idempotent_create(
                commands,
                scope="maintenance-window.create",
                key=idempotency_key,
                payload=payload.model_dump(mode="json"),
                action=lambda: _maintenance(
                    noise_commands.create_maintenance(
                        MaintenanceDraft(
                            payload.scope_kind,
                            payload.source_id,
                            payload.service_id,
                            payload.aggregation_rule_id,
                            payload.starts_at,
                            payload.ends_at,
                            payload.reason,
                        ),
                        actor=OperatorWitness().actor(),
                        now=datetime.now(timezone.utc),
                    )
                ).model_dump(mode="json"),
            )
            return MaintenanceResponse.model_validate(rendered)
        except Exception as exc:
            raise _safe(exc) from exc

    @router.post(
        "/maintenance-windows/{maintenance_id}/end",
        response_model=MaintenanceResponse,
        responses=errors,
    )
    async def end_maintenance(
        maintenance_id: int, payload: MaintenanceEndInput
    ) -> MaintenanceResponse:
        try:
            return _maintenance(
                noise_commands.end_maintenance(
                    maintenance_id,
                    expected_version=payload.expected_version,
                    actor=OperatorWitness().actor(),
                    now=datetime.now(timezone.utc),
                )
            )
        except Exception as exc:
            raise _safe(exc) from exc

    @router.get(
        "/occurrences/{occurrence_id}/noise",
        response_model=OccurrenceNoiseResponse,
        responses=errors,
    )
    async def occurrence_noise(occurrence_id: int) -> OccurrenceNoiseResponse:
        try:
            return _noise(port.occurrence_noise(occurrence_id, now=datetime.now(timezone.utc)))
        except Exception as exc:
            raise _safe(exc) from exc

    @router.post(
        "/occurrences/{occurrence_id}/suppression",
        response_model=SuppressionResponse,
        status_code=status.HTTP_201_CREATED,
        responses=errors,
    )
    async def create_suppression(
        occurrence_id: int,
        payload: SuppressionInput,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> SuppressionResponse:
        try:
            rendered = execute_idempotent_create(
                commands,
                scope=f"occurrence-suppression.create:{occurrence_id}",
                key=idempotency_key,
                payload=payload.model_dump(mode="json"),
                action=lambda: _suppression(
                    noise_commands.create_suppression(
                        occurrence_id,
                        duration_seconds=payload.duration_seconds,
                        reason=payload.reason,
                        actor=OperatorWitness().actor(),
                        now=datetime.now(timezone.utc),
                    )
                ).model_dump(mode="json"),
            )
            return SuppressionResponse.model_validate(rendered)
        except Exception as exc:
            raise _safe(exc) from exc

    @router.post(
        "/occurrences/{occurrence_id}/suppression/end",
        response_model=SuppressionResponse,
        responses=errors,
    )
    async def end_suppression(
        occurrence_id: int, payload: SuppressionEndInput
    ) -> SuppressionResponse:
        try:
            return _suppression(
                noise_commands.end_suppression(
                    occurrence_id,
                    expected_version=payload.expected_version,
                    actor=OperatorWitness().actor(),
                    now=datetime.now(timezone.utc),
                )
            )
        except Exception as exc:
            raise _safe(exc) from exc

    return router
