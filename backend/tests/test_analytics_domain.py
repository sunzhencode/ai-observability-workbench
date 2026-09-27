"""Fixed formulas for operational analytics."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.domains.analytics import (
    analytics_window,
    duration_summary,
    rate_summary,
    ratio_summary,
)


def test_ranges_end_at_latest_complete_utc_hour_independent_of_browser_timezone() -> None:
    china = timezone(timedelta(hours=8))
    instant = datetime(2026, 9, 8, 18, 47, tzinfo=china)

    seven_days = analytics_window("7d", now=instant)

    assert seven_days.end.isoformat() == "2026-09-08T10:00:00+00:00"
    assert seven_days.start.isoformat() == "2026-09-01T10:00:00+00:00"
    assert analytics_window("24h", now=instant).start.isoformat() == (
        "2026-09-07T10:00:00+00:00"
    )
    assert analytics_window("30d", now=instant).start.isoformat() == (
        "2026-08-09T10:00:00+00:00"
    )


def test_range_rejects_naive_time_and_unknown_value() -> None:
    with pytest.raises(ValueError, match="ANALYTICS_NOW_UTC_REQUIRED"):
        analytics_window("7d", now=datetime(2026, 9, 8, 10))
    with pytest.raises(ValueError, match="ANALYTICS_RANGE_INVALID"):
        analytics_window("90d", now=datetime(2026, 9, 8, 10, tzinfo=timezone.utc))


def test_duration_summary_uses_nearest_rank_without_merging_daily_percentiles() -> None:
    values = (9000, 1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000, 10_000)

    summary = duration_summary(values)

    assert summary.count == 10
    assert summary.median_ms == 5000
    assert summary.p90_ms == 9000
    assert duration_summary(()).median_ms is None


def test_ratio_keeps_formula_inputs_and_uses_na_for_empty_denominator() -> None:
    assert ratio_summary(3, 4).ratio == 0.75
    assert ratio_summary(0, 0).ratio is None
    with pytest.raises(ValueError, match="ANALYTICS_RATIO_INVALID"):
        ratio_summary(2, 1)
    assert rate_summary(6, 2).ratio == 3.0
