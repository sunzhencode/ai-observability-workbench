"""Current Incident handling and append-only occurrence-history HTTP surface."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Header, Query, Request

from app.api.v1.contracts import validate_idempotency_key
from app.api.v1.operator_witness import OperatorWitness
from app.api.v1.cursor import CursorError
from app.api.v1.schemas import (
    AlertResponse,
    ErrorEnvelope,
    IncidentDetailResponse,
    IncidentHandlingAuditResponse,
    IncidentOccurrencePageResponse,
    IncidentOccurrenceResponse,
    OperationalOccurrencePageResponse,
    OperationalOccurrenceResponse,
    OccurrenceStartHandlingInput,
    OccurrenceResolveInput,
    BatchStartHandlingInput,
    BatchStartHandlingItemResponse,
    BatchStartHandlingResponse,
    CollaborationCommandResponse,
    IncidentNoteCreateInput,
    IncidentNoteRedactInput,
    IncidentTaskCreateInput,
    IncidentTaskResponse,
    IncidentTaskTransitionInput,
    ResponseCommandResponse,
    ServiceAssignmentInput,
    ServiceAssignmentResponse,
    SimilarHistoryMatchReasonResponse,
    SimilarHistoryResponse,
    TimelineEntryResponse,
)
from app.application.commands import CommandConflict
from app.application.incidents import (
    CollaborationCommandResult,
    HandlingAuditView,
    IncidentStore,
    IncidentCollaborationCommands,
    IncidentTaskView,
    IncidentResponseCommands,
    IncidentServiceAssignmentCommands,
    OccurrenceView,
    OperationalOccurrenceView,
    ResponseCommandResult,
    ServiceAssignmentCommandResult,
    SimilarHistoryView,
    TimelineEntryView,
)
from app.domains.incidents.collaboration import (
    IncidentTaskDraft,
    IncidentTaskState,
    validate_note_text,
    validate_task_text,
)
from app.domains.incidents.response import (
    ResolutionCode,
    ResponseAction,
)
from app.platform.errors import SafeApiError
from app.platform.utc import to_utc_iso


def _errors() -> dict[int | str, dict[str, Any]]:
    return {
        400: {"model": ErrorEnvelope},
        404: {"model": ErrorEnvelope},
        409: {"model": ErrorEnvelope},
        422: {"model": ErrorEnvelope},
        428: {"model": ErrorEnvelope},
    }


def _audit(item: HandlingAuditView) -> IncidentHandlingAuditResponse:
    return IncidentHandlingAuditResponse(
        id=item.id,
        incident_id=item.incident_id,
        actor=item.actor,
        from_state=item.from_state,
        to_state=item.to_state,
        reason=item.reason,
        created_at=to_utc_iso(item.created_at),
    )


def _occurrence(item: OccurrenceView) -> IncidentOccurrenceResponse:
    payload = asdict(item)
    payload["started_at"] = to_utc_iso(item.started_at)
    payload["recovered_at"] = to_utc_iso(item.recovered_at)
    return IncidentOccurrenceResponse(**payload)


def _member(item: Any) -> AlertResponse:
    payload = asdict(item)
    payload["starts_at"] = None if item.starts_at is None else to_utc_iso(item.starts_at)
    payload["missing_since_at"] = (
        None if item.missing_since_at is None else to_utc_iso(item.missing_since_at)
    )
    payload["last_seen_at"] = to_utc_iso(item.last_seen_at)
    return AlertResponse(**payload)


def _operational_occurrence(
    item: OperationalOccurrenceView,
) -> OperationalOccurrenceResponse:
    payload = asdict(item)
    for field in (
        "detected_at",
        "source_started_at",
        "ack_sla_due_at",
        "acknowledged_at",
        "resolved_at",
        "noise_starts_at",
        "noise_ends_at",
    ):
        value = payload[field]
        payload[field] = None if value is None else to_utc_iso(value)
    payload["latest_activity_at"] = to_utc_iso(item.latest_activity_at)
    return OperationalOccurrenceResponse(**payload)


def _timeline(item: TimelineEntryView) -> TimelineEntryResponse:
    return TimelineEntryResponse(
        id=item.id,
        occurrence_id=item.occurrence_id,
        sequence=item.sequence,
        actor_type=cast(Any, item.actor_type),
        event_type=cast(Any, item.event_type),
        summary=item.summary,
        detail=item.detail,
        request_id=item.request_id,
        source_ip=item.source_ip,
        created_at=to_utc_iso(item.created_at),
    )


def _task(item: IncidentTaskView) -> IncidentTaskResponse:
    return IncidentTaskResponse(
        id=item.id,
        occurrence_id=item.occurrence_id,
        title=item.title,
        description=item.description,
        due_at=None if item.due_at is None else to_utc_iso(item.due_at),
        runbook_link=item.runbook_link,
        status=cast(Any, item.status),
        result=item.result,
        version=item.version,
        created_at=to_utc_iso(item.created_at),
        updated_at=to_utc_iso(item.updated_at),
    )


def _collaboration(item: CollaborationCommandResult) -> CollaborationCommandResponse:
    return CollaborationCommandResponse(
        task=None if item.task is None else _task(item.task),
        timeline=_timeline(item.timeline),
        replayed=item.replayed,
    )


def _response_command(item: ResponseCommandResult) -> ResponseCommandResponse:
    return ResponseCommandResponse(
        occurrence_id=item.occurrence_id,
        previous_state=item.previous_state,
        current_state=item.current_state,
        resolution_code=item.resolution_code,
        version=item.version,
        timeline=[_timeline(entry) for entry in item.timeline],
        replayed=item.replayed,
    )


def _service_assignment(
    item: ServiceAssignmentCommandResult,
) -> ServiceAssignmentResponse:
    return ServiceAssignmentResponse(
        occurrence_id=item.occurrence_id,
        service_id=item.service_id,
        service_name=item.service_name,
        assignment_origin="MANUAL",
        service_assignment_state="MAPPED",
        version=item.version,
        timeline=_timeline(item.timeline),
        replayed=item.replayed,
    )


def _similar_history(item: SimilarHistoryView) -> SimilarHistoryResponse:
    return SimilarHistoryResponse(
        occurrence_id=item.occurrence_id,
        occurrence_no=item.occurrence_no,
        title=item.title,
        score=item.score,
        match_reasons=[
            SimilarHistoryMatchReasonResponse(
                kind=cast(Any, reason.kind),
                field=reason.field,
                value=reason.value,
                points=reason.points,
            )
            for reason in item.match_reasons
        ],
        resolution_code=item.resolution_code,
        operator_conclusion=item.operator_conclusion,
        task_outcome=item.task_outcome,
        handling_duration_seconds=item.handling_duration_seconds,
        resolved_at=to_utc_iso(item.resolved_at),
    )


def _response_error(exc: Exception) -> SafeApiError:
    if isinstance(exc, LookupError):
        code = str(exc)
        if code == "ACTIVE_SERVICE_NOT_FOUND":
            return SafeApiError(
                status_code=404,
                code=code,
                message="未找到可用于当前事件的有效服务",
            )
        if code == "INCIDENT_TASK_NOT_FOUND":
            return SafeApiError(
                status_code=404, code=code, message="未找到指定人工处置任务"
            )
        if code == "INCIDENT_NOTE_NOT_FOUND":
            return SafeApiError(
                status_code=404, code=code, message="未找到指定人工处置 Note"
            )
        return SafeApiError(
            status_code=404,
            code="OPERATIONAL_OCCURRENCE_NOT_FOUND",
            message="未找到指定 Incident Occurrence",
        )
    if isinstance(exc, FileExistsError):
        return SafeApiError(
            status_code=409,
            code="CONCURRENT_MODIFICATION",
            message="Occurrence 已被更新，请刷新当前状态后再执行命令",
        )
    if isinstance(exc, CommandConflict):
        return SafeApiError(
            status_code=409,
            code=exc.code,
            message=(
                "该 Idempotency-Key 已用于不同请求"
                if exc.code == "IDEMPOTENCY_KEY_REUSED"
                else "前次命令结果不确定，系统不会自动重复执行"
            ),
        )
    if isinstance(exc, PermissionError):
        return SafeApiError(
            status_code=403,
            code="INTERACTIVE_OPERATOR_REQUIRED",
            message="该响应命令只允许由交互式操作者请求发起",
        )
    code = str(exc)
    messages = {
        "RESPONSE_REASON_REQUIRED": "当前信号尚未确认恢复，结束处理必须填写判断说明",
        "RESPONSE_REASON_INVALID": "判断理由不能为空且不得超过 2000 字符",
        "RESPONSE_TRANSITION_INVALID": "当前 Response State 不允许执行该命令",
        "RESOLUTION_SIGNAL_STATE_INVALID": "当前信号尚未恢复，不能选择人工处理后恢复或上游自行恢复",
        "INCIDENT_OPEN_TASKS_REMAIN": "仍有未完成的人工处置任务，请先完成任务，或填写理由取消任务",
        "DUPLICATE_TARGET_REQUIRED": "DUPLICATE 结案必须选择 canonical Occurrence",
        "DUPLICATE_TARGET_NOT_ALLOWED": "仅 DUPLICATE 结案可以关联 canonical Occurrence",
        "DUPLICATE_TARGET_INVALID": "canonical Occurrence 不得是自身或另一条 DUPLICATE 链",
        "RUNBOOK_LINK_HTTPS_REQUIRED": "Runbook 链接只接受 HTTPS 地址",
        "RUNBOOK_LINK_CREDENTIALS_NOT_ALLOWED": "Runbook 链接不得内嵌用户名或密码",
        "RUNBOOK_LINK_TOO_LONG": "Runbook 链接不得超过 2048 字符",
        "INCIDENT_TASK_TITLE_INVALID": "Task 标题不能为空且不得超过 200 字符",
        "INCIDENT_TASK_DESCRIPTION_INVALID": "Task 描述不得超过 4000 字符",
        "INCIDENT_TASK_RESULT_INVALID": "Task 结果不得超过 4000 字符",
        "INCIDENT_TASK_REASON_INVALID": "Task 取消理由不得超过 2000 字符",
        "INCIDENT_TASK_DUE_AT_UTC_REQUIRED": "Task 截止时间必须包含明确时区",
        "INCIDENT_TASK_RESULT_REQUIRED": "完成 Task 必须记录核验结果",
        "INCIDENT_TASK_REASON_REQUIRED": "取消 Task 必须记录判断理由",
        "INCIDENT_TASK_RESULT_NOT_ALLOWED": "仅完成 Task 时可以记录结果",
        "INCIDENT_TASK_REASON_NOT_ALLOWED": "仅取消 Task 时可以记录取消理由",
        "INCIDENT_TASK_TRANSITION_INVALID": "当前 Task 状态不允许执行该转换",
        "INCIDENT_NOTE_TEXT_INVALID": "Note 不能为空且不得超过 4000 字符",
        "INCIDENT_NOTE_ALREADY_REDACTED": "该 Note 已脱敏，不会重复改写审计事实",
        "INCIDENT_COLLABORATION_RESOLVED_READ_ONLY": "本次 Occurrence 已结案；Task 与 Note 保持只读，如需继续处置请等待新的 Occurrence",
        "SERVICE_ASSIGNMENT_RESOLVED_READ_ONLY": "本次 Occurrence 已结案；服务归属保持只读",
    }
    return SafeApiError(
        status_code=422,
        code=code if code in messages else "INCIDENT_RESPONSE_INPUT_INVALID",
        message=messages.get(code, "响应命令字段或状态前置条件不符合约束"),
    )


def _csv_values(value: str | None) -> tuple[str, ...]:
    return tuple(
        sorted({item.strip() for item in (value or "").split(",") if item.strip()})
    )


def create_incidents_router(
    *,
    store: IncidentStore,
    response_commands: IncidentResponseCommands,
    collaboration_commands: IncidentCollaborationCommands,
    service_assignment_commands: IncidentServiceAssignmentCommands,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1")
    errors = _errors()

    @router.get("/incidents/{incident_id}", response_model=IncidentDetailResponse, responses=errors)
    async def get_incident(incident_id: int) -> IncidentDetailResponse:
        try:
            value = store.get_incident(incident_id)
        except LookupError as exc:
            raise SafeApiError(status_code=404, code="INCIDENT_NOT_FOUND", message="未找到指定 Incident") from exc
        return IncidentDetailResponse(
            id=value.id,
            source_id=value.source_id,
            source_name=value.source_name,
            group_key=value.group_key,
            title=value.title,
            severity=value.severity,
            source_state=value.source_state,
            freshness_state=value.freshness_state,
            handling_state=value.handling_state,
            handling_version=value.handling_version,
            occurrence_no=value.occurrence_no,
            occurrence_started_at=to_utc_iso(value.occurrence_started_at),
            updated_at=to_utc_iso(value.updated_at),
            member_count=value.member_count,
            aggregation_rule_id=value.aggregation_rule_id,
            aggregation_rule_name=value.aggregation_rule_name,
            aggregation_status=value.aggregation_status,
            grouping_explanation=value.grouping_explanation,
            aggregation_rule_version=value.aggregation_rule_version,
            members=[_member(item) for item in value.members],
            handling_history=[_audit(item) for item in value.handling_history],
        )

    @router.get(
        "/occurrences",
        response_model=OperationalOccurrencePageResponse,
        responses=errors,
    )
    async def list_operational_occurrences(
        view: Literal[
            "ALL", "UNACKNOWLEDGED", "SLA_AT_RISK", "UNMAPPED", "RESOLVED"
        ] = "ALL",
        source_ids: str | None = None,
        signal_states: str | None = None,
        cursor: str | None = None,
        limit: int = Query(default=50, ge=1, le=200),
    ) -> OperationalOccurrencePageResponse:
        try:
            page = store.list_operational_occurrences(
                view=view,
                source_ids=_csv_values(source_ids),
                signal_states=_csv_values(signal_states),
                cursor=cursor,
                limit=limit,
                now=datetime.now(timezone.utc),
            )
        except CursorError as exc:
            raise SafeApiError(
                status_code=400,
                code=exc.code,
                message="Incident Queue 游标无效或与当前筛选条件不匹配",
            ) from exc
        except ValueError as exc:
            raise SafeApiError(
                status_code=422,
                code="INCIDENT_QUEUE_FILTER_INVALID",
                message="Incident Queue 筛选值不受支持，请检查视图或 Signal 状态",
            ) from exc
        return OperationalOccurrencePageResponse(
            items=[_operational_occurrence(item) for item in page.items],
            next_cursor=page.next_cursor,
            evaluated_at=to_utc_iso(page.evaluated_at),
        )

    @router.get(
        "/occurrences/{occurrence_id}",
        response_model=OperationalOccurrenceResponse,
        responses=errors,
    )
    async def get_operational_occurrence(
        occurrence_id: int,
    ) -> OperationalOccurrenceResponse:
        try:
            value = store.get_operational_occurrence(
                occurrence_id, now=datetime.now(timezone.utc)
            )
        except LookupError as exc:
            raise SafeApiError(
                status_code=404,
                code="OPERATIONAL_OCCURRENCE_NOT_FOUND",
                message="未找到指定 Incident Occurrence",
            ) from exc
        return _operational_occurrence(value)

    @router.get(
        "/occurrences/{occurrence_id}/similar",
        response_model=list[SimilarHistoryResponse],
        responses=errors,
    )
    async def list_similar_occurrences(
        occurrence_id: int,
        limit: int = Query(default=10, ge=1, le=10),
    ) -> list[SimilarHistoryResponse]:
        try:
            return [
                _similar_history(item)
                for item in store.list_similar_history(occurrence_id, limit=limit)
            ]
        except LookupError as exc:
            raise SafeApiError(
                status_code=404,
                code="OPERATIONAL_OCCURRENCE_NOT_FOUND",
                message="未找到指定 Incident Occurrence",
            ) from exc

    @router.put(
        "/occurrences/{occurrence_id}/service",
        response_model=ServiceAssignmentResponse,
        responses=errors,
    )
    async def assign_occurrence_service(
        request: Request,
        occurrence_id: int,
        payload: ServiceAssignmentInput,
        idempotency_key: Annotated[
            str | None, Header(alias="Idempotency-Key")
        ] = None,
    ) -> ServiceAssignmentResponse:
        key = validate_idempotency_key(idempotency_key)
        try:
            return _service_assignment(
                service_assignment_commands.assign(
                    occurrence_id,
                    service_id=payload.service_id,
                    actor=OperatorWitness().actor(),
                    expected_version=payload.expected_version,
                    idempotency_key=key,
                    request_id=str(
                        getattr(request.state, "request_id", "unavailable")
                    ),
                    source_ip=(
                        request.client.host
                        if request.client is not None
                        else "unavailable"
                    ),
                    now=datetime.now(timezone.utc),
                )
            )
        except (
            LookupError,
            FileExistsError,
            CommandConflict,
            PermissionError,
            ValueError,
        ) as exc:
            raise _response_error(exc) from exc

    def execute_response(
        request: Request,
        occurrence_id: int,
        *,
        action: ResponseAction,
        expected_version: int,
        idempotency_key: str | None,
    ) -> ResponseCommandResponse:
        key = validate_idempotency_key(idempotency_key)
        try:
            value = response_commands.execute(
                occurrence_id,
                action=action,
                actor=OperatorWitness().actor(),
                expected_version=expected_version,
                idempotency_key=key,
                request_id=str(getattr(request.state, "request_id", "unavailable")),
                source_ip=(request.client.host if request.client is not None else "unavailable"),
                now=datetime.now(timezone.utc),
            )
        except (LookupError, FileExistsError, CommandConflict, PermissionError, ValueError) as exc:
            raise _response_error(exc) from exc
        return _response_command(value)

    def collaboration_context(request: Request) -> tuple[Any, str, str]:
        return (
            OperatorWitness().actor(),
            str(getattr(request.state, "request_id", "unavailable")),
            request.client.host if request.client is not None else "unavailable",
        )

    @router.get(
        "/occurrences/{occurrence_id}/tasks",
        response_model=list[IncidentTaskResponse],
        responses=errors,
    )
    async def list_occurrence_tasks(occurrence_id: int) -> list[IncidentTaskResponse]:
        try:
            return [_task(item) for item in store.list_tasks(occurrence_id)]
        except LookupError as exc:
            raise _response_error(exc) from exc

    @router.post(
        "/occurrences/{occurrence_id}/tasks",
        response_model=CollaborationCommandResponse,
        status_code=201,
        responses=errors,
    )
    async def create_occurrence_task(
        request: Request,
        occurrence_id: int,
        payload: IncidentTaskCreateInput,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> CollaborationCommandResponse:
        key = validate_idempotency_key(idempotency_key)
        actor, request_id, source_ip = collaboration_context(request)
        try:
            draft = IncidentTaskDraft.validated(
                title=payload.title,
                description=payload.description,
                due_at=payload.due_at,
                runbook_link=payload.runbook_link,
            )
            return _collaboration(
                collaboration_commands.create_task(
                    occurrence_id,
                    draft=draft,
                    actor=actor,
                    idempotency_key=key,
                    request_id=request_id,
                    source_ip=source_ip,
                    now=datetime.now(timezone.utc),
                )
            )
        except (LookupError, CommandConflict, ValueError) as exc:
            raise _response_error(exc) from exc

    @router.post(
        "/occurrences/{occurrence_id}/tasks/{task_id}/transition",
        response_model=CollaborationCommandResponse,
        responses=errors,
    )
    async def transition_occurrence_task(
        request: Request,
        occurrence_id: int,
        task_id: int,
        payload: IncidentTaskTransitionInput,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> CollaborationCommandResponse:
        key = validate_idempotency_key(idempotency_key)
        actor, request_id, source_ip = collaboration_context(request)
        try:
            return _collaboration(
                collaboration_commands.transition_task(
                    occurrence_id,
                    task_id,
                    expected_version=payload.expected_version,
                    target=IncidentTaskState(payload.target),
                    result=payload.result,
                    reason=payload.reason,
                    actor=actor,
                    idempotency_key=key,
                    request_id=request_id,
                    source_ip=source_ip,
                    now=datetime.now(timezone.utc),
                )
            )
        except (LookupError, FileExistsError, CommandConflict, ValueError) as exc:
            raise _response_error(exc) from exc

    @router.post(
        "/occurrences/{occurrence_id}/notes",
        response_model=CollaborationCommandResponse,
        status_code=201,
        responses=errors,
    )
    async def add_occurrence_note(
        request: Request,
        occurrence_id: int,
        payload: IncidentNoteCreateInput,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> CollaborationCommandResponse:
        key = validate_idempotency_key(idempotency_key)
        actor, request_id, source_ip = collaboration_context(request)
        try:
            text = validate_note_text(payload.text)
            return _collaboration(
                collaboration_commands.add_note(
                    occurrence_id,
                    text=text,
                    actor=actor,
                    idempotency_key=key,
                    request_id=request_id,
                    source_ip=source_ip,
                    now=datetime.now(timezone.utc),
                )
            )
        except (LookupError, CommandConflict, ValueError) as exc:
            raise _response_error(exc) from exc

    @router.post(
        "/occurrences/{occurrence_id}/notes/{sequence}/redact",
        response_model=CollaborationCommandResponse,
        responses=errors,
    )
    async def redact_occurrence_note(
        request: Request,
        occurrence_id: int,
        sequence: int,
        payload: IncidentNoteRedactInput,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> CollaborationCommandResponse:
        key = validate_idempotency_key(idempotency_key)
        actor, request_id, source_ip = collaboration_context(request)
        try:
            reason = validate_task_text(
                payload.reason, field="REASON", maximum=2000
            )
            return _collaboration(
                collaboration_commands.redact_note(
                    occurrence_id,
                    sequence,
                    reason=reason,
                    actor=actor,
                    idempotency_key=key,
                    request_id=request_id,
                    source_ip=source_ip,
                    now=datetime.now(timezone.utc),
                )
            )
        except (LookupError, CommandConflict, ValueError) as exc:
            raise _response_error(exc) from exc

    @router.get(
        "/occurrences/{occurrence_id}/timeline",
        response_model=list[TimelineEntryResponse],
        responses=errors,
    )
    async def list_occurrence_timeline(
        occurrence_id: int,
        after_sequence: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=200),
    ) -> list[TimelineEntryResponse]:
        try:
            return [
                _timeline(item)
                for item in store.list_timeline(
                    occurrence_id, after_sequence=after_sequence, limit=limit
                )
            ]
        except LookupError as exc:
            raise _response_error(exc) from exc

    @router.post(
        "/occurrences/{occurrence_id}/start-handling",
        response_model=ResponseCommandResponse,
        responses=errors,
    )
    async def start_handling_occurrence(
        request: Request,
        occurrence_id: int,
        payload: OccurrenceStartHandlingInput,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> ResponseCommandResponse:
        return execute_response(
            request,
            occurrence_id,
            action=ResponseAction.start_handling(payload.reason),
            expected_version=payload.expected_version,
            idempotency_key=idempotency_key,
        )

    @router.post(
        "/occurrences/{occurrence_id}/resolve",
        response_model=ResponseCommandResponse,
        responses=errors,
    )
    async def resolve_occurrence(
        request: Request,
        occurrence_id: int,
        payload: OccurrenceResolveInput,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> ResponseCommandResponse:
        return execute_response(
            request,
            occurrence_id,
            action=ResponseAction.resolve(
                ResolutionCode(payload.resolution_code),
                duplicate_of=payload.duplicate_of_occurrence_id,
                reason=payload.reason,
            ),
            expected_version=payload.expected_version,
            idempotency_key=idempotency_key,
        )

    @router.post(
        "/occurrences/batch-start-handling",
        response_model=BatchStartHandlingResponse,
        responses=errors,
    )
    async def batch_start_handling_occurrences(
        request: Request,
        payload: BatchStartHandlingInput,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> BatchStartHandlingResponse:
        key = validate_idempotency_key(idempotency_key)
        seen: set[int] = set()
        results: list[BatchStartHandlingItemResponse] = []
        for item in payload.items:
            if item.occurrence_id in seen:
                results.append(
                    BatchStartHandlingItemResponse(
                        occurrence_id=item.occurrence_id,
                        ok=False,
                        error_code="BATCH_OCCURRENCE_DUPLICATED",
                        message="同一批次不能重复包含相同 Occurrence",
                    )
                )
                continue
            seen.add(item.occurrence_id)
            try:
                command = execute_response(
                    request,
                    item.occurrence_id,
                    action=ResponseAction.start_handling(payload.reason),
                    expected_version=item.expected_version,
                    idempotency_key=key,
                )
                results.append(
                    BatchStartHandlingItemResponse(
                        occurrence_id=item.occurrence_id, ok=True, command=command
                    )
                )
            except SafeApiError as exc:
                results.append(
                    BatchStartHandlingItemResponse(
                        occurrence_id=item.occurrence_id,
                        ok=False,
                        error_code=exc.code,
                        message=exc.message,
                    )
                )
        succeeded = sum(item.ok for item in results)
        return BatchStartHandlingResponse(
            items=results, succeeded=succeeded, failed=len(results) - succeeded
        )

    @router.get(
        "/incident-occurrences",
        response_model=IncidentOccurrencePageResponse,
        responses=errors,
    )
    async def list_occurrences(
        source_ids: str | None = None,
        conclusion: str | None = None,
        include_archived: bool = False,
        cursor: str | None = None,
        limit: int = Query(default=50, ge=1, le=200),
    ) -> IncidentOccurrencePageResponse:
        normalized = tuple(sorted({item.strip() for item in (source_ids or "").split(",") if item.strip()}))
        try:
            page = store.list_occurrences(
                source_ids=normalized,
                conclusion=conclusion,
                include_archived=include_archived,
                cursor=cursor,
                limit=limit,
            )
        except CursorError as exc:
            raise SafeApiError(status_code=400, code=exc.code, message="历史游标无效或与当前过滤条件不匹配") from exc
        except ValueError as exc:
            raise SafeApiError(status_code=422, code="INCIDENT_HISTORY_FILTER_INVALID", message="历史过滤条件不符合约束") from exc
        return IncidentOccurrencePageResponse(
            items=[_occurrence(item) for item in page.items],
            next_cursor=page.next_cursor,
        )

    @router.get(
        "/incident-occurrences/{occurrence_id}",
        response_model=IncidentOccurrenceResponse,
        responses=errors,
    )
    async def get_occurrence(occurrence_id: int, include_archived: bool = False) -> IncidentOccurrenceResponse:
        try:
            return _occurrence(store.get_occurrence(occurrence_id, include_archived=include_archived))
        except LookupError as exc:
            raise SafeApiError(status_code=404, code="INCIDENT_OCCURRENCE_NOT_FOUND", message="未找到指定发生记录") from exc

    return router
