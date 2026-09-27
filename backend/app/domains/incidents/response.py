"""Pure, closed Incident occurrence response-state decisions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.domains.incidents.actors import Actor, require_interactive_operator


class ResponseState(StrEnum):
    UNACKNOWLEDGED = "UNACKNOWLEDGED"
    IN_PROGRESS = "IN_PROGRESS"
    RESOLVED = "RESOLVED"


class ResolutionCode(StrEnum):
    FIXED = "FIXED"
    SELF_RECOVERED = "SELF_RECOVERED"
    FALSE_POSITIVE = "FALSE_POSITIVE"
    DUPLICATE = "DUPLICATE"
    NO_ACTION = "NO_ACTION"


class ResponseActionKind(StrEnum):
    START_HANDLING = "START_HANDLING"
    RESOLVE = "RESOLVE"


@dataclass(frozen=True, slots=True)
class ResponseAction:
    kind: ResponseActionKind
    resolution_code: ResolutionCode | None = None
    duplicate_of: int | None = None
    reason: str | None = None

    @classmethod
    def start_handling(cls, reason: str | None = None) -> ResponseAction:
        return cls(ResponseActionKind.START_HANDLING, reason=reason)

    @classmethod
    def resolve(
        cls,
        code: ResolutionCode,
        *,
        duplicate_of: int | None = None,
        reason: str | None = None,
    ) -> ResponseAction:
        return cls(
            ResponseActionKind.RESOLVE,
            resolution_code=code,
            duplicate_of=duplicate_of,
            reason=reason,
        )


@dataclass(frozen=True, slots=True)
class ResponseDecision:
    target_state: ResponseState
    resolution_code: ResolutionCode | None
    duplicate_of: int | None
    reason: str | None
    timeline_events: tuple[str, ...]


def _reason(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > 2000:
        raise ValueError("RESPONSE_REASON_INVALID")
    return normalized


def decide_response(
    *,
    previous: ResponseState,
    signal_state: str,
    action: ResponseAction,
    actor: Actor | object,
) -> ResponseDecision:
    require_interactive_operator(actor)
    reason = _reason(action.reason)

    if action.kind is ResponseActionKind.START_HANDLING:
        if previous is not ResponseState.UNACKNOWLEDGED:
            raise ValueError("RESPONSE_TRANSITION_INVALID")
        return ResponseDecision(
            ResponseState.IN_PROGRESS,
            None,
            None,
            reason,
            ("RESPONSE_HANDLING_STARTED",),
        )

    if action.kind is not ResponseActionKind.RESOLVE:
        raise ValueError("RESPONSE_TRANSITION_INVALID")
    if previous is not ResponseState.IN_PROGRESS:
        raise ValueError("RESPONSE_TRANSITION_INVALID")
    if action.resolution_code is None:
        raise ValueError("RESOLUTION_CODE_REQUIRED")
    if action.resolution_code is ResolutionCode.DUPLICATE:
        if action.duplicate_of is None or action.duplicate_of < 1:
            raise ValueError("DUPLICATE_TARGET_REQUIRED")
    elif action.duplicate_of is not None:
        raise ValueError("DUPLICATE_TARGET_NOT_ALLOWED")

    if (
        action.resolution_code
        in {ResolutionCode.FIXED, ResolutionCode.SELF_RECOVERED}
        and signal_state != "RECOVERED"
    ):
        raise ValueError("RESOLUTION_SIGNAL_STATE_INVALID")
    if signal_state != "RECOVERED" and reason is None:
        raise ValueError("RESPONSE_REASON_REQUIRED")

    return ResponseDecision(
        ResponseState.RESOLVED,
        action.resolution_code,
        action.duplicate_of,
        reason,
        ("OCCURRENCE_RESOLVED",),
    )
