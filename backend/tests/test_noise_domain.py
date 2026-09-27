"""Closed deterministic noise rules and notification priority."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.domains.noise.models import (
    NoiseState,
    decide_notification_noise,
    flapping_state,
    storm_state,
    validate_grouping_window,
    validate_storm_thresholds,
    validate_suppression_duration,
)


UTC = timezone.utc


def test_closed_configuration_values() -> None:
    assert [validate_grouping_window(value) for value in (0, 30, 60, 120, 300)]
    assert validate_storm_thresholds(10, 5) == (10, 5)
    assert validate_storm_thresholds(10_000, 1_000) == (10_000, 1_000)
    assert [validate_suppression_duration(value) for value in (900, 3600, 14400, 86400)]
    with pytest.raises(ValueError, match="GROUPING_WINDOW_INVALID"):
        validate_grouping_window(45)
    with pytest.raises(ValueError, match="STORM_THRESHOLD_INVALID"):
        validate_storm_thresholds(9, 5)
    with pytest.raises(ValueError, match="SUPPRESSION_DURATION_INVALID"):
        validate_suppression_duration(1200)


def test_flapping_requires_four_recent_transitions_and_clears_after_stability() -> None:
    now = datetime(2026, 8, 24, 2, tzinfo=UTC)
    transitions = tuple(now - timedelta(minutes=value) for value in (12, 8, 4, 0))
    active = flapping_state(transitions, now=now, active_since=None)
    assert active == transitions[-1]
    assert flapping_state(transitions, now=now + timedelta(minutes=29), active_since=active) == active
    assert flapping_state(transitions, now=now + timedelta(minutes=30), active_since=active) is None


def test_storm_requires_two_quiet_windows_to_clear() -> None:
    assert storm_state(
        new_alerts=100,
        new_occurrences=1,
        alert_threshold=100,
        occurrence_threshold=20,
        was_active=False,
        below_half_windows=0,
    ) == (True, 0)
    assert storm_state(
        new_alerts=20,
        new_occurrences=2,
        alert_threshold=100,
        occurrence_threshold=20,
        was_active=True,
        below_half_windows=0,
    ) == (True, 1)
    assert storm_state(
        new_alerts=20,
        new_occurrences=2,
        alert_threshold=100,
        occurrence_threshold=20,
        was_active=True,
        below_half_windows=1,
    ) == (False, 2)


def test_notification_priority_and_grouping_delay_are_fixed() -> None:
    critical = decide_notification_noise(
        event_type="FIRING_OPENED",
        severity="critical",
        grouping_window_seconds=300,
        maintenance=True,
        suppression=True,
        storm=True,
        flapping=True,
    )
    assert critical.state is NoiseState.NONE

    maintenance = decide_notification_noise(
        event_type="FIRING_OPENED",
        severity="warning",
        grouping_window_seconds=300,
        maintenance=True,
        suppression=True,
        storm=True,
        flapping=True,
    )
    assert maintenance.state is NoiseState.MAINTENANCE and maintenance.suppress

    grouping = decide_notification_noise(
        event_type="FIRING_OPENED",
        severity="warning",
        grouping_window_seconds=120,
        maintenance=False,
        suppression=False,
        storm=False,
        flapping=False,
    )
    assert grouping.state is NoiseState.GROUPING
    assert grouping.delay_seconds == 120 and not grouping.suppress
