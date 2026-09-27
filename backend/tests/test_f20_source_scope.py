"""F20 phase 5 source scope, global Incident, and stale-source tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.api import incidents
from app.db import get_session
from app.registry_models import EventSource, F20Model
from app.models import (
    Alert,
    Incident,
    NotificationChannel,
    NotificationChannelRevision,
    NotificationRoute,
    NotificationRouteTarget,
)
from app.services.aggregation_rules import create_aggregation_rule
from app.services.event_sources import disable_event_source
from app.services.ingest import ingest_alerts
from app.services.notification_planner import plan_due_reminders
from app.services.notification_policies import (
    PolicyCandidate,
    choose_policy,
    incident_route_context,
    validate_policy_matchers,
)


@pytest.fixture
def f20_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    F20Model.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _source(session: Session, source_id: str, state: str = "ENABLED") -> EventSource:
    source = EventSource(
        id=source_id,
        name=f"{source_id} display",
        lifecycle_state=state,
        version=1,
    )
    session.add(source)
    session.flush()
    return source


def _incident(
    session: Session,
    source_id: str,
    *,
    severity: str = "critical",
    freshness_state: str = "FRESH",
) -> Incident:
    incident = Incident(
        source_id=source_id,
        environment="legacy-env-must-not-route",
        group_key=f"source={source_id}|rule=1|cluster={source_id}",
        title=f"TargetDown {source_id}",
        severity=severity,
        source_state="firing",
        freshness_state=freshness_state,
        group_labels={"cluster": source_id},
    )
    session.add(incident)
    session.flush()
    session.add(
        Alert(
            fingerprint=f"{source_id}:fp",
            upstream_fingerprint="fp",
            source_id=source_id,
            environment="legacy-env-must-not-route",
            alertname="TargetDown",
            severity=severity,
            cluster=source_id,
            labels={"alertname": "TargetDown", "cluster": source_id},
            annotations={},
            incident_id=incident.id,
        )
    )
    session.flush()
    return incident


def test_incidents_default_to_enabled_sources_and_report_source_badges(
    f20_session,
) -> None:
    _source(f20_session, "src_a", "ENABLED")
    _source(f20_session, "src_b", "ENABLED")
    _source(f20_session, "src_disabled", "DISABLED")
    _source(f20_session, "src_archived", "ARCHIVED")
    _incident(f20_session, "src_a", severity="critical")
    _incident(f20_session, "src_b", severity="warning")
    _incident(f20_session, "src_disabled", severity="info")
    _incident(f20_session, "src_archived", severity="critical")
    f20_session.commit()

    app = FastAPI()
    app.include_router(incidents.router, prefix="/api")
    app.dependency_overrides[get_session] = lambda: Session(f20_session.get_bind())
    client = TestClient(app)

    default_rows = client.get("/api/incidents").json()
    assert {item["source_id"] for item in default_rows} == {"src_a", "src_b"}
    assert default_rows[0]["source_name"].endswith("display")
    assert all(item["freshness_state"] == "FRESH" for item in default_rows)

    disabled_rows = client.get("/api/incidents?source_ids=src_disabled").json()
    assert [item["source_id"] for item in disabled_rows] == ["src_disabled"]

    assert client.get("/api/incidents?source_ids=src_archived").json() == []
    archived_rows = client.get(
        "/api/incidents?source_ids=src_archived&include_archived=true"
    ).json()
    assert [item["source_id"] for item in archived_rows] == ["src_archived"]


def _raw(fingerprint: str, cluster: str) -> dict:
    return {
        "fingerprint": fingerprint,
        "labels": {
            "alertname": "TargetDown",
            "severity": "critical",
            "cluster": cluster,
        },
        "annotations": {},
        "startsAt": "2026-07-22T00:00:00Z",
    }


def test_aggregation_rule_selected_scope_only_groups_selected_source(
    f20_session,
) -> None:
    _source(f20_session, "src_a", "ENABLED")
    _source(f20_session, "src_b", "ENABLED")
    f20_session.commit()
    rule = create_aggregation_rule(
        f20_session,
        name="target by cluster",
        priority=10,
        enabled=True,
        matchers=[{"label": "alertname", "operator": "=", "value": "TargetDown"}],
        group_by_labels=["cluster"],
        source_scope={"mode": "SELECTED", "source_ids": ["src_a"]},
    )
    f20_session.commit()
    ingest_alerts(
        f20_session,
        [_raw("same-upstream", "shared")],
        poll_time=datetime(2026, 7, 22, tzinfo=timezone.utc),
        source_id="src_a",
    )
    ingest_alerts(
        f20_session,
        [_raw("same-upstream", "shared")],
        poll_time=datetime(2026, 7, 22, tzinfo=timezone.utc),
        source_id="src_b",
    )
    f20_session.commit()

    rows = {
        item.source_id: item
        for item in f20_session.exec(select(Incident).order_by(Incident.source_id))
    }
    assert rows["src_a"].aggregation_rule_id == rule.id
    assert rows["src_b"].aggregation_rule_id is None
    assert "env=" not in rows["src_a"].group_key


def test_notification_policy_scope_replaces_environment_matcher() -> None:
    with pytest.raises(ValueError, match="stable Incident route field"):
        validate_policy_matchers(
            [{"field": "environment", "operator": "=", "value": "prod"}]
        )
    incident_a = Incident(
        id=1,
        source_id="src_a",
        environment="prod",
        group_key="source=src_a|rule=1|cluster=a",
        severity="critical",
        group_labels={"cluster": "a"},
    )
    incident_b = Incident(
        id=2,
        source_id="src_b",
        environment="prod",
        group_key="source=src_b|rule=1|cluster=a",
        severity="critical",
        group_labels={"cluster": "a"},
    )
    context = incident_route_context(incident_a)
    assert "environment" not in context
    assert context["source_id"] == "src_a"

    candidate = PolicyCandidate(
        id=10,
        logical_id="selected",
        name="selected",
        priority=1,
        matchers=[{"field": "severity", "operator": "=", "value": "critical"}],
        scope_mode="SELECTED",
        source_ids=("src_a",),
    )
    assert choose_policy(incident_route_context(incident_a), [candidate]).id == 10
    assert choose_policy(incident_route_context(incident_b), [candidate]) is None


def test_source_disable_marks_stale_and_pauses_reminders(f20_session) -> None:
    _source(f20_session, "src_a", "ENABLED")
    incident = _incident(f20_session, "src_a")
    channel = NotificationChannel(name="scope channel", state="ENABLED")
    f20_session.add(channel)
    f20_session.flush()
    revision = NotificationChannelRevision(
        channel_id=channel.id,
        version=1,
        state="ACTIVE",
        provider="FAKE",
        webhook_envelope={},
    )
    f20_session.add(revision)
    f20_session.flush()
    channel.active_revision_id = revision.id
    route = NotificationRoute(
        incident_id=incident.id,
        occurrence_no=incident.occurrence_no,
        policy_revision_id=1,
        policy_name="repeat",
        policy_version=1,
        policy_priority=1,
        repeat_interval_seconds=300,
        next_reminder_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
    )
    f20_session.add(route)
    f20_session.flush()
    f20_session.add(
        NotificationRouteTarget(
            route_id=route.id,
            channel_id=channel.id,
            routed_channel_revision_id=revision.id,
            channel_name=channel.name,
            provider="FAKE",
            channel_version=1,
            mention_mode="NONE",
            mention_users=[],
            mention_on={},
            opened_success_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
        )
    )
    f20_session.commit()

    disable_event_source(f20_session, "src_a", expected_version=1)
    f20_session.commit()
    stored = f20_session.get(Incident, incident.id)
    assert stored.source_state == "firing"
    assert stored.freshness_state == "STALE"
    assert plan_due_reminders(
        f20_session, now=datetime(2026, 7, 22, 0, 10, tzinfo=timezone.utc)
    ) == []
