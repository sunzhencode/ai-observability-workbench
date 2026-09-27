"""F27 query budget: window derivation and every refusal path.

No network, no database. The point of these tests is that the budget **refuses**
rather than trims — a silently clipped chart is indistinguishable from a correct
one.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services.metric_budget import (
    LEAD_IN_SECONDS,
    MAX_QUERIES_PER_ALERT,
    MAX_RANGE_SECONDS,
    MAX_SERIES_PER_QUERY,
    MIN_STEP_SECONDS,
    STEP_LADDER,
    TARGET_POINTS,
    TRAIL_OUT_SECONDS,
    BudgetExceeded,
    BudgetReason,
    QueryWindow,
    assert_query_count_within_budget,
    assert_series_count_within_budget,
    assert_window_within_budget,
    ladder_step,
    resolve_window,
)

NOW = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# resolve_window
# --------------------------------------------------------------------------


def test_firing_alert_window_runs_from_lead_in_to_now() -> None:
    started = NOW - timedelta(minutes=30)
    window = resolve_window(alert_starts_at=started, alert_ends_at=None, now=NOW)
    assert window.start == started - timedelta(seconds=LEAD_IN_SECONDS)
    assert window.end == NOW


def test_resolved_alert_window_keeps_drawing_past_recovery() -> None:
    started = NOW - timedelta(hours=3)
    ended = NOW - timedelta(hours=1)
    window = resolve_window(alert_starts_at=started, alert_ends_at=ended, now=NOW)
    assert window.end == ended + timedelta(seconds=TRAIL_OUT_SECONDS)
    assert window.end < NOW


def test_trail_out_never_runs_past_now() -> None:
    """A very recent recovery must not ask the store for the future."""

    started = NOW - timedelta(hours=1)
    ended = NOW - timedelta(minutes=1)
    window = resolve_window(alert_starts_at=started, alert_ends_at=ended, now=NOW)
    assert window.end == NOW


def test_long_running_alert_is_clamped_to_the_range_ceiling() -> None:
    started = NOW - timedelta(days=9)
    window = resolve_window(alert_starts_at=started, alert_ends_at=None, now=NOW)
    assert window.span_seconds == MAX_RANGE_SECONDS
    assert window.end == NOW


def test_naive_timestamps_are_read_as_utc() -> None:
    """SQLite hands back naive datetimes; they must not be read as local time."""

    naive_started = datetime(2026, 7, 30, 11, 30)
    naive_now = datetime(2026, 7, 30, 12, 0)
    window = resolve_window(
        alert_starts_at=naive_started, alert_ends_at=None, now=naive_now
    )
    aware = resolve_window(
        alert_starts_at=naive_started.replace(tzinfo=timezone.utc),
        alert_ends_at=None,
        now=naive_now.replace(tzinfo=timezone.utc),
    )
    assert window == aware


def test_clock_skew_start_after_now_still_yields_a_positive_window() -> None:
    """The monitored cluster's clock can run ahead of this machine's."""

    started = NOW + timedelta(minutes=10)
    window = resolve_window(alert_starts_at=started, alert_ends_at=None, now=NOW)
    assert window.span_seconds > 0
    assert window.end > window.start


def test_just_fired_alert_gets_a_usable_window() -> None:
    """`startsAt == now` happens on the poll that first sees an alert.

    The window then extends slightly past `now`; that tail simply has no data
    points, which is preferable to a zero-width window that cannot be charted.
    """

    window = resolve_window(alert_starts_at=NOW, alert_ends_at=None, now=NOW)
    assert window.start == NOW - timedelta(seconds=LEAD_IN_SECONDS)
    assert window.end == NOW + timedelta(seconds=TRAIL_OUT_SECONDS)
    assert_window_within_budget(window)


def test_every_derived_window_passes_its_own_budget() -> None:
    """Whatever resolve_window produces must never be refused downstream."""

    for minutes_ago in (0, 1, 5, 59, 60, 600, 1440, 10_000, 60_000):
        window = resolve_window(
            alert_starts_at=NOW - timedelta(minutes=minutes_ago),
            alert_ends_at=None,
            now=NOW,
        )
        assert_window_within_budget(window)


# --------------------------------------------------------------------------
# ladder_step
# --------------------------------------------------------------------------


def test_step_is_always_a_ladder_rung() -> None:
    for span in (60, 3600, 7200, 43_200, MAX_RANGE_SECONDS):
        assert ladder_step(span) in STEP_LADDER


def test_step_never_falls_below_the_minimum() -> None:
    assert ladder_step(1) >= MIN_STEP_SECONDS
    assert ladder_step(0) == MIN_STEP_SECONDS
    assert ladder_step(-5) == MIN_STEP_SECONDS


def test_step_keeps_points_at_or_under_target() -> None:
    for span in (600, 3600, 7200, 43_200, MAX_RANGE_SECONDS):
        step = ladder_step(span)
        assert span / step <= TARGET_POINTS


def test_step_grows_monotonically_with_span() -> None:
    steps = [ladder_step(span) for span in (600, 7200, 43_200, MAX_RANGE_SECONDS)]
    assert steps == sorted(steps)


# --------------------------------------------------------------------------
# refusals
# --------------------------------------------------------------------------


def test_window_wider_than_the_ceiling_is_refused() -> None:
    window = QueryWindow(
        start=NOW - timedelta(seconds=MAX_RANGE_SECONDS + 1), end=NOW, step_seconds=900
    )
    with pytest.raises(BudgetExceeded) as excinfo:
        assert_window_within_budget(window)
    assert excinfo.value.reason is BudgetReason.RANGE_TOO_LONG


def test_step_below_the_floor_is_refused() -> None:
    window = QueryWindow(start=NOW - timedelta(minutes=5), end=NOW, step_seconds=1)
    with pytest.raises(BudgetExceeded) as excinfo:
        assert_window_within_budget(window)
    assert excinfo.value.reason is BudgetReason.STEP_TOO_SMALL


def test_too_many_points_is_refused() -> None:
    window = QueryWindow(
        start=NOW - timedelta(hours=6), end=NOW, step_seconds=MIN_STEP_SECONDS
    )
    with pytest.raises(BudgetExceeded) as excinfo:
        assert_window_within_budget(window)
    assert excinfo.value.reason is BudgetReason.TOO_MANY_POINTS


def test_empty_window_is_refused() -> None:
    window = QueryWindow(start=NOW, end=NOW, step_seconds=60)
    with pytest.raises(BudgetExceeded) as excinfo:
        assert_window_within_budget(window)
    assert excinfo.value.reason is BudgetReason.EMPTY_WINDOW


def test_inverted_window_is_refused() -> None:
    window = QueryWindow(start=NOW, end=NOW - timedelta(hours=1), step_seconds=60)
    with pytest.raises(BudgetExceeded) as excinfo:
        assert_window_within_budget(window)
    assert excinfo.value.reason is BudgetReason.EMPTY_WINDOW


def test_query_count_ceiling() -> None:
    assert_query_count_within_budget(MAX_QUERIES_PER_ALERT)
    with pytest.raises(BudgetExceeded) as excinfo:
        assert_query_count_within_budget(MAX_QUERIES_PER_ALERT + 1)
    assert excinfo.value.reason is BudgetReason.TOO_MANY_QUERIES


def test_series_ceiling_refuses_rather_than_trims() -> None:
    """Keeping the first 20 of 500 would draw a chart that means nothing."""

    assert_series_count_within_budget(MAX_SERIES_PER_QUERY)
    with pytest.raises(BudgetExceeded) as excinfo:
        assert_series_count_within_budget(MAX_SERIES_PER_QUERY + 1)
    assert excinfo.value.reason is BudgetReason.TOO_MANY_SERIES


def test_budget_reasons_are_distinct_strings() -> None:
    """Each reason gets its own UI message, so none may collide."""

    values = [reason.value for reason in BudgetReason]
    assert len(values) == len(set(values))


def test_exception_message_carries_the_reason() -> None:
    error = BudgetExceeded(BudgetReason.RANGE_TOO_LONG, "span=99s")
    assert "RANGE_TOO_LONG" in str(error)
    assert "span=99s" in str(error)
