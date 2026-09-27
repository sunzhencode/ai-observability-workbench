"""Job query and persisted event-stream HTTP surface."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import json
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query
from fastapi.responses import StreamingResponse

from app.api.v1.cursor import CursorError
from app.api.v1.schemas import (
    ErrorEnvelope,
    EventPageResponse,
    EventResponse,
    JobResponse,
)
from app.application.events import EventPage, EventQueryService
from app.application.jobs import JobQueue
from app.domains.operations.jobs import JobView
from app.domains.operations.events import PlatformEvent
from app.platform.errors import SafeApiError
from app.platform.utc import to_utc_iso


def _types(value: str | None) -> tuple[str, ...]:
    if value is None or not value.strip():
        return ()
    normalized = tuple(sorted({item.strip() for item in value.split(",") if item.strip()}))
    if any(len(item) > 96 for item in normalized):
        raise SafeApiError(
            status_code=400,
            code="EVENT_TYPE_INVALID",
            message="事件类型过滤条件无效",
        )
    return normalized


def _event(item: PlatformEvent) -> EventResponse:
    return EventResponse(
        sequence=item.sequence,
        event_type=item.event_type,
        subject_type=item.subject_type,
        subject_id=item.subject_id,
        created_at=to_utc_iso(item.created_at),
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


def _poll(
    service: EventQueryService,
    *,
    cursor: str | None,
    types: str | None,
    limit: int,
) -> EventPage:
    try:
        return service.poll(
            cursor=cursor,
            event_types=_types(types),
            limit=limit,
        )
    except CursorError as exc:
        message = (
            "游标与当前过滤条件不匹配"
            if exc.code == "CURSOR_FILTER_MISMATCH"
            else "游标无效或已被修改"
        )
        raise SafeApiError(status_code=400, code=exc.code, message=message) from exc


def create_api_v1_router(
    *,
    job_queries: JobQueue,
    event_queries: EventQueryService,
    heartbeat_seconds: float = 15.0,
    max_heartbeats: int | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1")
    error_responses: dict[int | str, dict[str, Any]] = {
        400: {"model": ErrorEnvelope},
        404: {"model": ErrorEnvelope},
        422: {"model": ErrorEnvelope},
        500: {"model": ErrorEnvelope},
    }

    @router.get(
        "/jobs/{job_id}",
        response_model=JobResponse,
        responses=error_responses,
    )
    async def get_job(job_id: str) -> JobResponse:
        try:
            return _job(job_queries.get(job_id))
        except KeyError as exc:
            raise SafeApiError(
                status_code=404,
                code="JOB_NOT_FOUND",
                message="未找到指定任务",
            ) from exc

    @router.get(
        "/events/poll",
        response_model=EventPageResponse,
        responses=error_responses,
    )
    async def poll_events(
        cursor: str | None = None,
        types: str | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> EventPageResponse:
        page = _poll(
            event_queries,
            cursor=cursor,
            types=types,
            limit=limit,
        )
        return EventPageResponse(
            items=[_event(item) for item in page.items],
            next_cursor=page.next_cursor,
        )

    @router.get(
        "/events",
        response_class=StreamingResponse,
        responses={
            200: {"content": {"text/event-stream": {}}},
            **error_responses,
        },
    )
    async def stream_events(
        cursor: str | None = None,
        types: str | None = None,
        last_event_id: Annotated[
            str | None,
            Header(alias="Last-Event-ID"),
        ] = None,
    ) -> StreamingResponse:
        async def generate() -> AsyncIterator[str]:
            event_types = _types(types)
            current = cursor if cursor is not None else last_event_id
            heartbeats = 0
            while True:
                page = _poll(
                    event_queries,
                    cursor=current,
                    types=types,
                    limit=100,
                )
                for item in page.items:
                    payload = json.dumps(
                        _event(item).model_dump(),
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    item_cursor = event_queries.cursor_for(
                        position=item.sequence,
                        event_types=event_types,
                    )
                    yield f"id: {item_cursor}\nevent: {item.event_type}\ndata: {payload}\n\n"
                current = page.next_cursor
                await asyncio.sleep(heartbeat_seconds)
                yield ": heartbeat\n\n"
                heartbeats += 1
                if max_heartbeats is not None and heartbeats >= max_heartbeats:
                    return

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router
