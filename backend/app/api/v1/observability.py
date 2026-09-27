"""Composition root for metrics, monitoring and model configuration routes."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.observability_routes.common import (
    GrafanaReaderFactory,
    ModelProbe,
    ObservabilityApiDependencies,
    ThanosReaderFactory,
)
from app.api.v1.observability_routes.metric_templates import (
    register_metric_template_routes,
)
from app.api.v1.observability_routes.model_channels import register_model_channel_routes
from app.api.v1.observability_routes.monitoring import register_monitoring_routes
from app.api.v1.observability_routes.prompt_profiles import (
    register_prompt_profile_routes,
)
from app.application.commands import IdempotentCommands
from app.application.observability import ObservabilityPort


def create_observability_router(
    *,
    port: ObservabilityPort,
    thanos_factory: ThanosReaderFactory,
    grafana_factory: GrafanaReaderFactory,
    model_probe: ModelProbe,
    model_fake_mode: bool,
    commands: IdempotentCommands,
) -> APIRouter:
    """Compose narrow subdomain routers under the stable public API prefix."""
    router = APIRouter(prefix="/api/v1")
    dependencies = ObservabilityApiDependencies(
        port=port,
        thanos_factory=thanos_factory,
        grafana_factory=grafana_factory,
        model_probe=model_probe,
        model_fake_mode=model_fake_mode,
        commands=commands,
    )
    register_monitoring_routes(router, dependencies)
    register_metric_template_routes(router, dependencies)
    register_model_channel_routes(router, dependencies)
    register_prompt_profile_routes(router, dependencies)
    return router


__all__ = ["create_observability_router"]
