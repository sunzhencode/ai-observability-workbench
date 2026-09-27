"""Prompt Profile revision, preview, test and activation routes."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone

from fastapi import APIRouter, status

from app.api.v1.observability_routes.common import (
    ObservabilityApiDependencies,
    error_responses,
    prompt_profile_response,
    safe_error,
)
from app.api.v1.schemas import (
    PromptGuidanceInput,
    PromptProfileActionInput,
    PromptProfileActivateInput,
    PromptProfileCopyInput,
    PromptProfilePreviewResponse,
    PromptProfileResponse,
    PromptProfileRevisionResponse,
    PromptProfileUpdateInput,
)
from app.domains.investigations.prompt_profiles import PromptGuidanceV1, preview_profile
from app.platform.errors import SafeApiError
from app.platform.utc import to_utc_iso


def register_prompt_profile_routes(
    router: APIRouter,
    dependencies: ObservabilityApiDependencies,
) -> None:
    port = dependencies.port
    errors = error_responses()

    @router.get(
        "/prompt-profiles",
        response_model=list[PromptProfileResponse],
        responses=errors,
    )
    async def list_prompt_profiles() -> list[PromptProfileResponse]:
        return [prompt_profile_response(value) for value in port.list_prompt_profiles()]

    @router.get(
        "/prompt-profiles/{profile_id}/revisions",
        response_model=list[PromptProfileRevisionResponse],
        responses=errors,
    )
    async def list_prompt_profile_revisions(
        profile_id: str,
    ) -> list[PromptProfileRevisionResponse]:
        try:
            values = port.list_prompt_profile_revisions(profile_id)
        except Exception as exc:
            raise safe_error(exc) from exc
        return [
            PromptProfileRevisionResponse(
                revision=value.revision,
                status=value.status,  # type: ignore[arg-type]
                guidance=PromptGuidanceInput(**asdict(value.guidance)),
                tested_at=to_utc_iso(value.tested_at) if value.tested_at else None,
                last_test_code=value.last_test_code,
                created_at=to_utc_iso(value.created_at) if value.created_at else None,
                changed_fields=list(value.changed_fields),  # type: ignore[arg-type]
            )
            for value in values
        ]

    @router.post(
        "/prompt-profiles/copy-standard",
        response_model=PromptProfileResponse,
        status_code=status.HTTP_201_CREATED,
        responses=errors,
    )
    async def copy_standard_profile(
        payload: PromptProfileCopyInput,
    ) -> PromptProfileResponse:
        try:
            return prompt_profile_response(
                port.copy_prompt_profile(
                    payload.name,
                    now=datetime.now(timezone.utc),
                )
            )
        except Exception as exc:
            raise safe_error(exc) from exc

    @router.put(
        "/prompt-profiles/{profile_id}/draft",
        response_model=PromptProfileResponse,
        responses=errors,
    )
    async def update_prompt_profile(
        profile_id: str,
        payload: PromptProfileUpdateInput,
    ) -> PromptProfileResponse:
        try:
            return prompt_profile_response(
                port.update_prompt_profile(
                    profile_id,
                    PromptGuidanceV1(
                        payload.organization_context,
                        payload.investigation_focus,
                        payload.terminology,
                        payload.response_style,
                    ),
                    expected_revision=payload.expected_revision,
                    now=datetime.now(timezone.utc),
                )
            )
        except Exception as exc:
            raise safe_error(exc) from exc

    @router.post(
        "/prompt-profiles/{profile_id}/preview",
        response_model=PromptProfilePreviewResponse,
        responses=errors,
    )
    async def preview_prompt_profile(
        profile_id: str,
        payload: PromptProfileActionInput,
    ) -> PromptProfilePreviewResponse:
        try:
            value = next(
                item
                for item in port.list_prompt_profiles()
                if item.id == profile_id and item.revision == payload.expected_revision
            )
        except StopIteration as exc:
            raise SafeApiError(
                status_code=409,
                code="PROMPT_PROFILE_REVISION_CONFLICT",
                message="提示配置已变更，请刷新后重试",
            ) from exc
        rendered = preview_profile(value)
        return PromptProfilePreviewResponse(
            profile_id=rendered.profile_id,
            revision=rendered.revision,
            safety_kernel=list(rendered.safety_kernel),
            operator_guidance=rendered.operator_guidance,
            egress_categories=list(rendered.egress_categories),
            estimated_tokens=rendered.estimated_tokens,
            maximum_cost="UNKNOWN",
        )

    @router.post(
        "/prompt-profiles/{profile_id}/test",
        response_model=PromptProfileResponse,
        responses=errors,
    )
    async def test_prompt_profile(
        profile_id: str,
        payload: PromptProfileActionInput,
    ) -> PromptProfileResponse:
        try:
            return prompt_profile_response(
                port.test_prompt_profile(
                    profile_id,
                    expected_revision=payload.expected_revision,
                    now=datetime.now(timezone.utc),
                )
            )
        except Exception as exc:
            raise safe_error(exc) from exc

    @router.post(
        "/prompt-profiles/{profile_id}/activate",
        response_model=PromptProfileResponse,
        responses=errors,
    )
    async def activate_prompt_profile(
        profile_id: str,
        payload: PromptProfileActivateInput,
    ) -> PromptProfileResponse:
        try:
            return prompt_profile_response(
                port.activate_prompt_profile(
                    profile_id,
                    expected_revision=payload.expected_revision,
                    global_default=payload.global_default,
                    service_ids=tuple(payload.service_ids),
                    now=datetime.now(timezone.utc),
                )
            )
        except Exception as exc:
            raise safe_error(exc) from exc


__all__ = ["register_prompt_profile_routes"]
