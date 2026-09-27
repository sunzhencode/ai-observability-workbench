"""Local SQLite retention cleanup tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.registry_models import AlertEndpointObservation, F20Model
from app.models import (
    Alert,
    AlertTypeRule,
    GrafanaPanelConfig,
    GroupingPolicy,
    Incident,
    IncidentAudit,
    ConfigAudit,
    NotificationAttempt,
    NotificationChannel,
    NotificationChannelRevision,
    NotificationDelivery,
    NotificationPolicyRevision,
    NotificationRoute,
    NotificationRouteTarget,
    PanelMapping,
)
from app.services.retention import cleanup_expired_data


def test_retention_deletes_only_expired_runtime_data(session) -> None:
    now = datetime(2026, 7, 16, tzinfo=timezone.utc)
    old = now - timedelta(days=31)
    recent = now - timedelta(days=1)

    old_incident = Incident(group_key="old", title="old", updated_at=old)
    active_old_incident = Incident(
        group_key="active-old", title="active old", updated_at=old
    )
    recent_incident = Incident(group_key="recent", title="recent", updated_at=recent)
    session.add(old_incident)
    session.add(active_old_incident)
    session.add(recent_incident)
    session.commit()
    session.refresh(old_incident)
    session.refresh(recent_incident)

    session.add(
        Alert(
            fingerprint="old-alert",
            alertname="OldAlert",
            incident_id=old_incident.id,
            source_state="resolved",
            first_seen_at=old,
            last_seen_at=old,
        )
    )
    session.add(
        Alert(
            fingerprint="active-old-alert",
            alertname="StillFiring",
            incident_id=active_old_incident.id,
            source_state="firing",
            first_seen_at=old,
            last_seen_at=old,
        )
    )
    session.add(
        Alert(
            fingerprint="recent-alert",
            alertname="RecentAlert",
            incident_id=recent_incident.id,
            first_seen_at=recent,
            last_seen_at=recent,
        )
    )
    session.add(
        IncidentAudit(
            incident_id=old_incident.id,
            from_state="NEW",
            to_state="CLOSED",
            reason="old audit",
            created_at=old,
        )
    )
    session.add(
        GroupingPolicy(
            version=1,
            group_by=["alertname", "cluster", "severity"],
            enabled=False,
            created_at=old,
        )
    )
    session.add(
        GroupingPolicy(
            version=2,
            group_by=["alertname", "cluster", "severity"],
            enabled=True,
            created_at=old,
        )
    )
    session.add(
        PanelMapping(
            alertname="OldAlert",
            url="https://grafana.example/d/old",
            label="Keep config",
            created_at=old,
        )
    )
    session.add(
        AlertTypeRule(
            alertname="OldAlert",
            version=1,
            group_by_labels=["cluster"],
            enabled=True,
            created_at=old,
        )
    )
    session.add(
        GrafanaPanelConfig(
            alertname="OldAlert",
            dashboard_uid="ops",
            dashboard_title="Operations",
            panel_id="7",
            panel_title="Old alert panel",
            created_at=old,
        )
    )
    session.commit()

    result = cleanup_expired_data(session, now=now, retention_days=30)

    assert result.alerts_deleted == 1
    assert result.audits_deleted == 1
    assert result.policies_deleted == 1
    assert result.alert_type_rules_deleted == 0
    assert result.incidents_deleted == 1
    assert {alert.fingerprint for alert in session.exec(select(Alert)).all()} == {
        "active-old-alert",
        "recent-alert",
    }
    assert [policy.version for policy in session.exec(select(GroupingPolicy)).all()] == [2]
    assert len(session.exec(select(PanelMapping)).all()) == 1
    assert len(session.exec(select(AlertTypeRule)).all()) == 1
    assert len(session.exec(select(GrafanaPanelConfig)).all()) == 1
    assert {incident.group_key for incident in session.exec(select(Incident)).all()} == {
        "active-old",
        "recent",
    }


def test_retention_cleans_terminal_notification_history_but_keeps_active_work(
    session,
) -> None:
    now = datetime(2026, 7, 18, tzinfo=timezone.utc)
    old = now - timedelta(days=31)
    incident = Incident(group_key="notification-retention", title="retention")
    session.add(incident)
    session.flush()
    channel = NotificationChannel(name="retention channel")
    session.add(channel)
    session.flush()
    revision = NotificationChannelRevision(
        channel_id=channel.id,
        version=1,
        state="ACTIVE",
        webhook_envelope={"ciphertext": "retained"},
        created_at=old,
    )
    session.add(revision)
    session.flush()
    channel.active_revision_id = revision.id
    session.add(channel)
    policy = NotificationPolicyRevision(
        logical_id="retention-policy",
        version=1,
        name="retention policy",
        state="ACTIVE",
        created_at=old,
    )
    session.add(policy)
    session.flush()

    closed_route = NotificationRoute(
        incident_id=incident.id,
        occurrence_no=1,
        policy_revision_id=policy.id,
        policy_name=policy.name,
        policy_version=1,
        policy_priority=100,
        repeat_interval_seconds=3600,
        status="TERMINATED",
        created_at=old,
        closed_at=old,
    )
    active_route = NotificationRoute(
        incident_id=incident.id,
        occurrence_no=2,
        policy_revision_id=policy.id,
        policy_name=policy.name,
        policy_version=1,
        policy_priority=100,
        repeat_interval_seconds=3600,
        status="ACTIVE",
        created_at=old,
    )
    session.add(closed_route)
    session.add(active_route)
    session.flush()
    closed_target = NotificationRouteTarget(
        route_id=closed_route.id,
        channel_id=channel.id,
        routed_channel_revision_id=revision.id,
        channel_name=channel.name,
        provider="FEISHU_CUSTOM_BOT",
        channel_version=1,
        mention_mode="NONE",
    )
    active_target = NotificationRouteTarget(
        route_id=active_route.id,
        channel_id=channel.id,
        routed_channel_revision_id=revision.id,
        channel_name=channel.name,
        provider="FEISHU_CUSTOM_BOT",
        channel_version=1,
        mention_mode="NONE",
    )
    session.add(closed_target)
    session.add(active_target)
    session.flush()
    terminal = NotificationDelivery(
        event_key="retention-terminal",
        incident_id=incident.id,
        route_id=closed_route.id,
        route_target_id=closed_target.id,
        event_type="RECOVERED",
        incident_change_version=1,
        state="SUCCEEDED",
        created_at=old,
        updated_at=old,
        scheduled_at=old,
        next_attempt_at=old,
        succeeded_at=old,
    )
    pending = NotificationDelivery(
        event_key="retention-pending",
        incident_id=incident.id,
        route_id=active_route.id,
        route_target_id=active_target.id,
        event_type="FIRING_OPENED",
        incident_change_version=2,
        state="PENDING",
        created_at=old,
        updated_at=old,
        scheduled_at=old,
        next_attempt_at=old,
    )
    session.add(terminal)
    session.add(pending)
    session.flush()
    session.add(
        NotificationAttempt(
            delivery_id=terminal.id,
            attempt_no=1,
            started_at=old,
            finished_at=old,
            outcome="SUCCESS",
        )
    )
    session.add(
        ConfigAudit(
            resource_type="NOTIFICATION_CHANNEL",
            resource_id=str(channel.id),
            action="TEST",
            result="SUCCESS",
            created_at=old,
        )
    )
    session.commit()

    result = cleanup_expired_data(session, now=now, retention_days=30)

    assert result.notification_attempts_deleted == 1
    assert result.notification_deliveries_deleted == 1
    assert result.notification_route_targets_deleted == 1
    assert result.notification_routes_deleted == 1
    assert result.config_audits_deleted == 1
    assert [item.event_key for item in session.exec(select(NotificationDelivery)).all()] == [
        "retention-pending"
    ]
    assert session.get(NotificationRoute, active_route.id) is not None
    assert session.get(NotificationRouteTarget, active_target.id) is not None
    assert session.get(NotificationPolicyRevision, policy.id) is not None
    assert session.get(NotificationChannelRevision, revision.id) is not None


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


def test_retention_reclaims_endpoint_observations_of_deleted_alerts(
    f20_session,
) -> None:
    """An observation outlives its Alert only until the next retention pass.

    `alert_id` has no foreign key (Alert is in the other metadata), so nothing
    stops the row from lingering. It has to be swept explicitly or SQLite will
    eventually hand its `alert_id` to an unrelated Alert.
    """
    now = datetime(2026, 7, 16, tzinfo=timezone.utc)
    old = now - timedelta(days=31)

    expired = Alert(
        fingerprint="expired",
        alertname="Expired",
        source_state="resolved",
        first_seen_at=old,
        last_seen_at=old,
    )
    still_firing = Alert(
        fingerprint="still-firing",
        alertname="StillFiring",
        source_state="firing",
        first_seen_at=old,
        last_seen_at=old,
    )
    f20_session.add(expired)
    f20_session.add(still_firing)
    f20_session.commit()

    f20_session.add(
        AlertEndpointObservation(
            alert_id=int(expired.id), endpoint_revision_id=1, last_seen_at=old
        )
    )
    f20_session.add(
        AlertEndpointObservation(
            alert_id=int(still_firing.id), endpoint_revision_id=1, last_seen_at=old
        )
    )
    # An orphan left behind by an earlier release, before this sweep existed.
    f20_session.add(
        AlertEndpointObservation(
            alert_id=9_999, endpoint_revision_id=1, last_seen_at=old
        )
    )
    f20_session.commit()

    result = cleanup_expired_data(f20_session, now=now, retention_days=30)

    assert result.alerts_deleted == 1
    assert result.endpoint_observations_deleted == 2
    surviving = f20_session.exec(select(AlertEndpointObservation)).all()
    assert [item.alert_id for item in surviving] == [still_firing.id]


def test_retention_tolerates_a_database_without_the_f20_tables(session) -> None:
    """Pre-F20 databases have no observation table; the sweep must not care."""
    now = datetime(2026, 7, 16, tzinfo=timezone.utc)
    result = cleanup_expired_data(session, now=now, retention_days=30)
    assert result.endpoint_observations_deleted == 0
