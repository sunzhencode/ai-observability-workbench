"""Tests for deterministic grouping and ingest."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlmodel import select

from app.models import Alert, Incident
from app.services.grouping import compute_group_key
from app.services.ingest import ingest_alerts
from app.services.aggregation_rules import create_aggregation_rule


def configure_sample_grouping(session) -> None:
    create_aggregation_rule(
        session,
        name="KubePodCrashLooping by namespace",
        priority=10,
        enabled=True,
        matchers=[
            {"label": "alertname", "operator": "=", "value": "KubePodCrashLooping"}
        ],
        group_by_labels=["cluster", "namespace"],
    )


def test_group_key_isolates_environment_and_fields():
    a = {"environment": "prod", "alertname": "X", "cluster": "c1", "severity": "critical"}
    b = {"environment": "prod", "alertname": "X", "cluster": "c1", "severity": "warning"}
    assert compute_group_key(a) != compute_group_key(b)
    assert compute_group_key(a) == compute_group_key(dict(a))


def test_ingest_groups_same_key_into_one_incident(session, sample_alerts):
    configure_sample_grouping(session)
    result = ingest_alerts(session, sample_alerts)
    assert result.alerts_seen == 4

    incidents = session.exec(select(Incident)).all()
    # Two crit crash-loop alerts collapse into one incident; TargetDown and
    # HighLatency each form their own -> 3 incidents total.
    assert len(incidents) == 3

    crash = next(i for i in incidents if "KubePodCrashLooping" in i.title)
    members = session.exec(select(Alert).where(Alert.incident_id == crash.id)).all()
    assert len(members) == 2
    assert crash.severity == "critical"
    assert crash.source_state == "firing"


def test_ingest_is_idempotent(session, sample_alerts):
    configure_sample_grouping(session)
    ingest_alerts(session, sample_alerts)
    ingest_alerts(session, sample_alerts)
    alerts = session.exec(select(Alert)).all()
    incidents = session.exec(select(Incident)).all()
    assert len(alerts) == 4
    assert len(incidents) == 3


def test_incident_enters_pending_resolution_when_members_first_absent(session, sample_alerts):
    configure_sample_grouping(session)
    t1 = datetime(2026, 7, 15, 10, 0, 0, tzinfo=timezone.utc)
    ingest_alerts(session, sample_alerts, poll_time=t1)

    # Next poll: only the TargetDown alert remains active.
    t2 = datetime(2026, 7, 15, 10, 1, 0, tzinfo=timezone.utc)
    remaining = [a for a in sample_alerts if a["labels"]["alertname"] == "TargetDown"]
    ingest_alerts(session, remaining, poll_time=t2)

    incidents = session.exec(select(Incident)).all()
    crash = next(i for i in incidents if "KubePodCrashLooping" in i.title)
    target = next(i for i in incidents if "TargetDown" in i.title)
    assert crash.source_state == "pending_resolution"
    assert target.source_state == "firing"
