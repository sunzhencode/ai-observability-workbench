"""Shared transport-only dependencies and response projection helpers."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Protocol

from app.api.v1.schemas import (
    ErrorEnvelope,
    MetricTemplateResponse,
    ModelChannelResponse,
    MonitoringConnectionResponse,
    PromptGuidanceInput,
    PromptProfileResponse,
)
from app.application.commands import IdempotentCommands
from app.application.observability import (
    MetricTemplateView,
    ModelChannelView,
    MonitoringConnectionSecret,
    MonitoringConnectionView,
    ObservabilityPort,
)
from app.domains.investigations.prompt_profiles import PromptProfileView
from app.domains.investigations.runtime import ProviderProfile
from app.platform.errors import SafeApiError
from app.platform.utc import to_utc_iso


class ThanosReaderFactory(Protocol):
    def __call__(self, base_url: str, secret: str) -> Any: ...


class GrafanaReaderFactory(Protocol):
    def __call__(self, base_url: str, secret: str) -> Any: ...


class ModelProbe(Protocol):
    async def test(self, *, profile: ProviderProfile, api_key: str) -> Any: ...
    async def list_models(self, *, base_url: str, api_key: str) -> tuple[str, ...]: ...


@dataclass(frozen=True, slots=True)
class ObservabilityApiDependencies:
    port: ObservabilityPort
    thanos_factory: ThanosReaderFactory
    grafana_factory: GrafanaReaderFactory
    model_probe: ModelProbe
    model_fake_mode: bool
    commands: IdempotentCommands


def error_responses() -> dict[int | str, dict[str, Any]]:
    return {
        400: {"model": ErrorEnvelope},
        404: {"model": ErrorEnvelope},
        409: {"model": ErrorEnvelope},
        422: {"model": ErrorEnvelope},
        500: {"model": ErrorEnvelope},
    }


def connection_response(value: MonitoringConnectionView) -> MonitoringConnectionResponse:
    return MonitoringConnectionResponse(
        **{
            **asdict(value),
            "tested_at": to_utc_iso(value.tested_at) if value.tested_at else None,
        }
    )


def metric_template_response(value: MetricTemplateView) -> MetricTemplateResponse:
    return MetricTemplateResponse(**asdict(value))


def model_channel_response(value: ModelChannelView) -> ModelChannelResponse:
    return ModelChannelResponse(
        **{
            **asdict(value),
            "tested_at": to_utc_iso(value.tested_at) if value.tested_at else None,
        }
    )


def prompt_profile_response(value: PromptProfileView) -> PromptProfileResponse:
    return PromptProfileResponse(
        id=value.id,
        name=value.name,
        builtin=value.builtin,
        status=value.status,  # type: ignore[arg-type]
        revision=value.revision,
        active_revision=value.active_revision,
        guidance=PromptGuidanceInput(**asdict(value.guidance)),
        is_global_default=value.is_global_default,
        service_ids=list(value.service_ids),
        tested_at=to_utc_iso(value.tested_at) if value.tested_at else None,
        last_test_code=value.last_test_code,
    )


def safe_error(exc: Exception) -> SafeApiError:
    if isinstance(exc, SafeApiError):
        return exc
    if isinstance(exc, LookupError):
        return SafeApiError(
            status_code=404,
            code="RESOURCE_NOT_FOUND",
            message="未找到指定配置对象",
        )
    if isinstance(exc, FileExistsError):
        return SafeApiError(
            status_code=409,
            code="VERSION_CONFLICT",
            message="配置已变更，请刷新后重试",
        )
    if isinstance(exc, RuntimeError):
        return SafeApiError(
            status_code=409,
            code=str(exc)[:96],
            message="配置当前状态不允许该操作",
        )
    return SafeApiError(
        status_code=422,
        code="CONFIGURATION_INVALID",
        message="配置字段或范围不符合要求",
    )


def monitoring_secret(
    port: ObservabilityPort,
    source_id: str,
    kind: str,
    *,
    active: bool = False,
) -> MonitoringConnectionSecret:
    try:
        return port.load_monitoring_secret(source_id, kind, require_active=active)
    except Exception as exc:
        raise safe_error(exc) from exc


__all__ = [
    "GrafanaReaderFactory",
    "ModelProbe",
    "ObservabilityApiDependencies",
    "ThanosReaderFactory",
    "connection_response",
    "error_responses",
    "metric_template_response",
    "model_channel_response",
    "monitoring_secret",
    "prompt_profile_response",
    "safe_error",
]
