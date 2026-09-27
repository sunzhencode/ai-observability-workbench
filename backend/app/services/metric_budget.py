"""F27 query budget: code constants, no configuration surface, no off switch.

Read-only is not the same as harmless. One `query_range` with a wide window, a
small step and high cardinality is enough to stall the upstream store, so every
call passes through here first — the same shape as `providers/egress.py`
(CAP-12.3 / design §2).

Exceeding a limit is a **readable failure**, never a silent truncation: a chart
quietly clipped to the last hour looks exactly like a chart that is telling the
truth.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

# --- the budget itself -----------------------------------------------------

MAX_RANGE_SECONDS = 24 * 3600
"""The widest window we will ever ask for.

The question this capability answers is "how did *this* alert build up", not
"how did last month compare". A day covers the former; anything wider mostly
buys resolution loss."""

MIN_STEP_SECONDS = 15
"""Below a typical scrape interval a smaller step only multiplies points."""

TARGET_POINTS = 240
"""~600px of SVG at ~2.5px per point. More points than pixels help nobody."""

STEP_LADDER: tuple[int, ...] = (15, 30, 60, 300, 900, 3600)
"""Fixed rungs so a chart never reports a step like "37s", which reads as noise."""

MAX_SERIES_PER_QUERY = 20
"""Above this a chart is unreadable anyway, and it signals a cardinality mistake."""

MAX_QUERIES_PER_ALERT = 6
"""One primary curve plus at most five auxiliary ones."""

MAX_PROBES_PER_IMPORT = 40
"""How many candidate queries one dashboard import may check against the store.

A separate constant because every limit above is per-*alert-chart* and none of
them fits: `MAX_QUERIES_PER_ALERT` would kill a 40-panel import at the seventh
candidate, leaving the feature technically alive and practically useless, while
no limit at all makes one click an unbounded burst at the upstream.

Exceeding it is not a failure of the query. Candidates past the limit are
reported as **unverified** — never as broken, and never as working."""

MAX_PROBE_CONCURRENCY = 4
"""Enough to keep an import responsive, low enough not to look like a scrape."""

PROBE_TIMEOUT_SECONDS = 5.0
"""Answering "does this run at all" needs far less patience than drawing a
chart, and the import runs dozens of them behind a single click."""

REQUEST_TIMEOUT_SECONDS = 15.0
"""Matches the existing ThanosClient default."""

LEAD_IN_SECONDS = 2 * 3600
"""How far before the alert fired to look.

Two hours is enough to tell "fell off a cliff" from "slid down all morning",
which is the distinction that changes what you do next. Provisional: confirm it
against real data (tasks 1.8)."""

TRAIL_OUT_SECONDS = 1800
"""How far past a resolved alert to keep drawing, so recovery is visible."""


class BudgetReason(str, Enum):
    """Why a query was refused. Each maps to its own message in the UI."""

    RANGE_TOO_LONG = "RANGE_TOO_LONG"
    STEP_TOO_SMALL = "STEP_TOO_SMALL"
    TOO_MANY_POINTS = "TOO_MANY_POINTS"
    TOO_MANY_SERIES = "TOO_MANY_SERIES"
    TOO_MANY_QUERIES = "TOO_MANY_QUERIES"
    EMPTY_WINDOW = "EMPTY_WINDOW"


class BudgetExceeded(Exception):
    """Raised before a request leaves, never after results are trimmed."""

    def __init__(self, reason: BudgetReason, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason.value}: {detail}" if detail else reason.value)


@dataclass(frozen=True)
class QueryWindow:
    """An already-validated window. Constructing one does not check it."""

    start: datetime
    end: datetime
    step_seconds: int

    @property
    def span_seconds(self) -> int:
        return int((self.end - self.start).total_seconds())

    @property
    def point_count(self) -> int:
        return self.span_seconds // max(1, self.step_seconds) + 1


def _as_utc(value: datetime) -> datetime:
    """SQLite hands back naive datetimes; treat those as UTC (repo convention)."""

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def ladder_step(span_seconds: int) -> int:
    """Smallest ladder rung that keeps the point count at or under target."""

    if span_seconds <= 0:
        return MIN_STEP_SECONDS
    ideal = span_seconds / TARGET_POINTS
    for rung in STEP_LADDER:
        if rung >= ideal:
            return max(rung, MIN_STEP_SECONDS)
    return STEP_LADDER[-1]


def resolve_window(
    *,
    alert_starts_at: datetime,
    alert_ends_at: datetime | None,
    now: datetime,
) -> QueryWindow:
    """Derive the chart window from the alert itself, then clamp it to budget.

    The window follows the alert rather than being fixed, because the question is
    always "how did this one build up". A still-firing alert runs to `now`; a
    resolved one keeps drawing briefly past recovery so the drop is visible.
    """

    started = _as_utc(alert_starts_at)
    current = _as_utc(now)
    end = current
    if alert_ends_at is not None:
        ended = _as_utc(alert_ends_at)
        end = min(current, ended + timedelta(seconds=TRAIL_OUT_SECONDS))

    # A start timestamp ahead of `end` happens with clock skew between the
    # monitored cluster and this machine. Fall back to a plain lead-in window
    # rather than producing a negative span.
    if end <= started:
        end = started + timedelta(seconds=TRAIL_OUT_SECONDS)

    start = started - timedelta(seconds=LEAD_IN_SECONDS)
    earliest = end - timedelta(seconds=MAX_RANGE_SECONDS)
    if start < earliest:
        start = earliest

    span = int((end - start).total_seconds())
    if span <= 0:
        raise BudgetExceeded(BudgetReason.EMPTY_WINDOW, "window collapsed to zero")
    return QueryWindow(start=start, end=end, step_seconds=ladder_step(span))


def assert_window_within_budget(window: QueryWindow) -> None:
    """Run immediately before every range request. No caller may skip it."""

    span = window.span_seconds
    if span <= 0:
        raise BudgetExceeded(BudgetReason.EMPTY_WINDOW, f"span={span}s")
    if span > MAX_RANGE_SECONDS:
        raise BudgetExceeded(
            BudgetReason.RANGE_TOO_LONG, f"span={span}s > {MAX_RANGE_SECONDS}s"
        )
    if window.step_seconds < MIN_STEP_SECONDS:
        raise BudgetExceeded(
            BudgetReason.STEP_TOO_SMALL,
            f"step={window.step_seconds}s < {MIN_STEP_SECONDS}s",
        )
    if window.point_count > TARGET_POINTS * 2:
        raise BudgetExceeded(
            BudgetReason.TOO_MANY_POINTS,
            f"points={window.point_count} > {TARGET_POINTS * 2}",
        )


def assert_query_count_within_budget(count: int) -> None:
    if count > MAX_QUERIES_PER_ALERT:
        raise BudgetExceeded(
            BudgetReason.TOO_MANY_QUERIES, f"{count} > {MAX_QUERIES_PER_ALERT}"
        )


def assert_series_count_within_budget(count: int) -> None:
    """Checked against what the response actually carries.

    Refusing the whole curve is deliberate: silently keeping the first 20 of 500
    series would draw a chart that looks fine and means nothing.
    """

    if count > MAX_SERIES_PER_QUERY:
        raise BudgetExceeded(
            BudgetReason.TOO_MANY_SERIES, f"{count} > {MAX_SERIES_PER_QUERY}"
        )
