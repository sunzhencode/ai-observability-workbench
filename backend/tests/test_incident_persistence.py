"""Current Incident, occurrence history and retention persistence contracts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.adapters.persistence.incidents import (
    IncidentAuditRecord,
    IncidentOccurrenceRecord,
    SqlAlchemyIncidentStore,
)
from app.adapters.persistence.sources import (
    AlertRecord,
    IncidentRecord,
    PollRunRecord,
    SqlAlchemySourceStore,
)
from app.api.v1.cursor import CursorError, SignedCursorCodec
from app.application.sources import EndpointDraft, SourceDraft
from app.domains.sources.models import (
    EndpointObservation,
    SourceState,
    merge_endpoint_observations,
)
from app.domains.incidents.models import IncidentLifecycleSnapshot
from app.platform.persistence.database import (
    SessionFactory,
    SqliteDatabaseConfig,
    create_session_factory,
    create_sqlite_engine,
)
from app.platform.persistence.migrations import upgrade_database


UTC = timezone.utc
CURSOR_KEY = b"incident-history-test-cursor-key-32-bytes"


def _raw(fingerprint: str, *, severity: str = "warning") -> dict[str, object]:
    return {
        "fingerprint": fingerprint,
        "labels": {
            "alertname": "TargetDown",
            "severity": severity,
            "cluster": "cluster-a",
        },
        "annotations": {"summary": "target unavailable"},
        "startsAt": "2026-08-13T00:00:00Z",
    }


def _stores(
    tmp_path: Path,
) -> tuple[SqlAlchemySourceStore, SqlAlchemyIncidentStore, SessionFactory, Engine]:
    engine = create_sqlite_engine(
        SqliteDatabaseConfig(path=tmp_path / "incident-operations.db")
    )
    upgrade_database(engine)
    sessions = create_session_factory(engine)
    incidents = SqlAlchemyIncidentStore(
        sessions,
        cursor_codec=SignedCursorCodec(CURSOR_KEY),
    )
    sources = SqlAlchemySourceStore(sessions, incident_reconciler=incidents)
    sources.create_source(
        SourceDraft(
            id="src-a",
            name="Primary AM",
            endpoints=(EndpointDraft(0, "https://am.invalid"),),
            resolution_grace_seconds=0,
        ),
        now=datetime(2026, 8, 13, tzinfo=UTC),
    )
    return sources, incidents, sessions, engine


def _apply(
    sources: SqlAlchemySourceStore,
    alerts: tuple[dict[str, object], ...],
    *,
    now: datetime,
    status: str = "SUCCESS",
) -> None:
    snapshot = sources.load_snapshot("src-a", expected_version=1)
    safe_error = None if status == "SUCCESS" else "ENDPOINT_TIMEOUT"
    outcome = merge_endpoint_observations(
        (
            EndpointObservation(
                snapshot.endpoints[0],
                status,
                alerts,
                2,
                safe_error_code=safe_error,
            ),
        )
    )
    result = sources.apply_collection(snapshot, outcome, observed_at=now)
    assert result.committed is True


def test_real_recovery_seals_once_and_recurrence_resets_only_current_handling(
    tmp_path: Path,
) -> None:
    sources, incidents, sessions, engine = _stores(tmp_path)
    t0 = datetime(2026, 8, 13, 1, tzinfo=UTC)
    _apply(sources, (_raw("target"),), now=t0)
    current = sources.list_incidents()[0]
    incidents.change_handling(
        current.id,
        target="IN_PROGRESS",
        reason="operator investigating",
        actor="local-user",
        expected_version=1,
        now=t0 + timedelta(seconds=10),
    )

    # Absence needs the existing two-step grace transition; only the second
    # complete poll creates the immutable occurrence row.
    _apply(sources, (), now=t0 + timedelta(minutes=1))
    assert incidents.list_occurrences(
        source_ids=(), conclusion=None, include_archived=False, cursor=None, limit=50
    ).items == ()
    _apply(sources, (), now=t0 + timedelta(minutes=2))
    page = incidents.list_occurrences(
        source_ids=(), conclusion=None, include_archived=False, cursor=None, limit=50
    )
    assert len(page.items) == 1
    assert page.items[0].occurrence_no == 1
    assert page.items[0].handling_conclusion == "IN_PROGRESS"
    assert page.items[0].member_count == 1

    recovered = incidents.get_incident(current.id)
    assert recovered.source_state == "RECOVERED"
    assert recovered.handling_state == "IN_PROGRESS"
    incidents.change_handling(
        current.id,
        target="CLOSED",
        reason="service recovered and evidence verified",
        actor="local-user",
        expected_version=2,
        now=t0 + timedelta(minutes=2, seconds=10),
    )
    assert incidents.list_occurrences(
        source_ids=(), conclusion="CLOSED", include_archived=False, cursor=None, limit=50
    ).items[0].handling_conclusion == "CLOSED"

    # A trustworthy positive observation reopens occurrence 2 even on PARTIAL;
    # the previous history row remains frozen and a system audit records reset.
    snapshot = sources.load_snapshot("src-a", expected_version=1)
    partial = merge_endpoint_observations(
        (
            EndpointObservation(snapshot.endpoints[0], "SUCCESS", (_raw("target"),), 2),
            EndpointObservation(
                snapshot.endpoints[0],
                "TIMEOUT",
                (),
                2,
                safe_error_code="ENDPOINT_TIMEOUT",
            ),
        )
    )
    assert sources.apply_collection(
        snapshot, partial, observed_at=t0 + timedelta(minutes=3)
    ).committed

    reopened = incidents.get_incident(current.id)
    assert (reopened.source_state, reopened.occurrence_no) == ("FIRING", 2)
    assert reopened.handling_state == "NEW"
    assert [(item.actor, item.to_state, item.reason) for item in reopened.handling_history] == [
        ("local-user", "IN_PROGRESS", "operator investigating"),
        ("local-user", "CLOSED", "service recovered and evidence verified"),
        ("system", "NEW", "source_recurrence"),
    ]
    assert incidents.list_occurrences(
        source_ids=(), conclusion="CLOSED", include_archived=False, cursor=None, limit=50
    ).items[0].occurrence_no == 1

    first_before_second_recovery = incidents.get_occurrence(
        page.items[0].id,
        include_archived=False,
    )
    _apply(sources, (), now=t0 + timedelta(minutes=4))
    _apply(sources, (), now=t0 + timedelta(minutes=5))
    histories = incidents.list_occurrences(
        source_ids=(), conclusion=None, include_archived=False, cursor=None, limit=50
    ).items
    assert [item.occurrence_no for item in histories] == [2, 1]
    assert histories[1] == first_before_second_recovery

    with sessions() as session:
        assert len(tuple(session.scalars(select(IncidentOccurrenceRecord)))) == 2
    engine.dispose()


def test_failed_or_partial_absence_never_seals_history(tmp_path: Path) -> None:
    sources, incidents, _sessions, engine = _stores(tmp_path)
    t0 = datetime(2026, 8, 13, 1, tzinfo=UTC)
    _apply(sources, (_raw("target"),), now=t0)
    _apply(sources, (), now=t0 + timedelta(minutes=1), status="TIMEOUT")
    assert sources.list_incidents()[0].source_state == "FIRING"
    assert incidents.list_occurrences(
        source_ids=(), conclusion=None, include_archived=False, cursor=None, limit=50
    ).items == ()
    engine.dispose()


def test_history_keyset_is_signed_and_bound_to_filters(tmp_path: Path) -> None:
    sources, incidents, sessions, engine = _stores(tmp_path)
    t0 = datetime(2026, 8, 13, 1, tzinfo=UTC)
    _apply(sources, (_raw("target"),), now=t0)
    _apply(sources, (), now=t0 + timedelta(minutes=1))
    _apply(sources, (), now=t0 + timedelta(minutes=2))
    with sessions.begin() as session:
        first = session.scalar(select(IncidentOccurrenceRecord))
        assert first is not None
        session.add(
            IncidentOccurrenceRecord(
                incident_id=first.incident_id,
                occurrence_no=2,
                source_id=first.source_id,
                source_name=first.source_name,
                group_key=first.group_key,
                title=first.title,
                aggregation_rule_id=first.aggregation_rule_id,
                aggregation_rule_name=first.aggregation_rule_name,
                started_at=first.started_at + timedelta(hours=1),
                recovered_at=first.recovered_at + timedelta(hours=1),
                member_count=1,
                member_max_severity="critical",
                handling_conclusion="CLOSED",
            )
        )

    page = incidents.list_occurrences(
        source_ids=("src-a",),
        conclusion=None,
        include_archived=False,
        cursor=None,
        limit=1,
    )
    assert [item.occurrence_no for item in page.items] == [2]
    assert page.next_cursor is not None
    second = incidents.list_occurrences(
        source_ids=("src-a",),
        conclusion=None,
        include_archived=False,
        cursor=page.next_cursor,
        limit=1,
    )
    assert [item.occurrence_no for item in second.items] == [1]

    with pytest.raises(CursorError, match="CURSOR_FILTER_MISMATCH"):
        incidents.list_occurrences(
            source_ids=("src-a",),
            conclusion="CLOSED",
            include_archived=False,
            cursor=page.next_cursor,
            limit=1,
        )
    with pytest.raises(CursorError, match="CURSOR_INVALID"):
        incidents.list_occurrences(
            source_ids=("src-a",),
            conclusion=None,
            include_archived=False,
            cursor=f"{page.next_cursor}x",
            limit=1,
        )

    occurrence_id = page.items[0].id
    sources.set_source_state(
        "src-a",
        target=SourceState.DISABLED,
        expected_version=1,
        now=t0 + timedelta(hours=3),
    )
    assert incidents.list_occurrences(
        source_ids=("src-a",),
        conclusion=None,
        include_archived=False,
        cursor=None,
        limit=10,
    ).items
    assert incidents.get_occurrence(occurrence_id, include_archived=False).id == occurrence_id
    sources.set_source_state(
        "src-a",
        target=SourceState.ARCHIVED,
        expected_version=2,
        now=t0 + timedelta(hours=4),
    )
    assert incidents.list_occurrences(
        source_ids=("src-a",),
        conclusion=None,
        include_archived=False,
        cursor=None,
        limit=10,
    ).items == ()
    assert incidents.get_occurrence(occurrence_id, include_archived=True).id == occurrence_id
    engine.dispose()


def test_retention_keeps_active_alerts_and_uses_a_separate_history_clock(
    tmp_path: Path,
) -> None:
    sources, incidents, sessions, engine = _stores(tmp_path)
    now = datetime(2026, 8, 13, 12, tzinfo=UTC)
    _apply(sources, (_raw("target"),), now=now - timedelta(days=40))
    current = sources.list_incidents()[0]
    with sessions.begin() as session:
        alert = session.scalar(select(AlertRecord))
        incident = session.get(IncidentRecord, current.id)
        assert alert is not None and incident is not None
        alert.last_seen_at = (now - timedelta(days=40)).replace(tzinfo=None)
        session.add(
            IncidentOccurrenceRecord(
                incident_id=incident.id,
                occurrence_no=99,
                source_id=incident.source_id,
                source_name="Primary AM",
                group_key=incident.group_key,
                title=incident.title,
                aggregation_rule_id=None,
                aggregation_rule_name=None,
                started_at=(now - timedelta(days=401)).replace(tzinfo=None),
                recovered_at=(now - timedelta(days=400)).replace(tzinfo=None),
                member_count=1,
                member_max_severity="warning",
                handling_conclusion="NEW",
            )
        )
        session.add(
            IncidentOccurrenceRecord(
                incident_id=incident.id,
                occurrence_no=100,
                source_id=incident.source_id,
                source_name="Primary AM",
                group_key=incident.group_key,
                title=incident.title,
                aggregation_rule_id=None,
                aggregation_rule_name=None,
                started_at=(now - timedelta(days=101)).replace(tzinfo=None),
                recovered_at=(now - timedelta(days=100)).replace(tzinfo=None),
                member_count=1,
                member_max_severity="warning",
                handling_conclusion="NEW",
            )
        )
        session.add(
            IncidentAuditRecord(
                incident_id=incident.id,
                actor="local-user",
                from_state="NEW",
                to_state="IN_PROGRESS",
                reason="old but independently retained audit",
                created_at=(now - timedelta(days=40)).replace(tzinfo=None),
            )
        )

    result = incidents.cleanup(now=now, runtime_days=30, history_days=365)
    assert result.alerts_deleted == 0
    assert result.audits_deleted == 1
    assert result.occurrences_deleted == 1
    with sessions() as session:
        alert = session.scalar(select(AlertRecord))
        assert alert is not None and alert.source_state == "FIRING"
        assert [item.occurrence_no for item in session.scalars(select(IncidentOccurrenceRecord))] == [100]
    engine.dispose()


class _FailAfterReconcile:
    def __init__(self, delegate: SqlAlchemyIncidentStore) -> None:
        self._delegate = delegate

    def snapshot(
        self,
        session: Session,
        source_id: str,
    ) -> dict[int, IncidentLifecycleSnapshot]:
        return self._delegate.snapshot(session, source_id)

    def reconcile_source_changes(
        self,
        session: Session,
        before: dict[int, IncidentLifecycleSnapshot],
        **kwargs: Any,
    ) -> tuple[object, ...]:
        self._delegate.reconcile_source_changes(session, before, **kwargs)
        raise RuntimeError("injected transaction failure")


def test_source_apply_rolls_back_poll_incident_and_history_together(tmp_path: Path) -> None:
    sources, incidents, sessions, engine = _stores(tmp_path)
    failing = SqlAlchemySourceStore(sessions, incident_reconciler=_FailAfterReconcile(incidents))
    snapshot = failing.load_snapshot("src-a", expected_version=1)
    outcome = merge_endpoint_observations(
        (EndpointObservation(snapshot.endpoints[0], "SUCCESS", (_raw("target"),), 2),)
    )
    with pytest.raises(RuntimeError, match="injected transaction failure"):
        failing.apply_collection(
            snapshot,
            outcome,
            observed_at=datetime(2026, 8, 13, 1, tzinfo=UTC),
        )
    with sessions() as session:
        assert session.scalar(select(PollRunRecord.id)) is None
        assert session.scalar(select(IncidentRecord.id)) is None
        assert session.scalar(select(IncidentOccurrenceRecord.id)) is None
    engine.dispose()
