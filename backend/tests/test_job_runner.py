"""Job runner concurrency, crash recovery and safe failure contracts."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.adapters.persistence.jobs import SqlAlchemyJobStore
from app.application.jobs import (
    JobHandlerRegistry,
    JobPolicyRegistry,
    JobQueue,
    JobRunner,
    JobRunnerState,
)
from app.domains.operations.jobs import ConcurrencyBudgets, JobPool, JobSpec, JobState
from app.platform.persistence.database import (
    SqliteDatabaseConfig,
    create_session_factory,
    create_sqlite_engine,
)
from app.platform.persistence.migrations import upgrade_database

UTC = timezone.utc


def _runtime(path: Path) -> tuple[JobQueue, JobRunnerState]:
    engine = create_sqlite_engine(SqliteDatabaseConfig(path=path))
    upgrade_database(engine)
    queue = JobQueue(SqlAlchemyJobStore(create_session_factory(engine)))
    return queue, JobRunnerState()


def _enqueue_many(queue: JobQueue, *, kind: str, pool: JobPool, count: int) -> None:
    for index in range(count):
        queue.enqueue(
            JobSpec(
                kind=kind,
                pool=pool,
                subject_type="test",
                subject_id=str(index),
                payload={"index": index},
                payload_revision=1,
                idempotency_key=f"{kind}-{index:04d}",
            )
        )


@pytest.mark.asyncio
async def test_runner_enforces_independent_pool_budgets(tmp_path: Path) -> None:
    queue, state = _runtime(tmp_path / "incident-operations.db")
    active: dict[JobPool, int] = defaultdict(int)
    maximum: dict[JobPool, int] = defaultdict(int)
    lock = asyncio.Lock()

    def handler(pool: JobPool):
        async def execute(_job, _payload) -> None:
            async with lock:
                active[pool] += 1
                maximum[pool] = max(maximum[pool], active[pool])
            await asyncio.sleep(0.01)
            async with lock:
                active[pool] -= 1

        return execute

    handlers = JobHandlerRegistry(
        {
            "source.collect": handler(JobPool.SOURCE),
            "investigation.plan": handler(JobPool.AI),
            "notification.send": handler(JobPool.NOTIFICATION),
        }
    )
    _enqueue_many(queue, kind="source.collect", pool=JobPool.SOURCE, count=16)
    _enqueue_many(queue, kind="investigation.plan", pool=JobPool.AI, count=5)
    _enqueue_many(queue, kind="notification.send", pool=JobPool.NOTIFICATION, count=8)
    runner = JobRunner(
        queue=queue,
        handlers=handlers,
        policies=JobPolicyRegistry(),
        state=state,
        budgets=ConcurrencyBudgets(),
        owner_prefix="runner-test",
    )

    await runner.drain()

    assert maximum == {
        JobPool.SOURCE: 8,
        JobPool.AI: 2,
        JobPool.NOTIFICATION: 4,
    }
    assert queue.count(state=JobState.SUCCEEDED) == 29


@pytest.mark.asyncio
async def test_source_backlog_cannot_starve_ai_or_notification(tmp_path: Path) -> None:
    queue, state = _runtime(tmp_path / "incident-operations.db")
    release_sources = asyncio.Event()
    ai_ran = asyncio.Event()
    notification_ran = asyncio.Event()

    async def source_handler(_job, _payload) -> None:
        await asyncio.wait_for(release_sources.wait(), timeout=1)

    async def ai_handler(_job, _payload) -> None:
        ai_ran.set()
        release_sources.set()

    async def notification_handler(_job, _payload) -> None:
        notification_ran.set()

    _enqueue_many(queue, kind="source.collect", pool=JobPool.SOURCE, count=32)
    _enqueue_many(queue, kind="investigation.plan", pool=JobPool.AI, count=1)
    _enqueue_many(queue, kind="notification.send", pool=JobPool.NOTIFICATION, count=1)
    runner = JobRunner(
        queue=queue,
        handlers=JobHandlerRegistry(
            {
                "source.collect": source_handler,
                "investigation.plan": ai_handler,
                "notification.send": notification_handler,
            }
        ),
        policies=JobPolicyRegistry(),
        state=state,
        budgets=ConcurrencyBudgets(),
        owner_prefix="fairness-test",
    )

    await asyncio.wait_for(runner.drain(), timeout=2)

    assert ai_ran.is_set()
    assert notification_ran.is_set()


@pytest.mark.asyncio
async def test_handler_runs_outside_claim_transaction_and_raw_error_is_not_stored(
    tmp_path: Path,
) -> None:
    queue, state = _runtime(tmp_path / "incident-operations.db")
    original = queue.enqueue(
        JobSpec(
            kind="source.collect",
            pool=JobPool.SOURCE,
            subject_type="source",
            subject_id="source-a",
            payload={},
            payload_revision=1,
            idempotency_key="original-job",
        )
    )

    async def handler(_job, _payload) -> None:
        queue.enqueue(
            JobSpec(
                kind="source.followup",
                pool=JobPool.SOURCE,
                subject_type="source",
                subject_id="source-b",
                payload={},
                payload_revision=1,
                idempotency_key="followup-job",
            )
        )
        raise RuntimeError("upstream token=must-never-be-stored")

    runner = JobRunner(
        queue=queue,
        handlers=JobHandlerRegistry({"source.collect": handler}),
        policies=JobPolicyRegistry(),
        state=state,
        budgets=ConcurrencyBudgets(source=1, ai=1, notification=1),
        owner_prefix="failure-test",
    )

    await runner.drain()

    failed = queue.get(original.id)
    assert failed.state is JobState.FAILED
    assert failed.safe_error_code == "JOB_HANDLER_FAILED"
    assert "token" not in str(failed)
    assert queue.count() == 2


@pytest.mark.asyncio
async def test_restart_reaps_an_expired_claim_before_running_new_work(
    tmp_path: Path,
) -> None:
    queue, state = _runtime(tmp_path / "incident-operations.db")
    started = datetime.now(UTC) - timedelta(minutes=2)
    job = queue.enqueue(
        JobSpec(
            kind="source.collect",
            pool=JobPool.SOURCE,
            subject_type="source",
            subject_id="source-a",
            payload={},
            payload_revision=1,
            idempotency_key="crashed-job",
            available_at=started,
        )
    )
    queue.claim_next(
        pool=JobPool.SOURCE,
        lease_owner="crashed-process",
        now=started,
        lease_seconds=1,
    )
    ran = asyncio.Event()

    async def handler(_job, _payload) -> None:
        ran.set()

    runner = JobRunner(
        queue=queue,
        handlers=JobHandlerRegistry({"source.collect": handler}),
        policies=JobPolicyRegistry(),
        state=state,
        budgets=ConcurrencyBudgets(source=1, ai=1, notification=1),
        owner_prefix="restarted-process",
    )

    await runner.drain()

    assert not ran.is_set()
    failed = queue.get(job.id)
    assert failed.state is JobState.FAILED
    assert failed.safe_error_code == "JOB_LEASE_EXPIRED"
