"""Pure formulas and UTC range semantics for operational analytics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
import math
from typing import Iterable


UTC = timezone.utc


class AnalyticsRange(StrEnum):
    HOURS_24 = "24h"
    DAYS_7 = "7d"
    DAYS_30 = "30d"

    @property
    def duration(self) -> timedelta:
        return {
            AnalyticsRange.HOURS_24: timedelta(hours=24),
            AnalyticsRange.DAYS_7: timedelta(days=7),
            AnalyticsRange.DAYS_30: timedelta(days=30),
        }[self]


@dataclass(frozen=True, slots=True)
class AnalyticsWindow:
    range: AnalyticsRange
    start: datetime
    end: datetime


@dataclass(frozen=True, slots=True)
class DurationSummary:
    count: int
    median_ms: int | None
    p90_ms: int | None


@dataclass(frozen=True, slots=True)
class RatioSummary:
    numerator: int
    denominator: int
    ratio: float | None


def analytics_window(value: str | AnalyticsRange, *, now: datetime) -> AnalyticsWindow:
    """Return a rolling window ending at the latest complete UTC hour."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("ANALYTICS_NOW_UTC_REQUIRED")
    try:
        selected = value if isinstance(value, AnalyticsRange) else AnalyticsRange(value)
    except ValueError as exc:
        raise ValueError("ANALYTICS_RANGE_INVALID") from exc
    end = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    return AnalyticsWindow(selected, end - selected.duration, end)


def _nearest_rank(values: tuple[int, ...], percentile: float) -> int | None:
    if not values:
        return None
    rank = max(1, math.ceil(percentile * len(values)))
    return values[rank - 1]


def duration_summary(values: Iterable[int]) -> DurationSummary:
    normalized_values = tuple(int(value) for value in values)
    normalized = tuple(sorted(value for value in normalized_values if value >= 0))
    return DurationSummary(
        count=len(normalized),
        median_ms=_nearest_rank(normalized, 0.5),
        p90_ms=_nearest_rank(normalized, 0.9),
    )


def ratio_summary(numerator: int, denominator: int) -> RatioSummary:
    if numerator < 0 or denominator < 0 or numerator > denominator:
        raise ValueError("ANALYTICS_RATIO_INVALID")
    return RatioSummary(
        numerator=numerator,
        denominator=denominator,
        ratio=None if denominator == 0 else round(numerator / denominator, 6),
    )


def rate_summary(numerator: int, denominator: int) -> RatioSummary:
    """Return a rate whose numerator is not required to be a denominator subset."""
    if numerator < 0 or denominator < 0:
        raise ValueError("ANALYTICS_RATE_INVALID")
    return RatioSummary(
        numerator=numerator,
        denominator=denominator,
        ratio=None if denominator == 0 else round(numerator / denominator, 6),
    )
