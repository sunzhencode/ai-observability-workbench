"""Source and alerting HTTP adapter."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import re
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Header, Query, status
from sqlalchemy.exc import IntegrityError

from app.api.v1.schemas import (
    AggregationLabelCatalogResponse,
    AggregationLabelResponse,
    AlertResponse,
    CollectSourceInput,
    ErrorEnvelope,
    EndpointTestResponse,
    ExpectedWatchdogClusterInput,
    IncidentResponse,
    JobAcceptedResponse,
    MatcherInput,
    RuleInput,
    RulePreviewInput,
    RulePreviewResponse,
    RuleResponse,
    RuleUpdateInput,
    SourceConfigInput,
    SourceLifecycleInput,
    SourceResponse,
    SourceUpdateInput,
    SourceTestResponse,
    SourceAuditResponse,
    WatchdogInventoryInput,
    WatchdogClusterResponse,
)
from app.application.jobs import JobQueue
from app.application.commands import IdempotentCommands
from app.api.v1.idempotency import execute_idempotent_create
from app.application.sources import (
    EndpointDraft,
    SourceDraft,
    SourceSlicePort,
    SourceView,
    TestSource,
    new_source_id,
)
from app.domains.sources.models import SourceState
from app.domains.operations.jobs import JobPool, JobSpec
from app.platform.errors import SafeApiError
from app.platform.utc import to_utc_iso

HistoricalLabelStatus = Literal["ok", "unconfigured", "error"]


def _source(value: SourceView) -> SourceResponse:
    payload = asdict(value)
    payload["created_at"] = to_utc_iso(value.created_at)
    payload["updated_at"] = to_utc_iso(value.updated_at)
    payload["last_poll_at"] = (
        None if value.last_poll_at is None else to_utc_iso(value.last_poll_at)
    )
    return SourceResponse(**payload)


def _incident(value: Any) -> IncidentResponse:
    payload = asdict(value)
    payload["occurrence_started_at"] = to_utc_iso(value.occurrence_started_at)
    payload["updated_at"] = to_utc_iso(value.updated_at)
    return IncidentResponse(**payload)


def _alert(value: Any) -> AlertResponse:
    payload = asdict(value)
    payload["starts_at"] = None if value.starts_at is None else to_utc_iso(value.starts_at)
    payload["missing_since_at"] = (
        None if value.missing_since_at is None else to_utc_iso(value.missing_since_at)
    )
    payload["last_seen_at"] = to_utc_iso(value.last_seen_at)
    return AlertResponse(**payload)


def _rule(value: Any) -> RuleResponse:
    return RuleResponse(
        id=value.id,
        name=value.name,
        priority=value.priority,
        enabled=value.enabled,
        version=value.version,
        matchers=[
            MatcherInput(label=label, operator=operator, value=matcher_value)
            for label, operator, matcher_value in value.matchers
        ],
        group_by_labels=list(value.group_by_labels),
        source_ids=list(value.source_ids),
        grouping_window_seconds=value.grouping_window_seconds,
        created_at=to_utc_iso(value.created_at),
        updated_at=to_utc_iso(value.updated_at),
    )


def _matchers(payload: RuleInput) -> tuple[tuple[str, str, str], ...]:
    return tuple((item.label, item.operator, item.value) for item in payload.matchers)


def _draft(source_id: str, payload: SourceConfigInput) -> SourceDraft:
    return SourceDraft(
        id=source_id,
        name=payload.name,
        endpoints=tuple(
            EndpointDraft(
                position=item.position,
                canonical_url=item.url,
                enabled=item.enabled,
                auth_kind=item.auth_kind,
                username=item.username,
                secret_action=item.secret.action,
                secret_value=item.secret.value,
                timeout_seconds=item.timeout_seconds,
            )
            for item in payload.endpoints
        ),
        poll_interval_seconds=payload.poll_interval_seconds,
        resolution_grace_seconds=payload.resolution_grace_seconds,
        max_parallel_endpoints=payload.max_parallel_endpoints,
        watchdog_enabled=payload.watchdog_enabled,
        watchdog_alertname=payload.watchdog_alertname,
        watchdog_identity_label=payload.watchdog_identity_label,
        watchdog_missing_after_seconds=payload.watchdog_missing_after_seconds,
    )


def create_sources_router(
    *,
    port: SourceSlicePort,
    jobs: JobQueue,
    tester: TestSource,
    commands: IdempotentCommands,
    historical_label_reader: Callable[
        [int],
        Awaitable[
            tuple[tuple[dict[str, str], ...], HistoricalLabelStatus, str | None]
        ],
    ]
    | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1")
    errors: dict[int | str, dict[str, Any]] = {
        400: {"model": ErrorEnvelope},
        404: {"model": ErrorEnvelope},
        409: {"model": ErrorEnvelope},
        422: {"model": ErrorEnvelope},
        500: {"model": ErrorEnvelope},
    }

    @router.get("/sources", response_model=list[SourceResponse], responses=errors)
    async def list_sources(
        include_archived: bool = False,
    ) -> list[SourceResponse]:
        return [
            _source(item)
            for item in port.list_sources(include_archived=include_archived)
        ]

    @router.post(
        "/sources",
        response_model=SourceResponse,
        status_code=status.HTTP_201_CREATED,
        responses=errors,
    )
    async def create_source(
        payload: SourceConfigInput,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> SourceResponse:
        try:
            rendered = execute_idempotent_create(
                commands,
                scope="source.create",
                key=idempotency_key,
                payload=payload.model_dump(mode="json"),
                action=lambda: _source(
                    port.create_source(
                        _draft(new_source_id(), payload),
                        now=datetime.now(timezone.utc),
                    )
                ).model_dump(mode="json"),
            )
        except IntegrityError as exc:
            raise SafeApiError(
                status_code=409,
                code="SOURCE_CONFIGURATION_CONFLICT",
                message="监控来源名称或 Endpoint 已存在",
            ) from exc
        except ValueError as exc:
            raise SafeApiError(
                status_code=422,
                code="SOURCE_CONFIGURATION_INVALID",
                message="监控来源配置无效",
            ) from exc
        return SourceResponse.model_validate(rendered)

    @router.get(
        "/sources/{source_id}", response_model=SourceResponse, responses=errors
    )
    async def get_source(source_id: str) -> SourceResponse:
        item = port.get_source(source_id)
        if item is None:
            raise SafeApiError(
                status_code=404,
                code="SOURCE_NOT_FOUND",
                message="未找到指定监控来源",
            )
        return _source(item)

    @router.put(
        "/sources/{source_id}", response_model=SourceResponse, responses=errors
    )
    async def update_source(
        source_id: str,
        payload: SourceUpdateInput,
    ) -> SourceResponse:
        try:
            updated = port.update_source(
                source_id,
                _draft(source_id, payload),
                expected_version=payload.expected_version,
                now=datetime.now(timezone.utc),
            )
        except LookupError as exc:
            raise SafeApiError(
                status_code=404,
                code="SOURCE_NOT_FOUND",
                message="未找到指定监控来源",
            ) from exc
        except FileExistsError as exc:
            raise SafeApiError(
                status_code=409,
                code="SOURCE_VERSION_CONFLICT",
                message="监控来源配置已变更",
            ) from exc
        except IntegrityError as exc:
            raise SafeApiError(
                status_code=409,
                code="SOURCE_CONFIGURATION_CONFLICT",
                message="监控来源名称或 Endpoint 已存在",
            ) from exc
        except ValueError as exc:
            raise SafeApiError(
                status_code=422,
                code="SOURCE_CONFIGURATION_INVALID",
                message="监控来源配置无效",
            ) from exc
        return _source(updated)

    def lifecycle(
        source_id: str,
        payload: SourceLifecycleInput,
        target: SourceState,
    ) -> SourceResponse:
        try:
            value = port.set_source_state(
                source_id,
                target=target,
                expected_version=payload.expected_version,
                now=datetime.now(timezone.utc),
            )
        except LookupError as exc:
            raise SafeApiError(
                status_code=404,
                code="SOURCE_NOT_FOUND",
                message="未找到指定监控来源",
            ) from exc
        except FileExistsError as exc:
            raise SafeApiError(
                status_code=409,
                code="SOURCE_VERSION_CONFLICT",
                message="监控来源配置已变更",
            ) from exc
        except RuntimeError as exc:
            raise SafeApiError(
                status_code=409,
                code="SOURCE_LIFECYCLE_CONFLICT",
                message="监控来源当前状态不允许该操作",
            ) from exc
        return _source(value)

    @router.post("/sources/{source_id}/enable", response_model=SourceResponse, responses=errors)
    async def enable_source(source_id: str, payload: SourceLifecycleInput) -> SourceResponse:
        return lifecycle(source_id, payload, SourceState.ENABLED)

    @router.post("/sources/{source_id}/disable", response_model=SourceResponse, responses=errors)
    async def disable_source(source_id: str, payload: SourceLifecycleInput) -> SourceResponse:
        return lifecycle(source_id, payload, SourceState.DISABLED)

    @router.post("/sources/{source_id}/archive", response_model=SourceResponse, responses=errors)
    async def archive_source(source_id: str, payload: SourceLifecycleInput) -> SourceResponse:
        return lifecycle(source_id, payload, SourceState.ARCHIVED)

    @router.get(
        "/sources/{source_id}/audit",
        response_model=list[SourceAuditResponse],
        responses=errors,
    )
    async def source_audit(source_id: str) -> list[SourceAuditResponse]:
        try:
            values = port.list_source_audit(source_id)
        except LookupError as exc:
            raise SafeApiError(
                status_code=404,
                code="SOURCE_NOT_FOUND",
                message="未找到指定监控来源",
            ) from exc
        return [
            SourceAuditResponse(
                sequence=item.sequence,
                source_id=item.source_id,
                action=item.action,
                source_version=item.source_version,
                changed_at=to_utc_iso(item.changed_at),
            )
            for item in values
        ]

    @router.get(
        "/sources/{source_id}/watchdog-clusters",
        response_model=list[WatchdogClusterResponse],
        responses=errors,
    )
    async def watchdog_clusters(source_id: str) -> list[WatchdogClusterResponse]:
        try:
            values = port.list_watchdog_clusters(
                source_id,
                now=datetime.now(timezone.utc),
            )
        except LookupError as exc:
            raise SafeApiError(
                status_code=404,
                code="SOURCE_NOT_FOUND",
                message="未找到指定监控来源",
            ) from exc
        return [
            WatchdogClusterResponse(
                id=item.id,
                source_id=item.source_id,
                identity_value=item.identity_value,
                inventory_state=item.inventory_state,
                health_state=item.health_state,
                last_observed_at=(
                    None
                    if item.last_observed_at is None
                    else to_utc_iso(item.last_observed_at)
                ),
            )
            for item in values
        ]

    @router.post(
        "/sources/{source_id}/watchdog-clusters",
        status_code=status.HTTP_204_NO_CONTENT,
        responses=errors,
    )
    async def add_watchdog_cluster(
        source_id: str,
        payload: ExpectedWatchdogClusterInput,
    ) -> None:
        try:
            port.add_expected_watchdog_cluster(
                source_id,
                payload.identity_value,
                now=datetime.now(timezone.utc),
            )
        except LookupError as exc:
            raise SafeApiError(
                status_code=404,
                code="SOURCE_NOT_FOUND",
                message="未找到指定监控来源",
            ) from exc

    @router.put(
        "/sources/{source_id}/watchdog-clusters/{identity_value}",
        status_code=status.HTTP_204_NO_CONTENT,
        responses=errors,
    )
    async def update_watchdog_cluster(
        source_id: str,
        identity_value: str,
        payload: WatchdogInventoryInput,
    ) -> None:
        try:
            port.set_watchdog_inventory_state(
                source_id,
                identity_value,
                inventory_state=payload.inventory_state,
            )
        except LookupError as exc:
            raise SafeApiError(
                status_code=404,
                code="WATCHDOG_CLUSTER_NOT_FOUND",
                message="未找到指定 Watchdog cluster",
            ) from exc

    @router.post(
        "/sources/{source_id}/collect",
        response_model=JobAcceptedResponse,
        status_code=status.HTTP_202_ACCEPTED,
        responses=errors,
    )
    async def collect_source(
        source_id: str,
        payload: CollectSourceInput,
        idempotency_key: Annotated[
            str,
            Header(alias="Idempotency-Key", min_length=8, max_length=128),
        ],
    ) -> JobAcceptedResponse:
        source = port.get_source(source_id)
        if source is None:
            raise SafeApiError(
                status_code=404,
                code="SOURCE_NOT_FOUND",
                message="未找到指定监控来源",
            )
        if source.state != "ENABLED":
            raise SafeApiError(
                status_code=409,
                code="SOURCE_NOT_POLLABLE",
                message="监控来源当前未启用",
            )
        if source.version != payload.expected_version:
            raise SafeApiError(
                status_code=409,
                code="SOURCE_VERSION_CONFLICT",
                message="监控来源配置已变更",
            )
        job = jobs.enqueue(
            JobSpec(
                kind="source.collect",
                pool=JobPool.SOURCE,
                subject_type="source",
                subject_id=source_id,
                payload={"expected_version": payload.expected_version},
                payload_revision=1,
                idempotency_key=idempotency_key,
            )
        )
        return JobAcceptedResponse(job_id=job.id, state=job.state.value)

    @router.post(
        "/sources/{source_id}/test",
        response_model=SourceTestResponse,
        responses=errors,
    )
    async def test_source(
        source_id: str,
        payload: SourceLifecycleInput,
    ) -> SourceTestResponse:
        try:
            result = await tester.execute(source_id, payload.expected_version)
        except LookupError as exc:
            raise SafeApiError(
                status_code=404,
                code="SOURCE_NOT_FOUND",
                message="未找到指定监控来源",
            ) from exc
        except FileExistsError as exc:
            raise SafeApiError(
                status_code=409,
                code="SOURCE_VERSION_CONFLICT",
                message="监控来源配置已变更",
            ) from exc
        return SourceTestResponse(
            ok=result.completeness.value == "COMPLETE",
            completeness=result.completeness.value,
            safe_error_codes=list(result.safe_error_codes),
            endpoints=[
                EndpointTestResponse(
                    position=item.endpoint.position,
                    ok=item.succeeded,
                    status=item.status,
                    safe_error_code=item.safe_error_code,
                    duration_ms=item.duration_ms,
                )
                for item in result.endpoint_observations
            ],
        )

    @router.get("/alerts", response_model=list[AlertResponse], responses=errors)
    async def list_alerts(
        source_id: Annotated[str | None, Query(max_length=128)] = None,
    ) -> list[AlertResponse]:
        return [
            _alert(item)
            for item in port.list_alerts(source_id=source_id)
        ]

    @router.get("/incidents", response_model=list[IncidentResponse], responses=errors)
    async def list_incidents(
        source_id: Annotated[str | None, Query(max_length=128)] = None,
    ) -> list[IncidentResponse]:
        return [_incident(item) for item in port.list_incidents(source_id=source_id)]

    @router.get(
        "/aggregation-rules", response_model=list[RuleResponse], responses=errors
    )
    async def list_rules() -> list[RuleResponse]:
        return [_rule(item) for item in port.list_rules()]

    @router.get(
        "/aggregation-labels",
        response_model=AggregationLabelCatalogResponse,
        responses=errors,
    )
    async def aggregation_labels(
        lookback_hours: int = 168,
    ) -> AggregationLabelCatalogResponse:
        if lookback_hours not in {24, 168, 720}:
            raise SafeApiError(
                status_code=422,
                code="LABEL_LOOKBACK_INVALID",
                message="标签目录回看窗口仅支持 24、168 或 720 小时",
            )
        history: tuple[dict[str, str], ...] = ()
        history_status: HistoricalLabelStatus = "unconfigured"
        history_error: str | None = None
        if historical_label_reader is not None:
            history, history_status, history_error = await historical_label_reader(
                lookback_hours
            )
        return AggregationLabelCatalogResponse(
            lookback_hours=cast(Literal[24, 168, 720], lookback_hours),
            history_status=history_status,
            history_error=history_error,
            history_series_count=len(history),
            labels=[
                AggregationLabelResponse(**asdict(item))
                for item in port.label_catalog(history)
            ],
        )

    @router.post(
        "/aggregation-rules/preview",
        response_model=RulePreviewResponse,
        responses=errors,
    )
    async def preview_rule(payload: RulePreviewInput) -> RulePreviewResponse:
        try:
            result = port.preview_rule(
                rule_id=payload.rule_id,
                name=payload.name,
                priority=payload.priority,
                enabled=payload.enabled,
                matchers=_matchers(payload),
                group_by_labels=tuple(payload.group_by_labels),
                source_ids=tuple(payload.source_ids),
                grouping_window_seconds=payload.grouping_window_seconds,
            )
        except (ValueError, re.error) as exc:
            raise SafeApiError(
                status_code=422,
                code="AGGREGATION_RULE_INVALID",
                message="聚合规则定义无效",
            ) from exc
        return RulePreviewResponse(**asdict(result))

    @router.post(
        "/aggregation-rules",
        response_model=RuleResponse,
        status_code=status.HTTP_201_CREATED,
        responses=errors,
    )
    async def publish_rule(
        payload: RuleInput,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> RuleResponse:
        try:
            rendered = execute_idempotent_create(
                commands,
                scope="aggregation-rule.create",
                key=idempotency_key,
                payload=payload.model_dump(mode="json"),
                action=lambda: _rule(
                    port.publish_rule(
                        name=payload.name,
                        priority=payload.priority,
                        enabled=payload.enabled,
                        matchers=_matchers(payload),
                        group_by_labels=tuple(payload.group_by_labels),
                        source_ids=tuple(payload.source_ids),
                        grouping_window_seconds=payload.grouping_window_seconds,
                        now=datetime.now(timezone.utc),
                    )
                ).model_dump(mode="json"),
            )
        except IntegrityError as exc:
            raise SafeApiError(
                status_code=409,
                code="AGGREGATION_RULE_CONFLICT",
                message="聚合规则名称已存在",
            ) from exc
        except (ValueError, re.error) as exc:
            raise SafeApiError(
                status_code=422,
                code="AGGREGATION_RULE_INVALID",
                message="聚合规则定义无效",
            ) from exc
        return RuleResponse.model_validate(rendered)

    @router.put(
        "/aggregation-rules/{rule_id}",
        response_model=RuleResponse,
        responses=errors,
    )
    async def update_rule(rule_id: int, payload: RuleUpdateInput) -> RuleResponse:
        try:
            result = port.update_rule(
                rule_id,
                expected_version=payload.expected_version,
                name=payload.name,
                priority=payload.priority,
                enabled=payload.enabled,
                matchers=_matchers(payload),
                group_by_labels=tuple(payload.group_by_labels),
                source_ids=tuple(payload.source_ids),
                grouping_window_seconds=payload.grouping_window_seconds,
                now=datetime.now(timezone.utc),
            )
        except LookupError as exc:
            raise SafeApiError(
                status_code=404,
                code="AGGREGATION_RULE_NOT_FOUND",
                message="未找到指定聚合规则",
            ) from exc
        except FileExistsError as exc:
            raise SafeApiError(
                status_code=409,
                code="AGGREGATION_RULE_VERSION_CONFLICT",
                message="聚合规则已被更新",
            ) from exc
        except IntegrityError as exc:
            raise SafeApiError(
                status_code=409,
                code="AGGREGATION_RULE_CONFLICT",
                message="聚合规则名称已存在",
            ) from exc
        except (ValueError, re.error) as exc:
            raise SafeApiError(
                status_code=422,
                code="AGGREGATION_RULE_INVALID",
                message="聚合规则定义无效",
            ) from exc
        return _rule(result)

    return router
