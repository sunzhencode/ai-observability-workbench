"""Durable Job vocabulary shared by application use cases."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class JobPool(StrEnum):
    SOURCE = "SOURCE"
    AI = "AI"
    NOTIFICATION = "NOTIFICATION"


class JobState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


class LeaseExpiryAction(StrEnum):
    REQUEUE = "REQUEUE"
    FAIL = "FAIL"


@dataclass(frozen=True, slots=True)
class ConcurrencyBudgets:
    source: int = 8
    ai: int = 2
    notification: int = 4

    def __post_init__(self) -> None:
        if min(self.source, self.ai, self.notification) < 1:
            raise ValueError("Job concurrency budgets must be positive")

    def for_pool(self, pool: JobPool) -> int:
        return {
            JobPool.SOURCE: self.source,
            JobPool.AI: self.ai,
            JobPool.NOTIFICATION: self.notification,
        }[pool]


@dataclass(frozen=True, slots=True)
class JobSpec:
    kind: str
    pool: JobPool
    subject_type: str
    subject_id: str
    payload: dict[str, Any]
    payload_revision: int
    idempotency_key: str
    available_at: datetime | None = None

    def __post_init__(self) -> None:
        for name, value, maximum in (
            ("kind", self.kind, 80),
            ("subject_type", self.subject_type, 80),
            ("subject_id", self.subject_id, 128),
            ("idempotency_key", self.idempotency_key, 128),
        ):
            if not value or len(value) > maximum:
                raise ValueError(f"{name} must contain 1-{maximum} characters")
        if self.payload_revision < 1:
            raise ValueError("payload_revision must be positive")


@dataclass(frozen=True, slots=True)
class JobView:
    id: str
    kind: str
    pool: JobPool
    subject_type: str
    subject_id: str
    state: JobState
    payload: dict[str, Any]
    payload_revision: int
    idempotency_key: str
    attempt: int
    available_at: datetime
    lease_owner: str | None
    lease_expires_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    safe_error_code: str | None
    version: int
    created_at: datetime
    updated_at: datetime
