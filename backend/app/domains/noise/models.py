"""Pure deterministic noise decisions; no persistence or network access."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum


GROUPING_WINDOWS = frozenset({0, 30, 60, 120, 300})
SUPPRESSION_DURATIONS = frozenset({15 * 60, 60 * 60, 4 * 60 * 60, 24 * 60 * 60})
MAX_MAINTENANCE_SECONDS = 7 * 24 * 60 * 60
FLAPPING_WINDOW = timedelta(minutes=15)
FLAPPING_TRANSITIONS = 4
FLAPPING_CLEAR_AFTER = timedelta(minutes=30)
STORM_WINDOW = timedelta(minutes=5)


class NoiseState(StrEnum):
    NONE = "NONE"
    GROUPING = "GROUPING"
    FLAPPING = "FLAPPING"
    STORM = "STORM"
    MAINTENANCE = "MAINTENANCE"
    SUPPRESSED = "SUPPRESSED"


@dataclass(frozen=True, slots=True)
class NotificationNoiseDecision:
    state: NoiseState
    reason_code: str | None
    delay_seconds: int = 0
    suppress: bool = False
    summary_alert_count: int | None = None
    summary_occurrence_count: int | None = None


def validate_grouping_window(value: int) -> int:
    if value not in GROUPING_WINDOWS:
        raise ValueError("GROUPING_WINDOW_INVALID")
    return value


def validate_storm_thresholds(alerts: int, occurrences: int) -> tuple[int, int]:
    if not 10 <= alerts <= 10_000 or not 5 <= occurrences <= 1_000:
        raise ValueError("STORM_THRESHOLD_INVALID")
    return alerts, occurrences


def validate_suppression_duration(value: int) -> int:
    if value not in SUPPRESSION_DURATIONS:
        raise ValueError("SUPPRESSION_DURATION_INVALID")
    return value


def validate_window(*, starts_at: datetime, ends_at: datetime, max_seconds: int) -> None:
    duration = (ends_at - starts_at).total_seconds()
    if duration <= 0 or duration > max_seconds:
        raise ValueError("NOISE_WINDOW_INVALID")


def flapping_state(
    transitions: tuple[datetime, ...],
    *,
    now: datetime,
    active_since: datetime | None,
) -> datetime | None:
    recent = tuple(item for item in transitions if now - item <= FLAPPING_WINDOW)
    if len(recent) >= FLAPPING_TRANSITIONS:
        return active_since or recent[-1]
    if active_since is not None and transitions:
        if now - transitions[-1] < FLAPPING_CLEAR_AFTER:
            return active_since
    return None


def storm_state(
    *,
    new_alerts: int,
    new_occurrences: int,
    alert_threshold: int,
    occurrence_threshold: int,
    was_active: bool,
    below_half_windows: int,
) -> tuple[bool, int]:
    validate_storm_thresholds(alert_threshold, occurrence_threshold)
    triggered = new_alerts >= alert_threshold or new_occurrences >= occurrence_threshold
    if triggered:
        return True, 0
    if not was_active:
        return False, 0
    below_half = (
        new_alerts < alert_threshold * 0.5
        and new_occurrences < occurrence_threshold * 0.5
    )
    next_streak = below_half_windows + 1 if below_half else 0
    return next_streak < 2, next_streak


def decide_notification_noise(
    *,
    event_type: str,
    severity: str,
    grouping_window_seconds: int,
    maintenance: bool,
    suppression: bool,
    storm: bool,
    flapping: bool,
) -> NotificationNoiseDecision:
    """Apply the fixed product priority without consulting external systems."""
    validate_grouping_window(grouping_window_seconds)
    if severity.lower() == "critical" and event_type in {
        "FIRING_OPENED",
        "SEVERITY_ESCALATED",
    }:
        return NotificationNoiseDecision(NoiseState.NONE, None)
    if maintenance:
        return NotificationNoiseDecision(
            NoiseState.MAINTENANCE, "MAINTENANCE_WINDOW_ACTIVE", suppress=True
        )
    if suppression:
        return NotificationNoiseDecision(
            NoiseState.SUPPRESSED, "OCCURRENCE_SUPPRESSION_ACTIVE", suppress=True
        )
    if storm:
        return NotificationNoiseDecision(
            NoiseState.STORM, "SOURCE_STORM_AGGREGATED", suppress=True
        )
    if flapping:
        return NotificationNoiseDecision(
            NoiseState.FLAPPING, "ALERT_FLAPPING_AGGREGATED", suppress=True
        )
    if event_type == "FIRING_OPENED" and grouping_window_seconds:
        return NotificationNoiseDecision(
            NoiseState.GROUPING,
            "GROUPING_WINDOW_ACTIVE",
            delay_seconds=grouping_window_seconds,
        )
    return NotificationNoiseDecision(NoiseState.NONE, None)
