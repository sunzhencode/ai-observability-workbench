"""Application unit-of-work regressions for Incident mutations."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlmodel import select

from app.models import Alert, Incident, IncidentAudit
from app.services import ingest as ingest_service
from app.services.handling import change_handling_state


def test_outer_uow_rolls_back_alert_incident_and_lifecycle_together(
    session, sample_alerts, monkeypatch
) -> None:
    def fail_lifecycle(*args, **kwargs):
        raise RuntimeError("planner persistence failed")

    monkeypatch.setattr(ingest_service, "apply_successful_poll", fail_lifecycle)

    with pytest.raises(RuntimeError, match="planner persistence failed"):
        ingest_service.ingest_alerts(
            session,
            [sample_alerts[2]],
            poll_time=datetime(2026, 7, 18, 3, 0, tzinfo=timezone.utc),
        )

    session.rollback()
    assert session.exec(select(Alert)).all() == []
    assert session.exec(select(Incident)).all() == []


def test_outer_uow_rolls_back_when_notification_planner_persistence_fails(
    session, sample_alerts, monkeypatch
) -> None:
    def fail_planner(*args, **kwargs):
        raise RuntimeError("notification outbox failed")

    monkeypatch.setattr(ingest_service, "reconcile_incident_changes", fail_planner)
    with pytest.raises(RuntimeError, match="notification outbox failed"):
        ingest_service.ingest_alerts(
            session,
            [sample_alerts[2]],
            poll_time=datetime(2026, 7, 18, 3, 0, tzinfo=timezone.utc),
        )
    session.rollback()
    assert session.exec(select(Alert)).all() == []
    assert session.exec(select(Incident)).all() == []


def test_ingest_flushes_but_outer_rollback_is_authoritative(
    session, sample_alerts
) -> None:
    ingest_service.ingest_alerts(session, [sample_alerts[2]])
    assert session.exec(select(Alert)).all()

    session.rollback()

    assert session.exec(select(Alert)).all() == []
    assert session.exec(select(Incident)).all() == []


def test_handling_service_flushes_but_outer_rollback_is_authoritative(session) -> None:
    incident = Incident(
        group_key="source=legacy|env=prod|unmatched|isolation=fingerprint:one",
        title="TargetDown",
    )
    session.add(incident)
    session.commit()
    session.refresh(incident)

    change_handling_state(
        session,
        incident,
        to_state="IN_PROGRESS",
        reason="transaction test",
    )
    session.rollback()
    session.expire_all()

    stored = session.get(Incident, incident.id)
    assert stored is not None
    assert stored.handling_state == "NEW"
    assert session.exec(select(IncidentAudit)).all() == []
