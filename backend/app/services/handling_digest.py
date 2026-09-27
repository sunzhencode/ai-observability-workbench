"""What this alert group's past looks like, computed rather than asked.

This is the input that makes the difference between an investigation worth
reading and a paragraph of general Kubernetes advice. A model reasoning from the
alert text alone can only say "check the pod logs, check resource limits" --
correct, and useless, because it does not know this group. What it cannot know
without this module is:

    这个组过去 30 天发生过 7 次，中位持续 4 分钟。
    **最近 3 次你都标成了误报。** 当前这次已经 22 分钟，是窗口内最长的一次。

No other tool in this space can say that, because none of them has the user's
own handling record (design D21).

**Every number here is computed, never asked of the model** (ADR 0011): counting
is a thing models do badly and confidently, and a wrong count reads exactly like
a right one. The model receives these as facts and may cite them; it never
derives them.

The window is a fixed rolling 30 days and every field name says so (D53). An
unscoped name like `current_is_longest` would let a 30-day statistic be read as
"the longest ever", which is a claim this data cannot support.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from statistics import median

from sqlmodel import Session, select

from app.registry_models import IncidentOccurrence

#: Fixed, and named into every field derived from it. Thirty days is long enough
#: to show a pattern and short enough that "recently" still means recently.
HANDLING_WINDOW_DAYS = 30

#: Enough to see a habit, few enough to read. The digest is prose context, not
#: a table the reader is meant to scan.
MAX_RECENT_OCCURRENCES = 5


@dataclass(frozen=True)
class PastOccurrence:
    occurrence_no: int
    started_at: datetime
    recovered_at: datetime
    duration_seconds: int
    conclusion: str
    member_count: int


@dataclass(frozen=True)
class HandlingDigest:
    """Deterministic facts about one group's recent past.

    `has_history=False` is a **fact**, not a missing value: "no previous record
    in the last 30 days" is genuinely useful for judging whether something is
    routine, and rendering it as an absence invites the model to fill the gap
    (D52).
    """

    group_key: str
    window_days: int = HANDLING_WINDOW_DAYS
    has_history: bool = False
    occurrences_in_window: int = 0
    median_duration_seconds: int | None = None
    longest_duration_seconds: int | None = None
    conclusion_counts: dict[str, int] = field(default_factory=dict)
    recent: tuple[PastOccurrence, ...] = ()
    #: Windowed on purpose (D53): this data cannot support "longest ever".
    current_is_longest_in_window: bool | None = None
    current_duration_seconds: int | None = None


def _utc(value: datetime) -> datetime:
    # SQLite hands datetimes back naive. Comparing one of those against an aware
    # `now` raises, and comparing two naive ones silently comes out eight hours
    # wrong on this machine -- the oldest recurring defect in this repository.
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def build_handling_digest(
    session: Session,
    *,
    group_key: str,
    now: datetime,
    current_started_at: datetime | None = None,
) -> HandlingDigest:
    """Read-only. Touches no F26 write path (CAP-11 stays append-only)."""

    cutoff = _utc(now) - timedelta(days=HANDLING_WINDOW_DAYS)
    rows = list(
        session.exec(
            select(IncidentOccurrence)
            .where(
                IncidentOccurrence.group_key == group_key,
                IncidentOccurrence.recovered_at >= cutoff,
            )
            .order_by(IncidentOccurrence.recovered_at.desc())
        ).all()
    )

    current_duration = None
    if current_started_at is not None:
        current_duration = max(
            0, int((_utc(now) - _utc(current_started_at)).total_seconds())
        )

    if not rows:
        return HandlingDigest(
            group_key=group_key,
            has_history=False,
            current_duration_seconds=current_duration,
        )

    past = [
        PastOccurrence(
            occurrence_no=int(row.occurrence_no or 1),
            started_at=_utc(row.started_at),
            recovered_at=_utc(row.recovered_at),
            duration_seconds=max(
                0, int((_utc(row.recovered_at) - _utc(row.started_at)).total_seconds())
            ),
            conclusion=str(row.handling_conclusion or "NEW"),
            member_count=int(row.member_count or 0),
        )
        for row in rows
    ]
    durations = [item.duration_seconds for item in past]
    longest = max(durations)

    return HandlingDigest(
        group_key=group_key,
        has_history=True,
        occurrences_in_window=len(past),
        median_duration_seconds=int(median(durations)),
        longest_duration_seconds=longest,
        conclusion_counts=dict(Counter(item.conclusion for item in past)),
        recent=tuple(past[:MAX_RECENT_OCCURRENCES]),
        # Strictly longer, so a repeat of the same length is not announced as a
        # record. Ties are the common case for a group that recovers on a timer.
        current_is_longest_in_window=(
            None if current_duration is None else current_duration > longest
        ),
        current_duration_seconds=current_duration,
    )


def digest_facts(digest: HandlingDigest) -> list[dict[str, object]]:
    """The digest as citable facts, each with an id the model must reference.

    Same rule as the metric facts: a conclusion has to point at something
    checkable (D22). "You have marked this a false alarm three times" is only
    worth reading if the reader can see which three.
    """

    facts: list[dict[str, object]] = []
    if not digest.has_history:
        facts.append(
            {
                "fact_id": "hist_none",
                "statement": (
                    f"这个组在过去 {digest.window_days} 天内没有既往发生记录"
                ),
            }
        )
        return facts

    facts.append(
        {
            "fact_id": "hist_count",
            "statement": (
                f"过去 {digest.window_days} 天发生 {digest.occurrences_in_window} 次，"
                f"中位持续 {digest.median_duration_seconds} 秒，"
                f"最长 {digest.longest_duration_seconds} 秒"
            ),
        }
    )
    if digest.conclusion_counts:
        parts = "、".join(
            f"{name} {count} 次" for name, count in sorted(digest.conclusion_counts.items())
        )
        facts.append({"fact_id": "hist_conclusions", "statement": f"处理结论分布：{parts}"})
    if digest.current_is_longest_in_window:
        facts.append(
            {
                "fact_id": "hist_longest",
                "statement": (
                    f"当前这次已持续 {digest.current_duration_seconds} 秒，"
                    f"是这 {digest.window_days} 天窗口内最长的一次"
                ),
            }
        )
    return facts


__all__ = [
    "HANDLING_WINDOW_DAYS",
    "MAX_RECENT_OCCURRENCES",
    "HandlingDigest",
    "PastOccurrence",
    "build_handling_digest",
    "digest_facts",
]
