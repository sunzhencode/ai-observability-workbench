"""Sealing an ended occurrence into the append-only history (F26 / CAP-11).

The interesting half of this file is the negatives. "A group recovered" is easy;
what makes history trustworthy is that a *poll that did not observe recovery*
never writes a record, and that a record already written can never be emptied.

Written before the implementation, on purpose: the four sealing conditions in
design D8 are each a way the naive version gets it wrong.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.registry_models import EventSource, F20Model, IncidentOccurrence
from app.models import Alert, Incident
from app.services.notification_planner import IncidentChange
from app.services.occurrence_history import seal_ended_occurrences

NOW = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    F20Model.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(EventSource(id="src_a", name="nonprod AM", lifecycle_state="ENABLED"))
        session.flush()
        yield session


def _incident(
    session: Session,
    *,
    incident_id: int = 1,
    occurrence_no: int = 1,
    severity: str = "warning",
    handling_state: str = "NEW",
    members: int = 1,
) -> Incident:
    incident = Incident(
        id=incident_id,
        source_id="src_a",
        group_key=f"gk-{incident_id}",
        title="KubePersistentVolumeFillingUp",
        severity=severity,
        source_state="recovered",
        handling_state=handling_state,
        occurrence_no=occurrence_no,
        occurrence_started_at=NOW - timedelta(hours=2),
        aggregation_rule_id=None,
    )
    session.add(incident)
    session.flush()
    for index in range(members):
        session.add(
            Alert(
                fingerprint=f"fp-{incident_id}-{index}",
                source_id="src_a",
                alertname="KubePersistentVolumeFillingUp",
                severity=severity,
                source_state="resolved",
                incident_id=incident_id,
                starts_at=NOW - timedelta(hours=2),
                first_seen_at=NOW - timedelta(hours=2),
                last_seen_at=NOW,
            )
        )
    session.flush()
    return incident


def _change(
    *,
    incident_id: int = 1,
    occurrence_no: int = 1,
    previous: str | None = "firing",
    current: str = "recovered",
    origin: str = "LIVE_POLL",
) -> IncidentChange:
    return IncidentChange(
        incident_id=incident_id,
        occurrence_no=occurrence_no,
        previous_source_state=previous,
        current_source_state=current,
        previous_severity="warning",
        current_severity="warning",
        previous_handling_state="NEW",
        current_handling_state="NEW",
        change_version=2,
        origin=origin,
        observed_at=NOW,
    )


def _records(session: Session) -> list[IncidentOccurrence]:
    return list(session.exec(select(IncidentOccurrence)))


def _naive(value: datetime) -> datetime:
    """Compare timestamps the way this repository stores them.

    SQLite hands datetimes back without tzinfo (the documented pitfall in
    `AGENTS.md`); the convention is that a naive value in the database *is* UTC,
    and `schemas.py` re-attaches the `Z` on the way out. Comparing an
    aware expectation against a freshly-read row therefore always fails, which
    says nothing about the behaviour under test.
    """

    return value.replace(tzinfo=None)


class TestItSealsARealEnding:
    def test_a_complete_live_poll_that_saw_recovery_writes_one_record(
        self, session: Session
    ) -> None:
        _incident(session, members=2)

        sealed = seal_ended_occurrences(
            session, [_change()], reconcile_lifecycle=True, observed_at=NOW
        )

        assert sealed == 1
        record = _records(session)[0]
        assert record.incident_id == 1
        assert record.occurrence_no == 1
        assert _naive(record.recovered_at) == _naive(NOW)
        assert _naive(record.started_at) == _naive(NOW - timedelta(hours=2))
        assert record.member_count == 2
        assert record.member_max_severity == "warning"
        assert record.handling_conclusion == "NEW"

    def test_the_record_denormalises_everything_needed_to_read_it(
        self, session: Session
    ) -> None:
        """D9: the Incident, source and rule will all be deleted before this row."""

        _incident(session)

        seal_ended_occurrences(
            session, [_change()], reconcile_lifecycle=True, observed_at=NOW
        )

        record = _records(session)[0]
        assert record.source_id == "src_a"
        assert record.source_name == "nonprod AM"
        assert record.group_key == "gk-1"
        assert record.title == "KubePersistentVolumeFillingUp"

    def test_severity_is_the_max_across_members_not_the_incident_field(
        self, session: Session
    ) -> None:
        incident = _incident(session, severity="info", members=1)
        session.add(
            Alert(
                fingerprint="fp-critical",
                source_id="src_a",
                alertname="Other",
                severity="critical",
                source_state="resolved",
                incident_id=incident.id,
                starts_at=NOW,
                first_seen_at=NOW,
                last_seen_at=NOW,
            )
        )
        session.flush()

        seal_ended_occurrences(
            session, [_change()], reconcile_lifecycle=True, observed_at=NOW
        )

        assert _records(session)[0].member_max_severity == "critical"

    def test_two_occurrences_of_one_group_produce_two_records(
        self, session: Session
    ) -> None:
        """The point of the whole capability: recurrence must not overwrite history."""

        incident = _incident(session)
        seal_ended_occurrences(
            session, [_change(occurrence_no=1)], reconcile_lifecycle=True, observed_at=NOW
        )

        # The group fires again and recovers again; the live row moves on.
        later = NOW + timedelta(hours=5)
        incident.occurrence_no = 2
        incident.occurrence_started_at = NOW + timedelta(hours=4)
        incident.severity = "critical"
        session.add(incident)
        session.flush()

        seal_ended_occurrences(
            session,
            [_change(occurrence_no=2)],
            reconcile_lifecycle=True,
            observed_at=later,
        )

        records = sorted(_records(session), key=lambda item: item.occurrence_no)
        assert [item.occurrence_no for item in records] == [1, 2]
        # The first record still describes the first occurrence.
        assert _naive(records[0].recovered_at) == _naive(NOW)
        assert _naive(records[0].started_at) == _naive(NOW - timedelta(hours=2))
        assert _naive(records[1].recovered_at) == _naive(later)


class TestItRefusesToSealAnythingElse:
    def test_a_partial_poll_never_seals(self, session: Session) -> None:
        """R3: PARTIAL/FAILED must not turn "not observed" into "it ended"."""

        _incident(session)

        sealed = seal_ended_occurrences(
            session, [_change()], reconcile_lifecycle=False, observed_at=NOW
        )

        assert sealed == 0
        assert _records(session) == []

    def test_backfill_never_seals(self, session: Session) -> None:
        _incident(session)

        sealed = seal_ended_occurrences(
            session,
            [_change(origin="BACKFILL")],
            reconcile_lifecycle=True,
            observed_at=NOW,
        )

        assert sealed == 0
        assert _records(session) == []

    def test_a_brand_new_incident_is_not_an_ended_occurrence(
        self, session: Session
    ) -> None:
        """`previous is None` means the pre-poll snapshot had no such Incident."""

        _incident(session)

        sealed = seal_ended_occurrences(
            session, [_change(previous=None)], reconcile_lifecycle=True, observed_at=NOW
        )

        assert sealed == 0
        assert _records(session) == []

    def test_staying_recovered_does_not_seal_again(self, session: Session) -> None:
        _incident(session)

        sealed = seal_ended_occurrences(
            session,
            [_change(previous="recovered")],
            reconcile_lifecycle=True,
            observed_at=NOW,
        )

        assert sealed == 0
        assert _records(session) == []

    def test_a_transition_that_is_not_into_recovered_does_not_seal(
        self, session: Session
    ) -> None:
        _incident(session)

        sealed = seal_ended_occurrences(
            session,
            [_change(previous="firing", current="pending_resolution")],
            reconcile_lifecycle=True,
            observed_at=NOW,
        )

        assert sealed == 0
        assert _records(session) == []

    def test_a_memberless_incident_never_seals(self, session: Session) -> None:
        """The trap that made this condition necessary (design D8).

        `lifecycle.recompute_incident_lifecycle` derives `recovered` for an
        Incident with **zero members**. So retention deleting the members, or a
        regroup moving them to another group, both look exactly like a recovery.
        Without this guard the history would fill up with occurrences that never
        happened -- and they would be indistinguishable from real ones.
        """

        _incident(session, members=0)

        sealed = seal_ended_occurrences(
            session, [_change()], reconcile_lifecycle=True, observed_at=NOW
        )

        assert sealed == 0
        assert _records(session) == []

    def test_a_change_for_a_vanished_incident_does_not_crash_or_seal(
        self, session: Session
    ) -> None:
        sealed = seal_ended_occurrences(
            session, [_change(incident_id=999)], reconcile_lifecycle=True, observed_at=NOW
        )

        assert sealed == 0
        assert _records(session) == []


class TestIdempotency:
    def test_sealing_the_same_occurrence_twice_writes_one_row(
        self, session: Session
    ) -> None:
        """R5: a repeated poll or a replayed transaction must be a no-op."""

        _incident(session)

        first = seal_ended_occurrences(
            session, [_change()], reconcile_lifecycle=True, observed_at=NOW
        )
        second = seal_ended_occurrences(
            session,
            [_change()],
            reconcile_lifecycle=True,
            observed_at=NOW + timedelta(minutes=1),
        )

        assert (first, second) == (1, 0)
        assert len(_records(session)) == 1
        # The existing record keeps its original recovery time.
        assert _naive(_records(session)[0].recovered_at) == _naive(NOW)

    def test_the_same_batch_containing_a_duplicate_change_writes_one_row(
        self, session: Session
    ) -> None:
        _incident(session)

        sealed = seal_ended_occurrences(
            session, [_change(), _change()], reconcile_lifecycle=True, observed_at=NOW
        )

        assert sealed == 1
        assert len(_records(session)) == 1


class TestTheConclusionField:
    """D16: the one mutable field, and the derived window it is mutable in."""

    def test_a_conclusion_given_after_recovery_reaches_the_record(
        self, session: Session
    ) -> None:
        """Otherwise the conclusion filter would answer "unsettled" forever.

        Recovery never settles handling (CAP-04.9), so at seal time the record
        says NEW. The human's verdict almost always arrives afterwards.
        """

        incident = _incident(session)
        seal_ended_occurrences(
            session, [_change()], reconcile_lifecycle=True, observed_at=NOW
        )

        incident.handling_state = "CLOSED"
        session.add(incident)
        session.flush()
        from app.services.occurrence_history import sync_open_conclusion

        sync_open_conclusion(session, incident)

        assert _records(session)[0].handling_conclusion == "CLOSED"

    def test_a_conclusion_cannot_reach_a_closed_occurrence(
        self, session: Session
    ) -> None:
        """Once the group fires again, that record is finished for good."""

        incident = _incident(session)
        seal_ended_occurrences(
            session, [_change()], reconcile_lifecycle=True, observed_at=NOW
        )

        # The group fires again: the live row moves to occurrence 2.
        incident.occurrence_no = 2
        incident.handling_state = "FALSE_POSITIVE"
        session.add(incident)
        session.flush()

        from app.services.occurrence_history import sync_open_conclusion

        sync_open_conclusion(session, incident)

        assert _records(session)[0].handling_conclusion == "NEW"

    def test_the_facts_are_never_touched_by_a_conclusion_change(
        self, session: Session
    ) -> None:
        incident = _incident(session, members=2)
        seal_ended_occurrences(
            session, [_change()], reconcile_lifecycle=True, observed_at=NOW
        )
        before = _records(session)[0]
        facts = (
            before.started_at,
            before.recovered_at,
            before.member_count,
            before.member_max_severity,
            before.group_key,
        )

        incident.handling_state = "CLOSED"
        incident.severity = "critical"
        session.add(incident)
        session.flush()
        from app.services.occurrence_history import sync_open_conclusion

        sync_open_conclusion(session, incident)

        after = _records(session)[0]
        assert (
            after.started_at,
            after.recovered_at,
            after.member_count,
            after.member_max_severity,
            after.group_key,
        ) == facts


class TestItOnlyFlushes:
    def test_sealing_does_not_commit(self, session: Session) -> None:
        """Transaction rule 3: the seal belongs to the caller's unit of work.

        If this committed, a later failure in the same poll could no longer roll
        the record back and history would disagree with the Incident it describes.
        """

        _incident(session)
        seal_ended_occurrences(
            session, [_change()], reconcile_lifecycle=True, observed_at=NOW
        )

        session.rollback()

        assert _records(session) == []


class TestRetentionKeepsItsOwnClock:
    """CAP-09.6a: 30 days deletes terminal facts; history is not a terminal fact."""

    def test_history_survives_the_thirty_day_cutoff(self, session: Session) -> None:
        from app.services.retention import cleanup_expired_data

        _incident(session)
        seal_ended_occurrences(
            session,
            [_change()],
            reconcile_lifecycle=True,
            observed_at=NOW - timedelta(days=200),
        )
        session.commit()

        cleanup_expired_data(session, now=NOW, retention_days=30)

        # 200 days old, and still here: the record is the only surviving evidence
        # that this occurrence ever happened.
        assert len(_records(session)) == 1

    def test_history_is_pruned_on_its_own_much_longer_clock(
        self, session: Session
    ) -> None:
        from app.services.retention import cleanup_expired_data

        _incident(session)
        seal_ended_occurrences(
            session,
            [_change()],
            reconcile_lifecycle=True,
            observed_at=NOW - timedelta(days=400),
        )
        session.commit()

        result = cleanup_expired_data(
            session,
            now=NOW,
            retention_days=30,
            occurrence_history_retention_days=365,
        )

        assert result.occurrence_history_deleted == 1
        assert _records(session) == []

    def test_the_two_retention_numbers_are_not_the_same_knob(self) -> None:
        from app.runtime_config import (
            OCCURRENCE_HISTORY_RETENTION_DAYS,
            RETENTION_DAYS,
            runtime_config_provider,
        )

        assert OCCURRENCE_HISTORY_RETENTION_DAYS > RETENTION_DAYS
        snapshot = runtime_config_provider.snapshot()
        assert snapshot.occurrence_history_retention_days == 365
        assert snapshot.retention_days == 30


def test_history_is_recorded_through_ingest_with_no_notifications_configured(
    session: Session, sample_alerts
) -> None:
    """The regression D7's shape invites: history must not depend on notifications.

    `seal_ended_occurrences` consumes the change list produced by
    `reconcile_incident_changes`, which lives in the notification planner module.
    That import direction is a deliberate trade (design D7), but it must not turn
    into a functional dependency: with no channel and no policy in the database,
    an occurrence that ends still has to be written down.
    """

    from app.registry_models import F20Model
    from app.models import NotificationChannel, NotificationPolicyRevision
    from app.services.ingest import ingest_alerts

    F20Model.metadata.create_all(session.connection())
    session.add(EventSource(id="legacy", name="legacy", lifecycle_state="ENABLED"))
    session.flush()
    assert session.exec(select(NotificationChannel)).all() == []
    assert session.exec(select(NotificationPolicyRevision)).all() == []

    first_poll = NOW
    ingest_alerts(session, [sample_alerts[2]], poll_time=first_poll)

    # The alert stops being reported; grace 0 means the next poll resolves it.
    for offset in (1, 2):
        ingest_alerts(
            session,
            [],
            poll_time=first_poll + timedelta(minutes=offset),
            resolution_grace_seconds=0,
        )

    records = _records(session)
    assert len(records) == 1
    assert records[0].member_count == 1
    assert records[0].handling_conclusion == "NEW"
