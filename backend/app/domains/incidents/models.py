"""Pure current-product Incident, handling and history decisions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class HandlingState(StrEnum):
    NEW = "NEW"
    IN_PROGRESS = "IN_PROGRESS"
    CLOSED = "CLOSED"
    FALSE_POSITIVE = "FALSE_POSITIVE"


_HANDLING_TRANSITIONS = {
    HandlingState.NEW: frozenset(
        {HandlingState.IN_PROGRESS, HandlingState.CLOSED, HandlingState.FALSE_POSITIVE}
    ),
    HandlingState.IN_PROGRESS: frozenset(
        {HandlingState.CLOSED, HandlingState.FALSE_POSITIVE}
    ),
    HandlingState.CLOSED: frozenset(),
    HandlingState.FALSE_POSITIVE: frozenset(),
}


def can_change_handling(previous: str, target: str) -> bool:
    try:
        return HandlingState(target) in _HANDLING_TRANSITIONS[HandlingState(previous)]
    except ValueError:
        return False


@dataclass(frozen=True, slots=True)
class IncidentLifecycleSnapshot:
    source_state: str
    severity: str
    handling_state: str
    occurrence_no: int
    change_version: int


@dataclass(frozen=True, slots=True)
class IncidentLifecycleChange:
    incident_id: int
    occurrence_no: int
    previous_source_state: str | None
    current_source_state: str
    previous_severity: str | None
    current_severity: str
    previous_handling_state: str | None
    current_handling_state: str
    change_version: int
    origin: str
    observed_at: datetime


def is_recurrence(previous: IncidentLifecycleSnapshot | None, current_state: str, origin: str) -> bool:
    return (
        previous is not None
        and origin == "LIVE_POLL"
        and previous.source_state == "RECOVERED"
        and current_state == "FIRING"
    )


def should_seal_occurrence(
    change: IncidentLifecycleChange,
    *,
    complete_poll: bool,
    member_count: int,
) -> bool:
    return (
        complete_poll
        and change.origin == "LIVE_POLL"
        and change.previous_source_state not in {None, "RECOVERED"}
        and change.current_source_state == "RECOVERED"
        and member_count > 0
    )
