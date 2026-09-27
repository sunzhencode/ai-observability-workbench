"""Pure current-Incident lifecycle and handling contracts."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.domains.incidents.models import (
    IncidentLifecycleChange,
    IncidentLifecycleSnapshot,
    can_change_handling,
    is_recurrence,
    should_seal_occurrence,
)


UTC = timezone.utc


@pytest.mark.parametrize(
    ("previous", "target", "allowed"),
    [
        ("NEW", "IN_PROGRESS", True),
        ("NEW", "CLOSED", True),
        ("NEW", "FALSE_POSITIVE", True),
        ("IN_PROGRESS", "CLOSED", True),
        ("IN_PROGRESS", "FALSE_POSITIVE", True),
        ("CLOSED", "IN_PROGRESS", False),
        ("FALSE_POSITIVE", "NEW", False),
        ("NEW", "NEW", False),
        ("UNKNOWN", "CLOSED", False),
    ],
)
def test_current_handling_transition_matrix(
    previous: str,
    target: str,
    allowed: bool,
) -> None:
    assert can_change_handling(previous, target) is allowed


def test_only_positive_live_poll_reopens_a_recovered_incident() -> None:
    previous = IncidentLifecycleSnapshot("RECOVERED", "warning", "CLOSED", 3, 8)

    assert is_recurrence(previous, "FIRING", "LIVE_POLL") is True
    assert is_recurrence(previous, "PENDING", "LIVE_POLL") is False
    assert is_recurrence(previous, "UNKNOWN", "LIVE_POLL") is False
    assert is_recurrence(previous, "FIRING", "BACKFILL") is False
    assert is_recurrence(previous, "FIRING", "REGROUP") is False
    assert is_recurrence(None, "FIRING", "LIVE_POLL") is False


def _change(
    *,
    previous: str | None = "FIRING",
    current: str = "RECOVERED",
    origin: str = "LIVE_POLL",
) -> IncidentLifecycleChange:
    return IncidentLifecycleChange(
        incident_id=7,
        occurrence_no=2,
        previous_source_state=previous,
        current_source_state=current,
        previous_severity="warning",
        current_severity="warning",
        previous_handling_state="IN_PROGRESS",
        current_handling_state="IN_PROGRESS",
        change_version=4,
        origin=origin,
        observed_at=datetime(2026, 8, 13, tzinfo=UTC),
    )


def test_occurrence_seals_only_for_a_real_complete_live_recovery() -> None:
    assert should_seal_occurrence(_change(), complete_poll=True, member_count=2)

    assert not should_seal_occurrence(_change(), complete_poll=False, member_count=2)
    assert not should_seal_occurrence(_change(origin="BACKFILL"), complete_poll=True, member_count=2)
    assert not should_seal_occurrence(_change(previous=None), complete_poll=True, member_count=2)
    assert not should_seal_occurrence(_change(previous="RECOVERED"), complete_poll=True, member_count=2)
    assert not should_seal_occurrence(_change(current="FIRING"), complete_poll=True, member_count=2)
    assert not should_seal_occurrence(_change(), complete_poll=True, member_count=0)
