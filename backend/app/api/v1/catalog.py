"""Service Catalog and deterministic Service Mapping HTTP surface."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, cast

from fastapi import APIRouter, Header, status
from sqlalchemy.exc import IntegrityError

from app.api.v1.idempotency import execute_idempotent_create
from app.api.v1.schemas import (
    ErrorEnvelope,
    JobResponse,
    MatcherInput,
    ServiceArchiveInput,
    ServiceAuditResponse,
    ServiceInput,
    ServiceMappingPreviewResponse,
    ServiceMappingPreviewSampleResponse,
    ServiceMappingPublishInput,
    ServiceMappingPublishResponse,
    ServiceMappingRuleInput,
    ServiceMappingRuleResponse,
    ServiceMappingRuleUpdateInput,
    ServiceResponse,
    ServiceUpdateInput,
)
from app.application.catalog import (
    ServiceCatalogPort,
    ServiceMappingPreview,
    ServiceMappingRuleView,
    ServiceView,
)
from app.application.commands import CommandConflict, IdempotentCommands
from app.domains.alerting.models import Matcher, MatcherOperator
from app.domains.catalog.models import (
    ServiceCriticality,
    ServiceDraft,
    ServiceMappingRuleDraft,
)
from app.domains.operations.jobs import JobView
from app.platform.errors import SafeApiError
from app.platform.utc import to_utc_iso


def _errors() -> dict[int | str, dict[str, Any]]:
    return {
        404: {"model": ErrorEnvelope},
        409: {"model": ErrorEnvelope},
        422: {"model": ErrorEnvelope},
    }


def _service(item: ServiceView) -> ServiceResponse:
    return ServiceResponse(
        id=item.id,
        name=item.name,
        slug=item.slug,
        criticality=cast(Any, item.criticality),
        ack_sla_seconds=item.ack_sla_seconds,
        status=cast(Any, item.status),
        links=list(item.links),
        version=item.version,
        created_at=to_utc_iso(item.created_at),
        updated_at=to_utc_iso(item.updated_at),
    )


def _rule(item: ServiceMappingRuleView) -> ServiceMappingRuleResponse:
    return ServiceMappingRuleResponse(
        id=item.id,
        name=item.name,
        priority=item.priority,
        service_id=item.service_id,
        service_name=item.service_name,
        enabled=item.enabled,
        source_ids=list(item.source_ids),
        matchers=[
            MatcherInput(label=label, operator=cast(Any, operator), value=value)
            for label, operator, value in item.matchers
        ],
        version=item.version,
        published_version=item.published_version,
        published_at=(
            None if item.published_at is None else to_utc_iso(item.published_at)
        ),
        has_unpublished_changes=item.has_unpublished_changes,
        created_at=to_utc_iso(item.created_at),
        updated_at=to_utc_iso(item.updated_at),
    )


def _preview(item: ServiceMappingPreview) -> ServiceMappingPreviewResponse:
    return ServiceMappingPreviewResponse(
        rule_id=item.rule_id,
        matched_alert_count=item.matched_alert_count,
        mapped_occurrence_count=item.mapped_occurrence_count,
        ambiguous_occurrence_count=item.ambiguous_occurrence_count,
        unmapped_occurrence_count=item.unmapped_occurrence_count,
        samples=[
            ServiceMappingPreviewSampleResponse(
                occurrence_id=sample.occurrence_id,
                title=sample.title,
                assignment_state=cast(Any, sample.assignment_state),
                service_ids=list(sample.service_ids),
            )
            for sample in item.samples
        ],
    )


def _job(item: JobView) -> JobResponse:
    return JobResponse(
        id=item.id,
        kind=item.kind,
        pool=item.pool.value,
        subject_type=item.subject_type,
        subject_id=item.subject_id,
        state=item.state.value,
        payload_revision=item.payload_revision,
        attempt=item.attempt,
        available_at=to_utc_iso(item.available_at),
        started_at=None if item.started_at is None else to_utc_iso(item.started_at),
        finished_at=None if item.finished_at is None else to_utc_iso(item.finished_at),
        safe_error_code=item.safe_error_code,
        version=item.version,
        created_at=to_utc_iso(item.created_at),
        updated_at=to_utc_iso(item.updated_at),
    )


def _service_draft(payload: ServiceInput) -> ServiceDraft:
    return ServiceDraft(
        payload.name,
        payload.slug,
        ServiceCriticality(payload.criticality),
        tuple(payload.links),
    )


def _mapping_draft(payload: ServiceMappingRuleInput) -> ServiceMappingRuleDraft:
    return ServiceMappingRuleDraft(
        payload.name,
        payload.priority,
        payload.service_id,
        payload.enabled,
        tuple(payload.source_ids),
        tuple(
            Matcher(item.label, MatcherOperator(item.operator), item.value)
            for item in payload.matchers
        ),
    )


def _safe(exc: Exception) -> SafeApiError:
    if isinstance(exc, LookupError):
        return SafeApiError(
            status_code=404,
            code=str(exc)[:96],
            message="未找到指定服务或映射规则",
        )
    if isinstance(exc, (CommandConflict, FileExistsError, IntegrityError)):
        return SafeApiError(
            status_code=409,
            code="SERVICE_CATALOG_CONFLICT",
            message="服务目录已变更，或名称与现有对象重复，请刷新后重试",
        )
    code = str(exc)[:96]
    messages = {
        "SERVICE_ARCHIVED_READ_ONLY": "已归档服务只保留历史引用，不能继续编辑",
        "SERVICE_MAPPING_TARGET_ARCHIVED": "映射规则只能选择当前有效的服务",
        "SERVICE_MAPPING_NO_CHANGES": "当前草稿与已发布版本相同，无需重复发布",
        "SERVICE_LINK_HTTPS_REQUIRED": "服务链接必须是未内嵌凭证的 HTTPS 地址",
    }
    return SafeApiError(
        status_code=422,
        code=code if code in messages else "SERVICE_CATALOG_INPUT_INVALID",
        message=messages.get(code, "服务或映射规则字段不符合约束"),
    )


def create_catalog_router(
    *, port: ServiceCatalogPort, commands: IdempotentCommands
) -> APIRouter:
    router = APIRouter(prefix="/api/v1")
    errors = _errors()

    @router.get("/services", response_model=list[ServiceResponse], responses=errors)
    async def list_services(include_archived: bool = False) -> list[ServiceResponse]:
        return [
            _service(item)
            for item in port.list_services(include_archived=include_archived)
        ]

    @router.post(
        "/services",
        response_model=ServiceResponse,
        status_code=status.HTTP_201_CREATED,
        responses=errors,
    )
    async def create_service(
        payload: ServiceInput,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> ServiceResponse:
        try:
            rendered = execute_idempotent_create(
                commands,
                scope="service.create",
                key=idempotency_key,
                payload=payload.model_dump(mode="json"),
                action=lambda: _service(
                    port.create_service(
                        _service_draft(payload), now=datetime.now(timezone.utc)
                    )
                ).model_dump(mode="json"),
            )
            return ServiceResponse.model_validate(rendered)
        except Exception as exc:
            raise _safe(exc) from exc

    @router.put(
        "/services/{service_id}", response_model=ServiceResponse, responses=errors
    )
    async def update_service(
        service_id: int, payload: ServiceUpdateInput
    ) -> ServiceResponse:
        try:
            return _service(
                port.update_service(
                    service_id,
                    _service_draft(payload),
                    expected_version=payload.expected_version,
                    now=datetime.now(timezone.utc),
                )
            )
        except Exception as exc:
            raise _safe(exc) from exc

    @router.post(
        "/services/{service_id}/archive",
        response_model=ServiceResponse,
        responses=errors,
    )
    async def archive_service(
        service_id: int, payload: ServiceArchiveInput
    ) -> ServiceResponse:
        try:
            return _service(
                port.archive_service(
                    service_id,
                    expected_version=payload.expected_version,
                    now=datetime.now(timezone.utc),
                )
            )
        except Exception as exc:
            raise _safe(exc) from exc

    @router.get(
        "/services/{service_id}/audit",
        response_model=list[ServiceAuditResponse],
        responses=errors,
    )
    async def service_audit(service_id: int) -> list[ServiceAuditResponse]:
        try:
            return [
                ServiceAuditResponse(
                    id=item.id,
                    service_id=item.service_id,
                    action=cast(Any, item.action),
                    service_version=item.service_version,
                    detail=item.detail,
                    changed_at=to_utc_iso(item.changed_at),
                )
                for item in port.service_audit(service_id)
            ]
        except Exception as exc:
            raise _safe(exc) from exc

    @router.get(
        "/service-mapping-rules",
        response_model=list[ServiceMappingRuleResponse],
        responses=errors,
    )
    async def list_mapping_rules() -> list[ServiceMappingRuleResponse]:
        return [_rule(item) for item in port.list_mapping_rules()]

    @router.post(
        "/service-mapping-rules",
        response_model=ServiceMappingRuleResponse,
        status_code=status.HTTP_201_CREATED,
        responses=errors,
    )
    async def create_mapping_rule(
        payload: ServiceMappingRuleInput,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> ServiceMappingRuleResponse:
        try:
            rendered = execute_idempotent_create(
                commands,
                scope="service-mapping-rule.create",
                key=idempotency_key,
                payload=payload.model_dump(mode="json"),
                action=lambda: _rule(
                    port.create_mapping_rule(
                        _mapping_draft(payload), now=datetime.now(timezone.utc)
                    )
                ).model_dump(mode="json"),
            )
            return ServiceMappingRuleResponse.model_validate(rendered)
        except Exception as exc:
            raise _safe(exc) from exc

    @router.put(
        "/service-mapping-rules/{rule_id}",
        response_model=ServiceMappingRuleResponse,
        responses=errors,
    )
    async def update_mapping_rule(
        rule_id: int, payload: ServiceMappingRuleUpdateInput
    ) -> ServiceMappingRuleResponse:
        try:
            return _rule(
                port.update_mapping_rule(
                    rule_id,
                    _mapping_draft(payload),
                    expected_version=payload.expected_version,
                    now=datetime.now(timezone.utc),
                )
            )
        except Exception as exc:
            raise _safe(exc) from exc

    @router.post(
        "/service-mapping-rules/{rule_id}/preview",
        response_model=ServiceMappingPreviewResponse,
        responses=errors,
    )
    async def preview_mapping_rule(rule_id: int) -> ServiceMappingPreviewResponse:
        try:
            return _preview(port.preview_mapping_rule(rule_id))
        except Exception as exc:
            raise _safe(exc) from exc

    @router.post(
        "/service-mapping-rules/{rule_id}/publish",
        response_model=ServiceMappingPublishResponse,
        status_code=status.HTTP_202_ACCEPTED,
        responses=errors,
    )
    async def publish_mapping_rule(
        rule_id: int, payload: ServiceMappingPublishInput
    ) -> ServiceMappingPublishResponse:
        try:
            result = port.publish_mapping_rule(
                rule_id,
                expected_version=payload.expected_version,
                now=datetime.now(timezone.utc),
            )
            return ServiceMappingPublishResponse(
                rule=_rule(result.rule), reprojection_job=_job(result.reprojection_job)
            )
        except Exception as exc:
            raise _safe(exc) from exc

    return router
