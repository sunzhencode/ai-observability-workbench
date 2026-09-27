"""One bounded retention interface for the Incident Operations scheduler."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Mapping, Protocol


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    invalid_raw_days: int = 7
    poll_detail_days: int = 7
    poll_runs_per_source: int = 200
    runtime_days: int = 30
    investigation_days: int = 90
    history_days: int = 365
    batch_rows: int = 500
    max_passes_per_day: int = 20

    def __post_init__(self) -> None:
        horizons = (
            self.invalid_raw_days,
            self.poll_detail_days,
            self.poll_runs_per_source,
            self.runtime_days,
            self.investigation_days,
            self.history_days,
        )
        if min(horizons) < 1:
            raise ValueError("RETENTION_POLICY_INVALID")
        if not 1 <= self.batch_rows <= 500:
            raise ValueError("RETENTION_POLICY_INVALID")
        if not 1 <= self.max_passes_per_day <= 20:
            raise ValueError("RETENTION_POLICY_INVALID")


@dataclass(frozen=True, slots=True)
class RetentionPass:
    as_of: datetime
    pass_number: int
    rollups_complete: bool

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> RetentionPass:
        as_of_raw = payload.get("as_of")
        pass_number = payload.get("pass_number")
        rollups_complete = payload.get("rollups_complete")
        if (
            not isinstance(as_of_raw, str)
            or isinstance(pass_number, bool)
            or not isinstance(pass_number, int)
            or not isinstance(rollups_complete, bool)
        ):
            raise ValueError("RETENTION_PASS_INVALID")
        try:
            as_of = datetime.fromisoformat(as_of_raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("RETENTION_PASS_INVALID") from exc
        return cls(
            as_of=as_of,
            pass_number=pass_number,
            rollups_complete=rollups_complete,
        )

    def to_payload(self) -> dict[str, object]:
        if self.as_of.tzinfo is None or self.as_of.utcoffset() is None:
            raise ValueError("RETENTION_PASS_INVALID")
        return {
            "as_of": self.as_of.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "pass_number": self.pass_number,
            "rollups_complete": self.rollups_complete,
        }

    @property
    def idempotency_key(self) -> str:
        if self.as_of.tzinfo is None or self.as_of.utcoffset() is None:
            raise ValueError("RETENTION_PASS_INVALID")
        return (
            "operations-retention-v3:"
            f"{self.as_of.astimezone(UTC).date().isoformat()}:{self.pass_number}"
        )


@dataclass(frozen=True, slots=True)
class RetentionBatchResult:
    deleted_by_table: Mapping[str, int]
    has_more: bool


@dataclass(frozen=True, slots=True)
class RetentionPassResult:
    deleted_by_table: Mapping[str, int]
    has_more: bool
    next_pass: RetentionPass | None


class RetentionPersistence(Protocol):
    def prepare_rollups(
        self, *, as_of: datetime, policy: RetentionPolicy
    ) -> None: ...

    def cleanup_batch(
        self, *, as_of: datetime, policy: RetentionPolicy
    ) -> RetentionBatchResult: ...


class RetentionCoordinator:
    """Hide rollup ordering, deletion policy and continuation decisions."""

    def __init__(
        self,
        persistence: RetentionPersistence,
        *,
        policy: RetentionPolicy | None = None,
    ) -> None:
        self._persistence = persistence
        self._policy = policy or RetentionPolicy()

    def run_pass(self, request: RetentionPass) -> RetentionPassResult:
        if (
            request.as_of.tzinfo is None
            or request.as_of.utcoffset() is None
            or request.pass_number < 0
            or request.pass_number >= self._policy.max_passes_per_day
            or (request.pass_number > 0 and not request.rollups_complete)
        ):
            raise ValueError("RETENTION_PASS_INVALID")
        if not request.rollups_complete:
            self._persistence.prepare_rollups(
                as_of=request.as_of,
                policy=self._policy,
            )
        batch = self._persistence.cleanup_batch(
            as_of=request.as_of,
            policy=self._policy,
        )
        next_number = request.pass_number + 1
        next_pass = (
            RetentionPass(request.as_of, next_number, True)
            if batch.has_more and next_number < self._policy.max_passes_per_day
            else None
        )
        return RetentionPassResult(
            deleted_by_table=dict(batch.deleted_by_table),
            has_more=batch.has_more,
            next_pass=next_pass,
        )


__all__ = [
    "RetentionBatchResult",
    "RetentionCoordinator",
    "RetentionPass",
    "RetentionPassResult",
    "RetentionPersistence",
    "RetentionPolicy",
]
