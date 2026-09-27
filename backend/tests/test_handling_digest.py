"""The input that makes an investigation worth reading.

Without this, a model given an alert can only produce general Kubernetes
advice — correct and useless, because it does not know this group. These are the
numbers that let it say "you have called this a false alarm three times", and
**every one of them is computed here** rather than asked of the model (ADR 0011):
counting is something models do badly and confidently, and a wrong count reads
exactly like a right one.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.registry_models import F20Model, IncidentOccurrence
from app.services.handling_digest import (
    HANDLING_WINDOW_DAYS,
    MAX_RECENT_OCCURRENCES,
    build_handling_digest,
    digest_facts,
)

NOW = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
GROUP = "es-cluster-yellow"


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    F20Model.metadata.create_all(engine)
    with Session(engine) as active:
        yield active


def _occurrence(session, *, days_ago: float, seconds: int, conclusion="NEW", no=1):
    recovered = NOW - timedelta(days=days_ago)
    session.add(
        IncidentOccurrence(
            incident_id=1,
            occurrence_no=no,
            source_id="src",
            group_key=GROUP,
            started_at=recovered - timedelta(seconds=seconds),
            recovered_at=recovered,
            handling_conclusion=conclusion,
            member_count=1,
        )
    )
    session.flush()


def test_no_history_is_a_fact_not_a_blank(session) -> None:
    """"Nothing in 30 days" is genuinely useful — it says this is not routine.

    Rendering it as an absence invites the model to fill the gap, which is the
    one thing the evidence gate exists to prevent (D52).
    """
    digest = build_handling_digest(session, group_key=GROUP, now=NOW)
    assert digest.has_history is False
    facts = digest_facts(digest)
    assert facts[0]["fact_id"] == "hist_none"
    assert "没有既往发生记录" in facts[0]["statement"]


def test_counts_durations_and_conclusions_are_computed(session) -> None:
    _occurrence(session, days_ago=1, seconds=120, conclusion="FALSE_ALARM", no=3)
    _occurrence(session, days_ago=2, seconds=300, conclusion="FALSE_ALARM", no=2)
    _occurrence(session, days_ago=3, seconds=600, conclusion="CLOSED", no=1)

    digest = build_handling_digest(session, group_key=GROUP, now=NOW)

    assert digest.occurrences_in_window == 3
    assert digest.median_duration_seconds == 300
    assert digest.longest_duration_seconds == 600
    assert digest.conclusion_counts == {"FALSE_ALARM": 2, "CLOSED": 1}


def test_records_outside_the_window_are_not_counted(session) -> None:
    _occurrence(session, days_ago=1, seconds=60, no=2)
    _occurrence(session, days_ago=HANDLING_WINDOW_DAYS + 5, seconds=9999, no=1)

    digest = build_handling_digest(session, group_key=GROUP, now=NOW)
    assert digest.occurrences_in_window == 1
    assert digest.longest_duration_seconds == 60


def test_another_group_is_not_mixed_in(session) -> None:
    _occurrence(session, days_ago=1, seconds=60, no=1)
    session.add(
        IncidentOccurrence(
            incident_id=2,
            occurrence_no=1,
            source_id="src",
            group_key="something-else",
            started_at=NOW - timedelta(hours=2),
            recovered_at=NOW - timedelta(hours=1),
            handling_conclusion="CLOSED",
        )
    )
    session.flush()

    digest = build_handling_digest(session, group_key=GROUP, now=NOW)
    assert digest.occurrences_in_window == 1


def test_longest_is_reported_only_when_strictly_longer(session) -> None:
    """A repeat of the same length must not be announced as a record.

    Groups that recover on a timer tie constantly; calling every tie "the
    longest yet" would make the one message that should mean something mean
    nothing.
    """
    _occurrence(session, days_ago=1, seconds=600, no=1)

    tie = build_handling_digest(
        session,
        group_key=GROUP,
        now=NOW,
        current_started_at=NOW - timedelta(seconds=600),
    )
    assert tie.current_is_longest_in_window is False

    longer = build_handling_digest(
        session,
        group_key=GROUP,
        now=NOW,
        current_started_at=NOW - timedelta(seconds=601),
    )
    assert longer.current_is_longest_in_window is True
    assert any(f["fact_id"] == "hist_longest" for f in digest_facts(longer))


def test_the_window_is_named_in_every_derived_field(session) -> None:
    """D53: an unscoped name lets a 30-day statistic read as "ever"."""
    _occurrence(session, days_ago=1, seconds=60, no=1)
    digest = build_handling_digest(
        session, group_key=GROUP, now=NOW, current_started_at=NOW - timedelta(hours=1)
    )
    assert digest.window_days == HANDLING_WINDOW_DAYS
    assert hasattr(digest, "current_is_longest_in_window")
    assert not hasattr(digest, "current_is_longest")
    for fact in digest_facts(digest):
        if fact["fact_id"] in {"hist_count", "hist_longest"}:
            assert str(HANDLING_WINDOW_DAYS) in fact["statement"]


def test_recent_list_is_bounded_and_newest_first(session) -> None:
    for index in range(MAX_RECENT_OCCURRENCES + 3):
        _occurrence(session, days_ago=index + 1, seconds=60 + index, no=index + 1)

    digest = build_handling_digest(session, group_key=GROUP, now=NOW)
    assert len(digest.recent) == MAX_RECENT_OCCURRENCES
    times = [item.recovered_at for item in digest.recent]
    assert times == sorted(times, reverse=True)


def test_a_naive_timestamp_from_sqlite_does_not_break_against_an_aware_now(
    session,
) -> None:
    """The repository's oldest recurring defect, exercised where it actually bites.

    An earlier version of this test made *both* stored timestamps naive, so the
    subtraction stayed naive-minus-naive and passed with the conversion deleted —
    it was testing nothing. The real risk is the live incident's start time,
    which comes back from SQLite naive and is then subtracted from an aware
    `now`: that raises outright, and in the stored-column case it silently comes
    out eight hours wrong on this machine.
    """
    session.add(
        IncidentOccurrence(
            incident_id=1,
            occurrence_no=1,
            source_id="src",
            group_key=GROUP,
            started_at=(NOW - timedelta(seconds=300)).replace(tzinfo=None),
            recovered_at=NOW.replace(tzinfo=None),
            handling_conclusion="CLOSED",
        )
    )
    session.flush()

    digest = build_handling_digest(
        session,
        group_key=GROUP,
        now=NOW,
        # Naive, exactly as it arrives from the database.
        current_started_at=(NOW - timedelta(seconds=900)).replace(tzinfo=None),
    )

    assert digest.median_duration_seconds == 300
    assert digest.current_duration_seconds == 900, "naive/aware 混算，时长错了 8 小时"
    assert digest.current_is_longest_in_window is True


def test_every_fact_carries_an_id_the_model_can_be_held_to(session) -> None:
    """A conclusion must point at something checkable (D22)."""
    _occurrence(session, days_ago=1, seconds=120, conclusion="FALSE_ALARM", no=1)
    facts = digest_facts(build_handling_digest(session, group_key=GROUP, now=NOW))
    assert facts
    assert all(f["fact_id"] and f["statement"] for f in facts)
    assert len({f["fact_id"] for f in facts}) == len(facts)
