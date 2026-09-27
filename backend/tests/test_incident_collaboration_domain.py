"""Closed Incident task, Runbook-link and Note domain contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, cast

import pytest

from app.application.incidents import IncidentCollaborationCommands
from app.domains.incidents.actors import SystemActor
from app.domains.incidents.collaboration import (
    IncidentTaskDraft,
    IncidentTaskState,
    decide_task_transition,
    validate_note_text,
    validate_runbook_link,
)


def test_task_state_machine_requires_terminal_evidence_and_never_reopens() -> None:
    started = decide_task_transition(
        previous=IncidentTaskState.TODO,
        target=IncidentTaskState.IN_PROGRESS,
        result=None,
        reason=None,
    )
    assert started.target is IncidentTaskState.IN_PROGRESS

    done = decide_task_transition(
        previous=IncidentTaskState.IN_PROGRESS,
        target=IncidentTaskState.DONE,
        result="Replica caught up and error rate stayed below 1% for 10m",
        reason=None,
    )
    assert done.result.startswith("Replica")

    with pytest.raises(ValueError, match="RESULT_REQUIRED"):
        decide_task_transition(
            previous=IncidentTaskState.TODO,
            target=IncidentTaskState.DONE,
            result=None,
            reason=None,
        )
    with pytest.raises(ValueError, match="REASON_REQUIRED"):
        decide_task_transition(
            previous=IncidentTaskState.TODO,
            target=IncidentTaskState.CANCELED,
            result=None,
            reason=None,
        )
    with pytest.raises(ValueError, match="TRANSITION_INVALID"):
        decide_task_transition(
            previous=IncidentTaskState.DONE,
            target=IncidentTaskState.IN_PROGRESS,
            result=None,
            reason=None,
        )


def test_task_note_and_runbook_inputs_are_bounded_without_fetching_links() -> None:
    due = datetime(2026, 8, 20, 2, tzinfo=timezone.utc)
    draft = IncidentTaskDraft.validated(
        title="Verify database replication",
        description="Read the external runbook and record the observed result.",
        due_at=due,
        runbook_link="https://runbooks.internal.example/db/replication",
    )
    assert draft.due_at == due
    assert draft.runbook_link == "https://runbooks.internal.example/db/replication"
    assert validate_note_text("  Customer impact confirmed.  ") == "Customer impact confirmed."

    for value in (
        "http://runbooks.example/unsafe",
        "file:///tmp/runbook",
        "https://user:password@runbooks.example/secret",
    ):
        with pytest.raises(ValueError):
            validate_runbook_link(value)
    with pytest.raises(ValueError, match="NOTE_TEXT_INVALID"):
        validate_note_text("x" * 4001)


class _NeverCalledCollaborationPort:
    def __getattr__(self, name: str) -> Any:
        pytest.fail(f"system actor reached collaboration port: {name}")


def test_collaboration_commands_reject_system_actor_before_calling_port() -> None:
    commands = IncidentCollaborationCommands(
        cast(Any, _NeverCalledCollaborationPort())
    )
    actor = cast(Any, SystemActor("AI"))
    now = datetime(2026, 8, 20, 2, tzinfo=timezone.utc)
    draft = IncidentTaskDraft.validated(
        title="Verify recovery",
        description=None,
        due_at=None,
        runbook_link=None,
    )
    calls = (
        lambda: commands.create_task(
            1,
            draft=draft,
            actor=actor,
            idempotency_key="task-create-system-actor",
            request_id="request-1",
            source_ip="127.0.0.1",
            now=now,
        ),
        lambda: commands.transition_task(
            1,
            1,
            expected_version=1,
            target=IncidentTaskState.IN_PROGRESS,
            result=None,
            reason=None,
            actor=actor,
            idempotency_key="task-transition-system-actor",
            request_id="request-2",
            source_ip="127.0.0.1",
            now=now,
        ),
        lambda: commands.add_note(
            1,
            text="must not be written",
            actor=actor,
            idempotency_key="note-create-system-actor",
            request_id="request-3",
            source_ip="127.0.0.1",
            now=now,
        ),
        lambda: commands.redact_note(
            1,
            1,
            reason="must not be written",
            actor=actor,
            idempotency_key="note-redact-system-actor",
            request_id="request-4",
            source_ip="127.0.0.1",
            now=now,
        ),
    )
    for call in calls:
        with pytest.raises(PermissionError, match="INTERACTIVE_OPERATOR_REQUIRED"):
            call()
