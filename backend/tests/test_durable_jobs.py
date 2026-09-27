"""Durable Job queue, lease and idempotency contracts."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.adapters.persistence.jobs import SqlAlchemyJobStore
from app.application.jobs import JobPolicyRegistry, JobQueue, LeaseLostError
from app.domains.operations.jobs import (
    JobPool,
    JobSpec,
    JobState,
    LeaseExpiryAction,
)
from app.platform.persistence.database import (
    SqliteDatabaseConfig,
    create_session_factory,
    create_sqlite_engine,
)
from app.platform.persistence.migrations import upgrade_database

UTC = timezone.utc


def _queue(path: Path) -> JobQueue:
    engine = create_sqlite_engine(SqliteDatabaseConfig(path=path))
    upgrade_database(engine)
    return JobQueue(SqlAlchemyJobStore(create_session_factory(engine)))


def _spec(
    *,
    kind: str = "source.collect",
    pool: JobPool = JobPool.SOURCE,
    key: str = "request-0001",
    available_at: datetime | None = None,
) -> JobSpec:
    return JobSpec(
        kind=kind,
        pool=pool,
        subject_type="event_source",
        subject_id="source-a",
        payload={"source_id": "source-a"},
        payload_revision=1,
        idempotency_key=key,
        available_at=available_at,
    )


def test_enqueue_is_idempotent_per_kind_and_key(tmp_path: Path) -> None:
    queue = _queue(tmp_path / "incident-operations.db")

    first = queue.enqueue(_spec())
    duplicate = queue.enqueue(_spec())
    other_kind = queue.enqueue(_spec(kind="source.refresh"))

    assert duplicate.id == first.id
    assert other_kind.id != first.id
    assert queue.count() == 2
    assert first.state is JobState.PENDING
    assert first.version == 1


def test_claim_is_atomic_and_only_available_jobs_can_run(tmp_path: Path) -> None:
    queue = _queue(tmp_path / "incident-operations.db")
    now = datetime(2026, 8, 11, 3, 0, tzinfo=UTC)
    future = queue.enqueue(_spec(key="request-future", available_at=now + timedelta(minutes=1)))
    ready = queue.enqueue(_spec(key="request-ready", available_at=now))

    assert queue.claim_next(
        pool=JobPool.SOURCE,
        lease_owner="worker-a",
        now=now,
        lease_seconds=30,
    ) == queue.get(ready.id)
    assert queue.get(future.id).state is JobState.PENDING

    claimed = queue.get(ready.id)
    assert claimed.state is JobState.RUNNING
    assert claimed.attempt == 1
    assert claimed.lease_owner == "worker-a"
    assert claimed.lease_expires_at == now + timedelta(seconds=30)


def test_competing_workers_cannot_claim_the_same_job(tmp_path: Path) -> None:
    database = tmp_path / "incident-operations.db"
    queue = _queue(database)
    now = datetime(2026, 8, 11, 3, 0, tzinfo=UTC)
    queued = queue.enqueue(_spec(available_at=now))

    def claim(owner: str):
        return queue.claim_next(
            pool=JobPool.SOURCE,
            lease_owner=owner,
            now=now,
            lease_seconds=30,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, ("worker-a", "worker-b")))

    claimed = [item for item in results if item is not None]
    assert len(claimed) == 1
    assert claimed[0].id == queued.id


def test_only_the_lease_owner_can_finish_a_running_job(tmp_path: Path) -> None:
    queue = _queue(tmp_path / "incident-operations.db")
    now = datetime(2026, 8, 11, 3, 0, tzinfo=UTC)
    job = queue.enqueue(_spec(available_at=now))
    queue.claim_next(
        pool=JobPool.SOURCE,
        lease_owner="worker-a",
        now=now,
        lease_seconds=30,
    )

    with pytest.raises(LeaseLostError):
        queue.succeed(job.id, lease_owner="worker-b", now=now)

    finished = queue.succeed(job.id, lease_owner="worker-a", now=now)
    assert finished.state is JobState.SUCCEEDED
    assert finished.finished_at == now
    assert finished.safe_error_code is None


def test_reaper_uses_explicit_kind_policy_and_never_guesses_retry(
    tmp_path: Path,
) -> None:
    queue = _queue(tmp_path / "incident-operations.db")
    started = datetime(2026, 8, 11, 3, 0, tzinfo=UTC)
    source = queue.enqueue(_spec(kind="source.collect", key="source-key", available_at=started))
    ai = queue.enqueue(
        _spec(
            kind="investigation.plan",
            pool=JobPool.AI,
            key="ai-request",
            available_at=started,
        )
    )
    queue.claim_next(
        pool=JobPool.SOURCE,
        lease_owner="old-source",
        now=started,
        lease_seconds=1,
    )
    queue.claim_next(
        pool=JobPool.AI,
        lease_owner="old-ai",
        now=started,
        lease_seconds=1,
    )
    policies = JobPolicyRegistry(
        lease_expiry={"source.collect": LeaseExpiryAction.REQUEUE}
    )

    result = queue.reap_expired(now=started + timedelta(seconds=2), policies=policies)

    assert result.requeued == 1
    assert result.failed == 1
    assert queue.get(source.id).state is JobState.PENDING
    failed_ai = queue.get(ai.id)
    assert failed_ai.state is JobState.FAILED
    assert failed_ai.safe_error_code == "JOB_LEASE_EXPIRED"
