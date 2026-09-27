"""Signed cursor, persisted event, SSE and polling fallback contracts."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.adapters.persistence.jobs import SqlAlchemyJobStore
from app.api.v1.cursor import CursorError, SignedCursorCodec
from app.api.v1.router import create_api_v1_router
from app.application.events import EventQueryService
from app.application.jobs import JobQueue
from app.bootstrap import create_platform_app, default_platform_wiring
from app.domains.operations.jobs import JobPool, JobSpec
from app.platform.persistence.database import (
    SqliteDatabaseConfig,
    create_session_factory,
    create_sqlite_engine,
)
from app.platform.persistence.migrations import upgrade_database


def _services(tmp_path: Path):
    engine = create_sqlite_engine(SqliteDatabaseConfig(path=tmp_path / "incident-operations.db"))
    upgrade_database(engine)
    sessions = create_session_factory(engine)
    store = SqlAlchemyJobStore(sessions)
    queue = JobQueue(store)
    codec = SignedCursorCodec(b"cursor-test-key-that-is-at-least-32-bytes")
    events = EventQueryService(store, codec)
    return queue, events, codec, store


def _enqueue(queue: JobQueue, key: str):
    return queue.enqueue(
        JobSpec(
            kind="source.collect",
            pool=JobPool.SOURCE,
            subject_type="source",
            subject_id="source-a",
            payload={},
            payload_revision=1,
            idempotency_key=key,
        )
    )


def test_cursor_tamper_and_filter_reuse_fail_closed() -> None:
    codec = SignedCursorCodec(b"cursor-test-key-that-is-at-least-32-bytes")
    cursor = codec.encode(position=42, filters={"types": ["job.pending"]})

    assert codec.decode(cursor, filters={"types": ["job.pending"]}).position == 42
    try:
        codec.decode(cursor + "x", filters={"types": ["job.pending"]})
    except CursorError as exc:
        assert exc.code == "CURSOR_INVALID"
    else:
        raise AssertionError("tampered cursor was accepted")

    try:
        codec.decode(cursor, filters={"types": ["job.succeeded"]})
    except CursorError as exc:
        assert exc.code == "CURSOR_FILTER_MISMATCH"
    else:
        raise AssertionError("cursor was reused with different filters")


def test_poll_fallback_returns_persisted_events_and_resumes(tmp_path: Path) -> None:
    queue, events, _codec, _store = _services(tmp_path)
    job = _enqueue(queue, "request-poll-1")
    router = create_api_v1_router(job_queries=queue, event_queries=events)
    app = create_platform_app(
        wiring=default_platform_wiring(routers=(router,))
    )

    with TestClient(app) as client:
        first = client.get("/api/v1/events/poll", params={"types": "job.pending"})
        cursor = first.json()["next_cursor"]
        second = client.get(
            "/api/v1/events/poll",
            params={"types": "job.pending", "cursor": cursor},
        )

    assert first.status_code == 200
    assert first.json()["items"] == [
        {
            "sequence": 1,
            "event_type": "job.pending",
            "subject_type": "job",
            "subject_id": job.id,
            "created_at": first.json()["items"][0]["created_at"],
        }
    ]
    assert first.json()["items"][0]["created_at"].endswith("Z")
    assert second.status_code == 200
    assert second.json()["items"] == []
    assert second.json()["next_cursor"] == cursor


def test_poll_cursor_cannot_be_reused_with_changed_filters(tmp_path: Path) -> None:
    queue, events, _codec, _store = _services(tmp_path)
    _enqueue(queue, "request-poll-2")
    app = create_platform_app(
        wiring=default_platform_wiring(
            routers=(create_api_v1_router(job_queries=queue, event_queries=events),)
        )
    )

    with TestClient(app) as client:
        cursor = client.get(
            "/api/v1/events/poll", params={"types": "job.pending"}
        ).json()["next_cursor"]
        response = client.get(
            "/api/v1/events/poll",
            params={"types": "job.succeeded", "cursor": cursor},
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "CURSOR_FILTER_MISMATCH"


def test_sse_emits_persisted_event_id_and_heartbeat_then_closes_for_test(
    tmp_path: Path,
) -> None:
    queue, events, _codec, _store = _services(tmp_path)
    _enqueue(queue, "request-sse-1")
    router = create_api_v1_router(
        job_queries=queue,
        event_queries=events,
        heartbeat_seconds=0.001,
        max_heartbeats=1,
    )
    app = create_platform_app(wiring=default_platform_wiring(routers=(router,)))

    with TestClient(app) as client:
        response = client.get("/api/v1/events", params={"types": "job.pending"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: job.pending" in response.text
    assert "data:" in response.text
    assert "id:" in response.text
    assert ": heartbeat" in response.text


def test_sse_last_event_id_replays_without_skipping_a_partial_batch(
    tmp_path: Path,
) -> None:
    queue, events, _codec, _store = _services(tmp_path)
    first_job = _enqueue(queue, "request-sse-batch-1")
    second_job = _enqueue(queue, "request-sse-batch-2")
    router = create_api_v1_router(
        job_queries=queue,
        event_queries=events,
        heartbeat_seconds=0.001,
        max_heartbeats=1,
    )
    app = create_platform_app(wiring=default_platform_wiring(routers=(router,)))

    with TestClient(app) as client:
        initial = client.get("/api/v1/events", params={"types": "job.pending"})
        ids = [
            line.removeprefix("id: ")
            for line in initial.text.splitlines()
            if line.startswith("id: ")
        ]
        resumed = client.get(
            "/api/v1/events",
            params={"types": "job.pending"},
            headers={"Last-Event-ID": ids[0]},
        )

    assert len(ids) == 2
    assert first_job.id not in resumed.text
    assert second_job.id in resumed.text


def test_event_cursor_survives_service_restart(tmp_path: Path) -> None:
    queue, events, codec, store = _services(tmp_path)
    _enqueue(queue, "request-restart-1")
    first = events.poll(cursor=None, event_types=("job.pending",), limit=100)
    _enqueue(queue, "request-restart-2")
    restarted = EventQueryService(store, codec)

    resumed = restarted.poll(
        cursor=first.next_cursor,
        event_types=("job.pending",),
        limit=100,
    )

    assert [item.sequence for item in resumed.items] == [2]
