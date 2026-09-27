"""Interactive adapter witness; production callers must not import this module."""

from __future__ import annotations

from app.domains.incidents.actors import (
    InteractiveOperatorActor,
    _mint_interactive_operator,
)


class OperatorWitness:
    """Mint the capability used by response commands for this HTTP request."""

    __slots__ = ()

    def actor(self) -> InteractiveOperatorActor:
        return _mint_interactive_operator()
