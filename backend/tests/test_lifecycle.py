"""Deterministic lifecycle tests with trustworthy and failed poll sequences."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import inspect, text
from sqlmodel import Session, create_engine, select

from app.db import ensure_compatible_schema
from app.models import Alert, Incident
from app.services.ingest import ingest_alerts
from app.services.lifecycle import derive_incident_source_state


def only_alert(session: Session) -> Alert:
    return session.exec(select(Alert)).one()


def only_incident(session: Session) -> Incident:
    return session.exec(select(Incident)).one()


def as_utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def test_missing_alert_waits_for_full_grace_period(session, sample_alerts) -> None:
    raw = [sample_alerts[2]]  # TargetDown
    t0 = datetime(2026, 7, 16, 0, 0, tzinfo=timezone.utc)
    ingest_alerts(session, raw, poll_time=t0, resolution_grace_seconds=300)

    first_miss = t0 + timedelta(minutes=1)
    ingest_alerts(session, [], poll_time=first_miss, resolution_grace_seconds=300)
    assert only_alert(session).source_state == "pending_resolution"
    assert as_utc(only_alert(session).missing_since_at) == first_miss
    assert only_incident(session).source_state == "pending_resolution"

    ingest_alerts(
        session,
        [],
        poll_time=first_miss + timedelta(seconds=299),
        resolution_grace_seconds=300,
    )
    assert only_alert(session).source_state == "pending_resolution"

    ingest_alerts(
        session,
        [],
        poll_time=first_miss + timedelta(seconds=300),
        resolution_grace_seconds=300,
    )
    assert only_alert(session).source_state == "resolved"
    assert only_incident(session).source_state == "recovered"


def test_reappearing_alert_resets_missing_window(session, sample_alerts) -> None:
    raw = [sample_alerts[2]]
    t0 = datetime(2026, 7, 16, 0, 0, tzinfo=timezone.utc)
    ingest_alerts(session, raw, poll_time=t0, resolution_grace_seconds=300)
    ingest_alerts(
        session, [], poll_time=t0 + timedelta(minutes=1), resolution_grace_seconds=300
    )

    ingest_alerts(
        session, raw, poll_time=t0 + timedelta(minutes=2), resolution_grace_seconds=300
    )
    alert = only_alert(session)
    assert alert.source_state == "firing"
    assert alert.missing_since_at is None
    assert only_incident(session).source_state == "firing"


def test_legacy_unknown_alert_restarts_the_resolution_grace(session, sample_alerts) -> None:
    """An M1-era `unknown` row must re-enter the grace, not resolve immediately.

    No current runtime writes this state -- a FAILED poll never reaches ingest
    (CAP-02.7) -- so the row is planted directly, the way an upgraded database
    presents one.
    """
    raw = [sample_alerts[2]]
    t0 = datetime(2026, 7, 16, 0, 0, tzinfo=timezone.utc)
    ingest_alerts(session, raw, poll_time=t0, resolution_grace_seconds=300)
    ingest_alerts(
        session, [], poll_time=t0 + timedelta(minutes=1), resolution_grace_seconds=300
    )

    alert = only_alert(session)
    alert.source_state = "unknown"
    alert.missing_since_at = None
    session.add(alert)
    session.commit()
    assert only_alert(session).source_state == "unknown"

    recovered_poll = t0 + timedelta(hours=1)
    ingest_alerts(
        session, [], poll_time=recovered_poll, resolution_grace_seconds=300
    )
    alert = only_alert(session)
    assert alert.source_state == "pending_resolution"
    assert as_utc(alert.missing_since_at) == recovered_poll

    ingest_alerts(
        session,
        [],
        poll_time=recovered_poll + timedelta(minutes=5),
        resolution_grace_seconds=300,
    )
    assert only_alert(session).source_state == "resolved"


def test_incident_state_precedence() -> None:
    assert derive_incident_source_state(["resolved", "pending_resolution"]) == (
        "pending_resolution"
    )
    assert derive_incident_source_state(["unknown", "pending_resolution"]) == "unknown"
    assert derive_incident_source_state(["unknown", "firing"]) == "firing"
    assert derive_incident_source_state(["resolved", "resolved"]) == "recovered"


def test_existing_alert_table_gets_missing_since_column(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE alert (
                    id INTEGER NOT NULL PRIMARY KEY,
                    fingerprint VARCHAR NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE incident (
                    id INTEGER NOT NULL PRIMARY KEY,
                    group_key VARCHAR NOT NULL
                )
                """
            )
        )
        connection.execute(
            text("INSERT INTO alert (id, fingerprint) VALUES (1, 'legacy-fp')")
        )

    ensure_compatible_schema(engine)

    columns = {column["name"] for column in inspect(engine).get_columns("alert")}
    assert "missing_since_at" in columns
    assert "source_id" in columns
    assert "upstream_fingerprint" in columns
    incident_columns = {
        column["name"] for column in inspect(engine).get_columns("incident")
    }
    assert "source_id" in incident_columns
    with engine.connect() as connection:
        migrated = connection.execute(
            text(
                "SELECT source_id, upstream_fingerprint FROM alert WHERE id = 1"
            )
        ).one()
    # F21 adopts the `.env` configuration and repoints legacy source ids onto it,
    # so the invariant is "no orphan", not the literal string "legacy".
    source_id, upstream_fingerprint = migrated
    assert upstream_fingerprint == "legacy-fp"
    with engine.connect() as connection:
        managed = [
            row[0]
            for row in connection.execute(
                text(
                    "SELECT id FROM eventsource WHERE lifecycle_state != 'ARCHIVED'"
                )
            ).all()
        ]
    assert source_id in managed or (source_id == "legacy" and managed == [])


def test_quiet_polls_do_not_refresh_updated_at(session, sample_alerts) -> None:
    """`updated_at` is what the alert list means by "recently updated".

    Recompute runs for every Incident of a source on every successful poll, so
    stamping it unconditionally gave a three-week-old Incident the same
    timestamp as one that had just fired, and CAP-08's ordering said nothing.
    """
    first = datetime(2026, 7, 1, tzinfo=timezone.utc)
    ingest_alerts(session, [sample_alerts[0]], poll_time=first)
    old = only_incident(session)
    old_updated_at = old.updated_at

    # Twenty days of the same alert still firing, observed twice more.
    later = first + timedelta(days=20)
    ingest_alerts(session, [sample_alerts[0]], poll_time=later)
    ingest_alerts(session, [sample_alerts[0]], poll_time=later + timedelta(minutes=5))

    session.refresh(old)
    assert old.updated_at == old_updated_at


def test_a_real_state_change_still_moves_updated_at(session, sample_alerts) -> None:
    def at_severity(value: str) -> dict:
        alert = dict(sample_alerts[0])
        alert["labels"] = {**sample_alerts[0]["labels"], "severity": value}
        return alert

    first = datetime(2026, 7, 1, tzinfo=timezone.utc)
    ingest_alerts(session, [at_severity("warning")], poll_time=first)
    incident = only_incident(session)
    assert incident.severity == "warning"

    changed_at = first + timedelta(hours=1)
    ingest_alerts(session, [at_severity("critical")], poll_time=changed_at)

    session.refresh(incident)
    assert incident.severity == "critical"
    assert as_utc(incident.updated_at) == changed_at
