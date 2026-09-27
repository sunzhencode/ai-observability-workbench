"""Closed three-state response and operator-capability contracts."""

from __future__ import annotations

import pytest

from app.api.v1.operator_witness import OperatorWitness
from app.domains.incidents.actors import InteractiveOperatorActor, SystemActor
from app.domains.incidents.response import (
    ResolutionCode,
    ResponseAction,
    ResponseState,
    decide_response,
)


class _WrongActor:
    pass


@pytest.fixture
def operator_actor():
    return OperatorWitness().actor()


def test_response_lifecycle_has_only_one_start_and_one_finish(
    operator_actor,
) -> None:
    started = decide_response(
        previous=ResponseState.UNACKNOWLEDGED,
        signal_state="FIRING",
        action=ResponseAction.start_handling("scope checked"),
        actor=operator_actor,
    )
    assert started.target_state is ResponseState.IN_PROGRESS
    assert started.timeline_events == ("RESPONSE_HANDLING_STARTED",)

    with pytest.raises(ValueError, match="RESPONSE_TRANSITION_INVALID"):
        decide_response(
            previous=ResponseState.IN_PROGRESS,
            signal_state="FIRING",
            action=ResponseAction.start_handling(),
            actor=operator_actor,
        )

    with pytest.raises(ValueError, match="RESPONSE_TRANSITION_INVALID"):
        decide_response(
            previous=ResponseState.UNACKNOWLEDGED,
            signal_state="RECOVERED",
            action=ResponseAction.resolve(ResolutionCode.SELF_RECOVERED),
            actor=operator_actor,
        )


def test_resolution_code_must_match_signal_fact(operator_actor) -> None:
    for code in (ResolutionCode.FIXED, ResolutionCode.SELF_RECOVERED):
        with pytest.raises(ValueError, match="RESOLUTION_SIGNAL_STATE_INVALID"):
            decide_response(
                previous=ResponseState.IN_PROGRESS,
                signal_state="FIRING",
                action=ResponseAction.resolve(code, reason="operator assertion"),
                actor=operator_actor,
            )

    with pytest.raises(ValueError, match="RESPONSE_REASON_REQUIRED"):
        decide_response(
            previous=ResponseState.IN_PROGRESS,
            signal_state="FIRING",
            action=ResponseAction.resolve(ResolutionCode.NO_ACTION),
            actor=operator_actor,
        )

    decision = decide_response(
        previous=ResponseState.IN_PROGRESS,
        signal_state="RECOVERED",
        action=ResponseAction.resolve(ResolutionCode.FIXED),
        actor=operator_actor,
    )
    assert decision.target_state is ResponseState.RESOLVED
    assert decision.timeline_events == ("OCCURRENCE_RESOLVED",)

    with pytest.raises(ValueError, match="DUPLICATE_TARGET_REQUIRED"):
        decide_response(
            previous=ResponseState.IN_PROGRESS,
            signal_state="RECOVERED",
            action=ResponseAction.resolve(ResolutionCode.DUPLICATE),
            actor=operator_actor,
        )


def test_system_or_forged_actor_cannot_advance_response(operator_actor) -> None:
    with pytest.raises(PermissionError, match="INTERACTIVE_OPERATOR_WITNESS_REQUIRED"):
        InteractiveOperatorActor(object())
    for actor in (SystemActor("POLL"), _WrongActor()):
        with pytest.raises(PermissionError, match="INTERACTIVE_OPERATOR_REQUIRED"):
            decide_response(
                previous=ResponseState.UNACKNOWLEDGED,
                signal_state="FIRING",
                action=ResponseAction.start_handling(),
                actor=actor,
            )
