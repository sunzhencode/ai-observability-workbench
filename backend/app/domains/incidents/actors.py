"""Closed actor capabilities for Incident response commands."""

from __future__ import annotations

from dataclasses import dataclass


_INTERACTIVE_MINT = object()


@dataclass(frozen=True, slots=True, init=False)
class InteractiveOperatorActor:
    """Proof that a command originated at the interactive HTTP adapter."""

    kind: str

    def __init__(self, mint: object) -> None:
        if mint is not _INTERACTIVE_MINT:
            raise PermissionError("INTERACTIVE_OPERATOR_WITNESS_REQUIRED")
        object.__setattr__(self, "kind", "INTERACTIVE_OPERATOR")


@dataclass(frozen=True, slots=True)
class SystemActor:
    kind: str

    def __post_init__(self) -> None:
        if self.kind not in {"POLL", "WORKER", "AI", "SCHEDULER", "NOTIFICATION", "SYSTEM"}:
            raise ValueError("SYSTEM_ACTOR_KIND_INVALID")


def _mint_interactive_operator() -> InteractiveOperatorActor:
    return InteractiveOperatorActor(_INTERACTIVE_MINT)


Actor = InteractiveOperatorActor | SystemActor


def require_interactive_operator(actor: Actor | object) -> InteractiveOperatorActor:
    """Reject every actor that was not minted by the interactive HTTP witness."""

    if type(actor) is not InteractiveOperatorActor:
        raise PermissionError("INTERACTIVE_OPERATOR_REQUIRED")
    return actor
