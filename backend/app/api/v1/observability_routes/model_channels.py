"""Model provider catalog and channel lifecycle routes."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Header, status

from app.api.v1.idempotency import execute_idempotent_create, execute_idempotent_external
from app.api.v1.observability_routes.common import (
    ObservabilityApiDependencies,
    error_responses,
    model_channel_response,
    safe_error,
)
from app.api.v1.schemas import (
    ModelChannelActionInput,
    ModelChannelInputSchema,
    ModelChannelResponse,
    ModelChannelUpdateInput,
    ModelListResponse,
    ModelRuntimeResponse,
    ModelVendorResponse,
)
from app.application.observability import ModelChannelInput
from app.domains.investigations.provider_catalog import PROVIDER_CATALOG
from app.platform.errors import SafeApiError


def register_model_channel_routes(
    router: APIRouter,
    dependencies: ObservabilityApiDependencies,
) -> None:
    port = dependencies.port
    errors = error_responses()

    @router.get("/model-vendors", response_model=list[ModelVendorResponse])
    async def model_vendors() -> list[ModelVendorResponse]:
        return [
            ModelVendorResponse(
                id=item.provider_id.value,
                label=item.label,
                base_url=item.base_url,
                key_hint=item.key_hint,
                recommended_models=list(item.recommended_models),
                protocol_profile=item.protocol.value,
                support_level=item.support_level.value,
            )
            for item in PROVIDER_CATALOG.values()
        ]

    @router.get("/model-runtime", response_model=ModelRuntimeResponse)
    async def model_runtime() -> ModelRuntimeResponse:
        active = port.active_model_channel()
        return ModelRuntimeResponse(
            configured=active is not None,
            channel_id=None if active is None else active.id,
            model=None if active is None else active.model,
            fake_mode=dependencies.model_fake_mode,
        )

    @router.get(
        "/model-channels",
        response_model=list[ModelChannelResponse],
        responses=errors,
    )
    async def list_channels() -> list[ModelChannelResponse]:
        return [model_channel_response(item) for item in port.list_model_channels()]

    @router.post(
        "/model-channels",
        response_model=ModelChannelResponse,
        status_code=status.HTTP_201_CREATED,
        responses=errors,
    )
    async def create_channel(
        payload: ModelChannelInputSchema,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> ModelChannelResponse:
        try:
            rendered = execute_idempotent_create(
                dependencies.commands,
                scope="model-channel.create",
                key=idempotency_key,
                payload=payload.model_dump(mode="json"),
                action=lambda: model_channel_response(
                    port.create_model_channel(
                        ModelChannelInput(
                            payload.name,
                            payload.kind,
                            payload.base_url,
                            payload.model,
                            payload.api_key.action,
                            payload.api_key.value,
                            payload.provider_id,
                        ),
                        now=datetime.now(timezone.utc),
                    )
                ).model_dump(mode="json"),
            )
        except Exception as exc:
            raise safe_error(exc) from exc
        return ModelChannelResponse.model_validate(rendered)

    @router.put(
        "/model-channels/{channel_id}/draft",
        response_model=ModelChannelResponse,
        responses=errors,
    )
    async def update_channel(
        channel_id: str,
        payload: ModelChannelUpdateInput,
    ) -> ModelChannelResponse:
        try:
            value = port.update_model_channel(
                channel_id,
                ModelChannelInput(
                    payload.name,
                    payload.kind,
                    payload.base_url,
                    payload.model,
                    payload.api_key.action,
                    payload.api_key.value,
                    payload.provider_id,
                ),
                expected_revision=payload.expected_revision,
                now=datetime.now(timezone.utc),
            )
        except Exception as exc:
            raise safe_error(exc) from exc
        return model_channel_response(value)

    @router.post(
        "/model-channels/{channel_id}/test",
        response_model=ModelChannelResponse,
        responses=errors,
    )
    async def test_channel(
        channel_id: str,
        payload: ModelChannelActionInput,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> ModelChannelResponse:
        async def action() -> dict[str, Any]:
            secret = port.load_model_secret(channel_id)
            profile = secret.provider_profile
            if profile is None:
                raise RuntimeError("MODEL_PROVIDER_PROFILE_REQUIRED")
            result = await dependencies.model_probe.test(
                profile=profile,
                api_key=secret.api_key,
            )
            value = port.record_model_test(
                channel_id,
                expected_revision=payload.expected_revision,
                ok=bool(result.ok),
                safe_error_code=str(result.safe_error_code),
                now=datetime.now(timezone.utc),
            )
            return model_channel_response(value).model_dump(mode="json")

        try:
            rendered = await execute_idempotent_external(
                dependencies.commands,
                scope="model-channel.test",
                key=idempotency_key,
                payload={"channel_id": channel_id, **payload.model_dump(mode="json")},
                action=action,
            )
        except Exception as exc:
            raise safe_error(exc) from exc
        return ModelChannelResponse.model_validate(rendered)

    @router.post(
        "/model-channels/{channel_id}/models",
        response_model=ModelListResponse,
        responses=errors,
    )
    async def list_channel_models(channel_id: str) -> ModelListResponse:
        try:
            secret = port.load_model_secret(channel_id)
            models = await dependencies.model_probe.list_models(
                base_url=secret.view.base_url,
                api_key=secret.api_key,
            )
        except Exception as exc:
            raise SafeApiError(
                status_code=409,
                code="MODEL_LIST_UNAVAILABLE",
                message="模型目录读取未完成，请检查地址、凭证和网络",
            ) from exc
        return ModelListResponse(models=list(models))

    @router.post(
        "/model-channels/{channel_id}/activate",
        response_model=ModelChannelResponse,
        responses=errors,
    )
    async def activate_channel(
        channel_id: str,
        payload: ModelChannelActionInput,
    ) -> ModelChannelResponse:
        try:
            value = port.activate_model_channel(
                channel_id,
                expected_revision=payload.expected_revision,
                now=datetime.now(timezone.utc),
            )
        except Exception as exc:
            raise safe_error(exc) from exc
        return model_channel_response(value)

    @router.post(
        "/model-channels/{channel_id}/enable",
        response_model=ModelChannelResponse,
        responses=errors,
    )
    async def enable_channel(channel_id: str) -> ModelChannelResponse:
        try:
            return model_channel_response(
                port.set_model_channel_enabled(
                    channel_id,
                    enabled=True,
                    now=datetime.now(timezone.utc),
                )
            )
        except Exception as exc:
            raise safe_error(exc) from exc

    @router.post(
        "/model-channels/{channel_id}/disable",
        response_model=ModelChannelResponse,
        responses=errors,
    )
    async def disable_channel(channel_id: str) -> ModelChannelResponse:
        try:
            return model_channel_response(
                port.set_model_channel_enabled(
                    channel_id,
                    enabled=False,
                    now=datetime.now(timezone.utc),
                )
            )
        except Exception as exc:
            raise safe_error(exc) from exc


__all__ = ["register_model_channel_routes"]
