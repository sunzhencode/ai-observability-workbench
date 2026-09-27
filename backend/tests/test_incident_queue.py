"""Initial operational occurrence and queue-projection contracts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from app.adapters.persistence.incidents import (
    OperationalOccurrenceRecord,
    SqlAlchemyIncidentStore,
)
from app.adapters.persistence.sources import SqlAlchemySourceStore
from app.api.v1.cursor import SignedCursorCodec
from app.application.sources import EndpointDraft, SourceDraft
from app.domains.incidents.queue import (
    AckSlaState,
    SignalState,
    ack_sla_at_risk_at,
    ack_sla_state,
    signal_state,
)
from app.domains.sources.models import (
    EndpointObservation,
    SourceState,
    merge_endpoint_observations,
)
from app.platform.persistence.database import (
    SqliteDatabaseConfig,
    create_session_factory,
    create_sqlite_engine,
)
from app.platform.persistence.migrations import upgrade_database


UTC = timezone.utc


def _raw(severity: str = "warning") -> dict[str, object]:
    return {
        "fingerprint": "target-one",
        "labels": {
            "alertname": "TargetDown",
            "severity": severity,
            "cluster": "cluster-a",
        },
        "annotations": {"summary": "target unavailable"},
        "startsAt": "2026-08-13T00:00:00Z",
    }


def _fixture(tmp_path: Path):
    engine = create_sqlite_engine(
        SqliteDatabaseConfig(path=tmp_path / "incident-operations.db")
    )
    upgrade_database(engine)
    sessions = create_session_factory(engine)
    incidents = SqlAlchemyIncidentStore(
        sessions,
        cursor_codec=SignedCursorCodec(b"queue-test-cursor-key-at-least-32-bytes"),
    )
    sources = SqlAlchemySourceStore(sessions, incident_reconciler=incidents)
    sources.create_source(
        SourceDraft(
            "src-a",
            "Primary AM",
            (EndpointDraft(0, "https://am.invalid"),),
            resolution_grace_seconds=0,
        ),
        now=datetime(2026, 8, 13, tzinfo=UTC),
    )
    return engine, sessions, sources, incidents


def _apply(sources, alerts, *, now, status="SUCCESS"):
    snapshot = sources.load_snapshot("src-a", expected_version=1)
    outcome = merge_endpoint_observations(
        (
            EndpointObservation(
                snapshot.endpoints[0],
                status,
                alerts,
                1,
                safe_error_code=None if status == "SUCCESS" else "ENDPOINT_TIMEOUT",
            ),
        )
    )
    assert sources.apply_collection(snapshot, outcome, observed_at=now).committed


def test_signal_projection_is_a_closed_deterministic_mapping() -> None:
    assert signal_state("FIRING", "STALE") is SignalState.STALE
    assert signal_state("unexpected", "FRESH") is SignalState.UNKNOWN


def test_complete_first_observation_freezes_unmapped_sla(tmp_path: Path) -> None:
    engine, sessions, sources, _ = _fixture(tmp_path)
    detected = datetime(2026, 8, 13, 1, tzinfo=UTC)

    _apply(sources, (_raw("critical"),), now=detected)

    with sessions() as session:
        occurrence = session.scalar(select(OperationalOccurrenceRecord))
        assert occurrence is not None
        assert occurrence.occurrence_no == 1
        assert occurrence.signal_state == "FIRING"
        assert occurrence.signal_severity == "critical"
        assert occurrence.response_state == "UNACKNOWLEDGED"
        assert occurrence.assignment_origin == "UNMAPPED"
        assert occurrence.service_id is None
        assert occurrence.detected_at == detected.replace(tzinfo=None)
        assert occurrence.ack_sla_seconds == 900
        assert occurrence.ack_sla_due_at == (detected + timedelta(minutes=15)).replace(
            tzinfo=None
        )
    engine.dispose()


def test_partial_creation_waits_for_first_complete_poll_to_start_sla(
    tmp_path: Path,
) -> None:
    engine, sessions, sources, _ = _fixture(tmp_path)
    partial_at = datetime(2026, 8, 13, 1, tzinfo=UTC)
    snapshot = sources.load_snapshot("src-a", expected_version=1)
    partial = merge_endpoint_observations(
        (
            EndpointObservation(snapshot.endpoints[0], "SUCCESS", (_raw(),), 1),
            EndpointObservation(
                snapshot.endpoints[0],
                "TIMEOUT",
                (),
                1,
                safe_error_code="ENDPOINT_TIMEOUT",
            ),
        )
    )
    assert sources.apply_collection(snapshot, partial, observed_at=partial_at).committed
    with sessions() as session:
        occurrence = session.scalar(select(OperationalOccurrenceRecord))
        assert occurrence is not None
        assert occurrence.detected_at is None
        assert occurrence.ack_sla_due_at is None

    trusted_at = partial_at + timedelta(minutes=2)
    _apply(sources, (_raw(),), now=trusted_at)
    _apply(sources, (_raw(),), now=trusted_at + timedelta(minutes=1))
    with sessions() as session:
        occurrence = session.scalar(select(OperationalOccurrenceRecord))
        assert occurrence is not None
        assert occurrence.detected_at == trusted_at.replace(tzinfo=None)
        assert occurrence.ack_sla_due_at == (
            trusted_at + timedelta(minutes=15)
        ).replace(tzinfo=None)
        assert occurrence.latest_activity_at == trusted_at.replace(tzinfo=None)
    engine.dispose()


def test_recovery_and_recurrence_keep_independent_response_roots(
    tmp_path: Path,
) -> None:
    engine, sessions, sources, _ = _fixture(tmp_path)
    started = datetime(2026, 8, 13, 1, tzinfo=UTC)
    _apply(sources, (_raw(),), now=started)
    _apply(sources, (), now=started + timedelta(minutes=1))
    _apply(sources, (), now=started + timedelta(minutes=2))
    _apply(sources, (_raw("critical"),), now=started + timedelta(minutes=3))

    with sessions() as session:
        occurrences = tuple(
            session.scalars(
                select(OperationalOccurrenceRecord).order_by(
                    OperationalOccurrenceRecord.occurrence_no
                )
            )
        )
        assert [(item.occurrence_no, item.signal_state) for item in occurrences] == [
            (1, "RECOVERED"),
            (2, "FIRING"),
        ]
        assert occurrences[0].signal_severity == "warning"
        assert all(item.response_state == "UNACKNOWLEDGED" for item in occurrences)
    engine.dispose()


def test_source_disable_marks_unresolved_occurrence_stale(tmp_path: Path) -> None:
    engine, sessions, sources, _ = _fixture(tmp_path)
    started = datetime(2026, 8, 13, 1, tzinfo=UTC)
    _apply(sources, (_raw(),), now=started)

    sources.set_source_state(
        "src-a",
        target=SourceState.DISABLED,
        expected_version=1,
        now=started + timedelta(minutes=1),
    )

    with sessions() as session:
        occurrence = session.scalar(select(OperationalOccurrenceRecord))
        assert occurrence is not None
        assert occurrence.signal_state == "STALE"
        assert occurrence.response_state == "UNACKNOWLEDGED"
    engine.dispose()


def test_ack_sla_final_twenty_percent_has_closed_boundaries() -> None:
    due = datetime(2026, 8, 13, 2, tzinfo=UTC)
    assert ack_sla_at_risk_at(due_at=due, ack_sla_seconds=300) == due - timedelta(
        minutes=1
    )
    assert ack_sla_at_risk_at(due_at=due, ack_sla_seconds=900) == due - timedelta(
        minutes=3
    )
    assert ack_sla_at_risk_at(due_at=due, ack_sla_seconds=1800) == due - timedelta(
        minutes=6
    )
    assert ack_sla_at_risk_at(due_at=due, ack_sla_seconds=3600) == due - timedelta(
        minutes=12
    )
    common = {"due_at": due, "ack_sla_seconds": 900, "acknowledged_at": None}
    assert ack_sla_state(now=due - timedelta(minutes=3, seconds=1), **common) is AckSlaState.ON_TRACK
    assert ack_sla_state(now=due - timedelta(minutes=3), **common) is AckSlaState.AT_RISK
    assert ack_sla_state(now=due, **common) is AckSlaState.BREACHED
    assert ack_sla_state(
        due_at=None,
        ack_sla_seconds=900,
        acknowledged_at=None,
        now=due,
    ) is AckSlaState.NOT_STARTED
    assert ack_sla_state(
        due_at=due,
        ack_sla_seconds=900,
        acknowledged_at=due - timedelta(minutes=4),
        now=due,
    ) is AckSlaState.STOPPED


def test_queue_sort_views_filters_and_keyset_are_deterministic(tmp_path: Path) -> None:
    engine, sessions, sources, incidents = _fixture(tmp_path)
    now = datetime(2026, 8, 13, 2, tzinfo=UTC)

    def create(alertname: str, severity: str, observed_at: datetime) -> int:
        raw = _raw(severity)
        raw["fingerprint"] = alertname.lower()
        raw["labels"] = {
            "alertname": alertname,
            "severity": severity,
            "cluster": "cluster-a",
        }
        _apply(sources, (raw,), now=observed_at)
        with sessions() as session:
            item = session.scalar(
                select(OperationalOccurrenceRecord).where(
                    OperationalOccurrenceRecord.detected_at
                    == observed_at.replace(tzinfo=None),
                )
            )
            assert item is not None
            return item.id

    breached = create("Breached", "info", now - timedelta(minutes=20))
    at_risk = create("AtRisk", "warning", now - timedelta(minutes=13))
    on_track = create("OnTrack", "critical", now - timedelta(minutes=2))

    page = incidents.list_operational_occurrences(
        view="ALL",
        source_ids=(),
        signal_states=(),
        cursor=None,
        limit=2,
        now=now,
    )
    assert [item.id for item in page.items] == [breached, at_risk]
    assert [item.ack_sla_state for item in page.items] == ["BREACHED", "AT_RISK"]
    assert page.next_cursor is not None
    second = incidents.list_operational_occurrences(
        view="ALL",
        source_ids=(),
        signal_states=(),
        cursor=page.next_cursor,
        limit=2,
        now=now,
    )
    assert [item.id for item in second.items] == [on_track]

    risk = incidents.list_operational_occurrences(
        view="SLA_AT_RISK",
        source_ids=(),
        signal_states=(),
        cursor=None,
        limit=50,
        now=now,
    )
    assert [item.id for item in risk.items] == [breached, at_risk]
    firing = incidents.list_operational_occurrences(
        view="ALL",
        source_ids=("src-a",),
        signal_states=("FIRING",),
        cursor=None,
        limit=50,
        now=now,
    )
    assert [item.id for item in firing.items] == [on_track]

    try:
        incidents.list_operational_occurrences(
            view="UNMAPPED",
            source_ids=(),
            signal_states=(),
            cursor=page.next_cursor,
            limit=2,
            now=now,
        )
    except ValueError as exc:
        assert str(exc) == "CURSOR_FILTER_MISMATCH"
    else:
        raise AssertionError("cursor must be bound to queue filters")
    engine.dispose()
