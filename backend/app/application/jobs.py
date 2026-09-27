"""Durable Job queue, explicit retry policy and independent pool runner."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol
from uuid import uuid4

from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from app.domains.operations.jobs import (
    ConcurrencyBudgets,
    JobPool,
    JobSpec,
    JobState,
    JobView,
    LeaseExpiryAction,
)
from app.platform.health import ReadinessProbe, ReadinessProbeResult
from app.platform.metrics import MetricRegistry

UTC = timezone.utc
JobHandler = Callable[[JobView, dict[str, object]], Awaitable[None]]


class JobStore(Protocol):
    def enqueue(self, spec: JobSpec, *, now: datetime) -> JobView: ...

    def get(self, job_id: str) -> JobView | None: ...

    def count(self, state: JobState | None) -> int: ...

    def claim_next(
        self,
        *,
        pool: JobPool,
        lease_owner: str,
        now: datetime,
        lease_expires_at: datetime,
    ) -> JobView | None: ...

    def finish(
        self,
        *,
        job_id: str,
        lease_owner: str,
        state: JobState,
        now: datetime,
        safe_error_code: str | None,
    ) -> JobView | None: ...

    def expired(self, *, now: datetime) -> tuple[JobView, ...]: ...

    def reap(self, *, job: JobView, requeue: bool, now: datetime) -> bool: ...


class LeaseLostError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ReapResult:
    requeued: int = 0
    failed: int = 0


@dataclass(frozen=True, slots=True)
class JobPolicyRegistry:
    lease_expiry: Mapping[str, LeaseExpiryAction] = field(default_factory=dict)

    def action_for(self, kind: str) -> LeaseExpiryAction:
        return self.lease_expiry.get(kind, LeaseExpiryAction.FAIL)


class JobHandlerRegistry:
    def __init__(self, handlers: Mapping[str, JobHandler] | None = None) -> None:
        self._handlers = dict(handlers or {})

    def get(self, kind: str) -> JobHandler | None:
        return self._handlers.get(kind)


class JobQueue:
    def __init__(self, store: JobStore) -> None:
        self._store = store

    def enqueue(self, spec: JobSpec) -> JobView:
        now = datetime.now(UTC)
        return self._store.enqueue(spec, now=now)

    def get(self, job_id: str) -> JobView:
        job = self._store.get(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def count(self, state: JobState | None = None) -> int:
        return self._store.count(state)

    def expired_count(self, *, now: datetime) -> int:
        return len(self._store.expired(now=now))

    def claim_next(
        self,
        *,
        pool: JobPool,
        lease_owner: str,
        now: datetime,
        lease_seconds: int,
    ) -> JobView | None:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        return self._store.claim_next(
            pool=pool,
            lease_owner=lease_owner,
            now=now,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
        )

    def succeed(self, job_id: str, *, lease_owner: str, now: datetime) -> JobView:
        return self._finish(
            job_id,
            lease_owner=lease_owner,
            now=now,
            state=JobState.SUCCEEDED,
            safe_error_code=None,
        )

    def fail(
        self,
        job_id: str,
        *,
        lease_owner: str,
        now: datetime,
        safe_error_code: str,
    ) -> JobView:
        return self._finish(
            job_id,
            lease_owner=lease_owner,
            now=now,
            state=JobState.FAILED,
            safe_error_code=safe_error_code,
        )

    def _finish(
        self,
        job_id: str,
        *,
        lease_owner: str,
        now: datetime,
        state: JobState,
        safe_error_code: str | None,
    ) -> JobView:
        job = self._store.finish(
            job_id=job_id,
            lease_owner=lease_owner,
            state=state,
            now=now,
            safe_error_code=safe_error_code,
        )
        if job is None:
            raise LeaseLostError("Job lease is no longer owned by this worker")
        return job

    def reap_expired(
        self,
        *,
        now: datetime,
        policies: JobPolicyRegistry,
    ) -> ReapResult:
        requeued = 0
        failed = 0
        for job in self._store.expired(now=now):
            requeue = policies.action_for(job.kind) is LeaseExpiryAction.REQUEUE
            if self._store.reap(job=job, requeue=requeue, now=now):
                if requeue:
                    requeued += 1
                else:
                    failed += 1
        return ReapResult(requeued=requeued, failed=failed)


@dataclass(slots=True)
class JobRunnerState:
    running: bool = False
    heartbeat_at: datetime | None = None

    def touch(self) -> None:
        self.heartbeat_at = datetime.now(UTC)


class JobRunner:
    def __init__(
        self,
        *,
        queue: JobQueue,
        handlers: JobHandlerRegistry,
        policies: JobPolicyRegistry,
        state: JobRunnerState,
        budgets: ConcurrencyBudgets,
        owner_prefix: str,
        lease_seconds: int = 30,
        idle_seconds: float = 0.1,
    ) -> None:
        self._queue = queue
        self._handlers = handlers
        self._policies = policies
        self.state = state
        self._budgets = budgets
        self._owner_prefix = owner_prefix
        self._lease_seconds = lease_seconds
        self._idle_seconds = idle_seconds
        self._background: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    async def _worker(self, pool: JobPool, ordinal: int) -> None:
        owner = f"{self._owner_prefix}:{pool.value.lower()}:{ordinal}:{uuid4().hex[:8]}"
        while True:
            job = self._queue.claim_next(
                pool=pool,
                lease_owner=owner,
                now=datetime.now(UTC),
                lease_seconds=self._lease_seconds,
            )
            if job is None:
                return
            self.state.touch()
            handler = self._handlers.get(job.kind)
            if handler is None:
                self._queue.fail(
                    job.id,
                    lease_owner=owner,
                    now=datetime.now(UTC),
                    safe_error_code="JOB_HANDLER_UNAVAILABLE",
                )
                continue
            try:
                await handler(job, job.payload)
            except Exception:  # noqa: BLE001 - only a stable code is persisted
                self._queue.fail(
                    job.id,
                    lease_owner=owner,
                    now=datetime.now(UTC),
                    safe_error_code="JOB_HANDLER_FAILED",
                )
            else:
                self._queue.succeed(job.id, lease_owner=owner, now=datetime.now(UTC))

    async def drain(self) -> None:
        self.state.running = True
        self.state.touch()
        self._queue.reap_expired(now=datetime.now(UTC), policies=self._policies)
        try:
            async with asyncio.TaskGroup() as group:
                for pool in JobPool:
                    for ordinal in range(self._budgets.for_pool(pool)):
                        group.create_task(self._worker(pool, ordinal))
        finally:
            self.state.touch()

    async def _run_forever(self) -> None:
        while not self._stopping.is_set():
            await self.drain()
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._idle_seconds)
            except TimeoutError:
                pass

    async def start(self) -> None:
        if self._background is not None:
            return
        self._stopping.clear()
        self.state.running = True
        self.state.touch()
        self._background = asyncio.create_task(self._run_forever())

    async def stop(self) -> None:
        self._stopping.set()
        if self._background is not None:
            await self._background
            self._background = None
        self.state.running = False
        self.state.touch()


@dataclass(slots=True)
class EnqueueOnlySchedulerState:
    running: bool = False
    heartbeat_at: datetime | None = None


class EnqueueOnlyScheduler:
    """A scheduling seam that can only create Jobs, never run handlers."""

    def __init__(self, queue: JobQueue, state: EnqueueOnlySchedulerState) -> None:
        self._queue = queue
        self.state = state
        self._scheduler = AsyncIOScheduler(timezone=UTC)

    def _heartbeat(self) -> None:
        self.state.heartbeat_at = datetime.now(UTC)

    def schedule_spec_supplier(
        self,
        *,
        identifier: str,
        seconds: int,
        supplier: Callable[[], tuple[JobSpec, ...]],
    ) -> None:
        """Register a bounded tick whose only mutation is durable enqueue."""
        if not identifier or not 1 <= seconds <= 3600:
            raise ValueError("scheduled enqueue interval is invalid")

        def enqueue_supplied() -> None:
            self._heartbeat()
            for spec in supplier():
                self._queue.enqueue(spec)

        self._scheduler.add_job(
            enqueue_supplied,
            trigger="interval",
            seconds=seconds,
            id=identifier,
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )

    async def start(self) -> None:
        if self._scheduler.running:
            return
        self._scheduler.add_job(
            self._heartbeat,
            trigger="interval",
            seconds=5,
            id="platform.scheduler.heartbeat",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        self._scheduler.start()
        self.state.running = True
        self._heartbeat()

    async def stop(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
        self.state.running = False
        self._heartbeat()

    def enqueue(self, spec: JobSpec) -> JobView:
        if not self.state.running:
            raise RuntimeError("scheduler is not running")
        self.state.heartbeat_at = datetime.now(UTC)
        return self._queue.enqueue(spec)


@dataclass(frozen=True, slots=True)
class JobOperationalThresholds:
    scheduler_delay_seconds: int = 30
    runner_heartbeat_seconds: int = 30
    pending_backlog_jobs: int = 100

    def __post_init__(self) -> None:
        if min(
            self.scheduler_delay_seconds,
            self.runner_heartbeat_seconds,
            self.pending_backlog_jobs,
        ) < 1:
            raise ValueError("operational thresholds must be positive")


def create_job_runtime_readiness_probes(
    *,
    queue: JobQueue,
    scheduler: EnqueueOnlySchedulerState,
    runner: JobRunnerState,
    metrics: MetricRegistry,
    thresholds: JobOperationalThresholds = JobOperationalThresholds(),
) -> tuple[ReadinessProbe, ReadinessProbe]:
    def refresh_alerts() -> tuple[bool, bool]:
        now = datetime.now(UTC)
        scheduler_delayed = (
            scheduler.heartbeat_at is None
            or (now - scheduler.heartbeat_at).total_seconds()
            > thresholds.scheduler_delay_seconds
        )
        runner_stale = (
            runner.heartbeat_at is None
            or (now - runner.heartbeat_at).total_seconds()
            > thresholds.runner_heartbeat_seconds
        )
        metrics.set_local_alert("SCHEDULER_DELAY", scheduler_delayed)
        metrics.set_local_alert("JOB_RUNNER_HEARTBEAT_STALE", runner_stale)
        metrics.set_local_alert(
            "JOB_BACKLOG_HIGH",
            queue.count(JobState.PENDING) >= thresholds.pending_backlog_jobs,
        )
        metrics.set_local_alert(
            "JOB_LEASE_EXPIRED",
            queue.expired_count(now=now) > 0,
        )
        return scheduler_delayed, runner_stale

    async def scheduler_check() -> ReadinessProbeResult:
        scheduler_delayed, _runner_stale = refresh_alerts()
        if not scheduler.running or scheduler_delayed:
            return ReadinessProbeResult(status="not_ready", code="SCHEDULER_NOT_RUNNING")
        return ReadinessProbeResult()

    async def runner_check() -> ReadinessProbeResult:
        _scheduler_delayed, runner_stale = refresh_alerts()
        if not runner.running or runner_stale:
            return ReadinessProbeResult(status="not_ready", code="JOB_RUNNER_NOT_RUNNING")
        return ReadinessProbeResult()

    return (
        ReadinessProbe(
            name="scheduler",
            check=scheduler_check,
            failure_code="SCHEDULER_READINESS_FAILED",
        ),
        ReadinessProbe(
            name="job_runner",
            check=runner_check,
            failure_code="JOB_RUNNER_READINESS_FAILED",
        ),
    )
