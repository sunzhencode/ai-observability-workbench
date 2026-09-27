"""Additive F17 schema constraints and Incident change metadata."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.models import (
    Incident,
    NotificationChannel,
    NotificationChannelRevision,
)
from app.runtime_config import DefaultRuntimeConfig
from app.services.ingest import ingest_alerts


def _alert() -> dict:
    return {
        "fingerprint": "fp-f17",
        "labels": {
            "alertname": "TargetDown",
            "severity": "warning",
            "cluster": "qa-retail",
            "namespace": "qa",
        },
        "annotations": {},
        "startsAt": "2026-07-18T04:00:00Z",
        "endsAt": "0001-01-01T00:00:00Z",
    }


def test_ingest_populates_additive_incident_fields(session) -> None:
    now = datetime(2026, 7, 18, 4, 5, tzinfo=timezone.utc)
    ingest_alerts(
        session,
        [_alert()],
        poll_time=now,
        runtime=DefaultRuntimeConfig().snapshot(),
    )

    incident = session.exec(select(Incident)).one()
    assert incident.aggregation_rule_id is None
    assert incident.group_labels == {}
    assert incident.missing_group_labels == []
    assert incident.occurrence_no == 1
    assert incident.occurrence_started_at == datetime(
        2026, 7, 18, 4, 0
    )
    assert incident.change_version == 1
    assert incident.change_origin == "LIVE_POLL"


def test_channel_revision_version_and_active_state_are_unique(session) -> None:
    channel = NotificationChannel(name="qa-oncall")
    session.add(channel)
    session.commit()
    session.refresh(channel)

    first = NotificationChannelRevision(
        channel_id=channel.id,
        version=1,
        state="ACTIVE",
        webhook_envelope={"version": 1, "ciphertext": "test-only"},
    )
    session.add(first)
    session.commit()

    duplicate = NotificationChannelRevision(
        channel_id=channel.id,
        version=2,
        state="ACTIVE",
        webhook_envelope={"version": 1, "ciphertext": "test-only-2"},
    )
    session.add(duplicate)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()

    same_version = NotificationChannelRevision(
        channel_id=channel.id,
        version=1,
        state="RETIRED",
        webhook_envelope={"version": 1, "ciphertext": "test-only-3"},
    )
    session.add(same_version)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
