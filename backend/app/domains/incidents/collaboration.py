"""Pure Incident task, runbook-link and manual-note decisions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from urllib.parse import urlsplit


class IncidentTaskState(StrEnum):
    TODO = "TODO"
    IN_PROGRESS = "IN_PROGRESS"
    DONE = "DONE"
    CANCELED = "CANCELED"


_TASK_TRANSITIONS = {
    IncidentTaskState.TODO: frozenset(
        {
            IncidentTaskState.IN_PROGRESS,
            IncidentTaskState.DONE,
            IncidentTaskState.CANCELED,
        }
    ),
    IncidentTaskState.IN_PROGRESS: frozenset(
        {IncidentTaskState.DONE, IncidentTaskState.CANCELED}
    ),
    IncidentTaskState.DONE: frozenset(),
    IncidentTaskState.CANCELED: frozenset(),
}


def validate_runbook_link(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    parsed = urlsplit(normalized)
    if parsed.scheme.lower() != "https" or parsed.hostname is None:
        raise ValueError("RUNBOOK_LINK_HTTPS_REQUIRED")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("RUNBOOK_LINK_CREDENTIALS_NOT_ALLOWED")
    if len(normalized) > 2048:
        raise ValueError("RUNBOOK_LINK_TOO_LONG")
    return normalized


def validate_task_text(value: str, *, field: str, maximum: int) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"INCIDENT_TASK_{field}_INVALID")
    return normalized


def validate_optional_task_text(
    value: str | None, *, field: str, maximum: int
) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if len(normalized) > maximum:
        raise ValueError(f"INCIDENT_TASK_{field}_INVALID")
    return normalized or None


def validate_note_text(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 4000:
        raise ValueError("INCIDENT_NOTE_TEXT_INVALID")
    return normalized


@dataclass(frozen=True, slots=True)
class TaskTransitionDecision:
    target: IncidentTaskState
    result: str | None
    reason: str | None


def decide_task_transition(
    *,
    previous: IncidentTaskState,
    target: IncidentTaskState,
    result: str | None,
    reason: str | None,
) -> TaskTransitionDecision:
    if target not in _TASK_TRANSITIONS[previous]:
        raise ValueError("INCIDENT_TASK_TRANSITION_INVALID")
    normalized_result = validate_optional_task_text(
        result, field="RESULT", maximum=4000
    )
    normalized_reason = validate_optional_task_text(
        reason, field="REASON", maximum=2000
    )
    if target is IncidentTaskState.DONE and normalized_result is None:
        raise ValueError("INCIDENT_TASK_RESULT_REQUIRED")
    if target is IncidentTaskState.CANCELED and normalized_reason is None:
        raise ValueError("INCIDENT_TASK_REASON_REQUIRED")
    if target is not IncidentTaskState.DONE and normalized_result is not None:
        raise ValueError("INCIDENT_TASK_RESULT_NOT_ALLOWED")
    if target is not IncidentTaskState.CANCELED and normalized_reason is not None:
        raise ValueError("INCIDENT_TASK_REASON_NOT_ALLOWED")
    return TaskTransitionDecision(target, normalized_result, normalized_reason)


@dataclass(frozen=True, slots=True)
class IncidentTaskDraft:
    title: str
    description: str | None
    due_at: datetime | None
    runbook_link: str | None

    @classmethod
    def validated(
        cls,
        *,
        title: str,
        description: str | None,
        due_at: datetime | None,
        runbook_link: str | None,
    ) -> IncidentTaskDraft:
        normalized_due = due_at
        if due_at is not None:
            if due_at.tzinfo is None or due_at.utcoffset() is None:
                raise ValueError("INCIDENT_TASK_DUE_AT_UTC_REQUIRED")
            normalized_due = due_at.astimezone(timezone.utc)
        return cls(
            title=validate_task_text(title, field="TITLE", maximum=200),
            description=validate_optional_task_text(
                description, field="DESCRIPTION", maximum=4000
            ),
            due_at=normalized_due,
            runbook_link=validate_runbook_link(runbook_link),
        )
