"""Incident-level notification lifecycle planning tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlmodel import select

from app.crypto import SecretBox
from app.models import (
    Alert,
    Incident,
    IncidentAudit,
    NotificationDelivery,
    NotificationRoute,
    NotificationRouteTarget,
)
from app.providers.feishu import FakeFeishuProvider
from app.services.aggregation_rules import create_aggregation_rule
from app.services.handling import change_handling_state
from app.services.ingest import ingest_alerts
from app.services.notification_channels import (
    activate_channel_revision,
    create_channel,
    disable_channel,
    test_channel_revision,
)
from app.services.notification_policies import (
    activate_policy,
    create_policy_draft,
    update_policy_draft,
)
from app.services.notification_planner import plan_due_reminders, plan_handling_change


def _raw(fingerprint: str, severity: str = "warning", starts_at: str | None = None):
    return {
        "fingerprint": fingerprint,
        "labels": {
            "alertname": "TargetDown",
            "severity": severity,
            "cluster": "qa-a",
            "namespace": "payments",
        },
        "annotations": {"summary": f"target {fingerprint} down"},
        "startsAt": starts_at or "2026-07-18T01:00:00Z",
    }


async def _configured_policy(session) -> None:
    box = SecretBox("planner-channel-key")
    channel, revision = create_channel(
        session,
        name="planner channel",
        webhook_action="REPLACE",
        webhook_value="https://open.feishu.cn/open-apis/bot/v2/hook/planner-token",
        signing_action="CLEAR",
        signing_value=None,
        required_keyword=None,
        mention_mode="NONE",
        mention_users=[],
        mention_on={},
        box=box,
    )
    await test_channel_revision(
        session,
        revision.id,
        box=box,
        provider=FakeFeishuProvider(),
    )
    activate_channel_revision(session, revision.id, expected_version=1, box=box)
    policy = create_policy_draft(
        session,
        name="all incidents",
        priority=100,
        matchers=[],
        repeat_interval_seconds=14_400,
        channel_ids=[channel.id],
    )
    activate_policy(session, policy.id, expected_version=1)
    session.commit()


async def test_multiple_alert_members_create_one_route_and_one_delivery(session) -> None:
    await _configured_policy(session)
    create_aggregation_rule(
        session,
        name="targets by cluster namespace",
        priority=1,
        enabled=True,
        matchers=[{"label": "alertname", "operator": "=", "value": "TargetDown"}],
        group_by_labels=["cluster", "namespace"],
    )
    now = datetime(2026, 7, 18, 2, 0, tzinfo=timezone.utc)
    ingest_alerts(
        session,
        [_raw(f"member-{index:02d}") for index in range(20)],
        poll_time=now,
    )

    incident = session.exec(select(Incident)).one()
    assert len(
        session.exec(select(Alert).where(Alert.incident_id == incident.id)).all()
    ) == 20
    assert len(session.exec(select(NotificationRoute)).all()) == 1
    assert len(session.exec(select(NotificationRouteTarget)).all()) == 1
    deliveries = session.exec(select(NotificationDelivery)).all()
    assert len(deliveries) == 1
    assert deliveries[0].event_type == "FIRING_OPENED"
    assert deliveries[0].payload_snapshot_json["member_count"] == 20


async def test_same_severity_member_change_is_silent_but_escalation_is_planned(session) -> None:
    await _configured_policy(session)
    create_aggregation_rule(
        session,
        name="targets by cluster namespace",
        priority=1,
        enabled=True,
        matchers=[{"label": "alertname", "operator": "=", "value": "TargetDown"}],
        group_by_labels=["cluster", "namespace"],
    )
    now = datetime(2026, 7, 18, 2, 0, tzinfo=timezone.utc)
    ingest_alerts(session, [_raw("one")], poll_time=now)
    target = session.exec(select(NotificationRouteTarget)).one()
    target.opened_success_at = now
    session.add(target)
    session.commit()

    ingest_alerts(
        session,
        [_raw("one"), _raw("two")],
        poll_time=now + timedelta(minutes=1),
    )
    assert len(session.exec(select(NotificationDelivery)).all()) == 1

    ingest_alerts(
        session,
        [_raw("one", "critical"), _raw("two")],
        poll_time=now + timedelta(minutes=2),
    )
    events = [item.event_type for item in session.exec(select(NotificationDelivery)).all()]
    assert events == ["FIRING_OPENED", "SEVERITY_ESCALATED"]


async def test_superseded_incident_does_not_plan_repeat(session) -> None:
    await _configured_policy(session)
    now = datetime(2026, 7, 18, 3, 0, tzinfo=timezone.utc)
    ingest_alerts(session, [_raw("superseded")], poll_time=now)
    historical = session.exec(select(Incident)).one()
    canonical = Incident(
        group_key="source=safe|rule=1|cluster=canonical",
        source_id=historical.source_id,
        source_state="firing",
    )
    session.add(canonical)
    session.flush()
    historical.superseded_by_incident_id = canonical.id
    session.add(historical)
    route = session.exec(select(NotificationRoute)).one()
    route.next_reminder_at = now
    session.add(route)
    target = session.exec(select(NotificationRouteTarget)).one()
    target.opened_success_at = now
    session.add(target)
    session.commit()

    before = len(session.exec(select(NotificationDelivery)).all())
    assert plan_due_reminders(session, now=now + timedelta(seconds=1)) == []
    assert len(session.exec(select(NotificationDelivery)).all()) == before


async def test_recovery_then_live_recurrence_opens_new_occurrence_and_resets_handling(
    session,
) -> None:
    await _configured_policy(session)
    start = datetime(2026, 7, 18, 2, 0, tzinfo=timezone.utc)
    ingest_alerts(session, [_raw("one")], poll_time=start, resolution_grace_seconds=0)
    incident = session.exec(select(Incident)).one()
    target = session.exec(select(NotificationRouteTarget)).one()
    target.opened_success_at = start
    session.add(target)
    change_handling_state(session, incident, to_state="CLOSED", reason="handled")
    plan_handling_change(
        session,
        incident,
        previous_handling_state="NEW",
        observed_at=start + timedelta(seconds=10),
    )
    session.commit()

    ingest_alerts(session, [], poll_time=start + timedelta(minutes=1), resolution_grace_seconds=0)
    ingest_alerts(session, [], poll_time=start + timedelta(minutes=2), resolution_grace_seconds=0)
    session.refresh(incident)
    assert incident.source_state == "recovered"

    ingest_alerts(
        session,
        [_raw("one", starts_at="2026-07-18T02:03:00Z")],
        poll_time=start + timedelta(minutes=3),
        resolution_grace_seconds=0,
    )
    session.refresh(incident)
    assert incident.occurrence_no == 2
    assert incident.handling_state == "NEW"
    assert len(session.exec(select(NotificationRoute)).all()) == 2
    assert session.exec(
        select(IncidentAudit).where(IncidentAudit.reason == "source_recurrence")
    ).one().actor == "system"


async def test_backfill_and_regroup_origins_do_not_create_routes(session) -> None:
    await _configured_policy(session)
    ingest_alerts(
        session,
        [_raw("backfill")],
        poll_time=datetime(2026, 7, 18, 2, 0, tzinfo=timezone.utc),
        origin="backfill",
        reconcile_lifecycle=False,
    )
    assert session.exec(select(NotificationRoute)).all() == []


async def test_existing_route_snapshot_does_not_drift_after_policy_revision(session) -> None:
    await _configured_policy(session)
    now = datetime(2026, 7, 18, 2, 0, tzinfo=timezone.utc)
    ingest_alerts(session, [_raw("one")], poll_time=now)
    route = session.exec(select(NotificationRoute)).one()
    target = session.exec(select(NotificationRouteTarget)).one()
    policy_id = route.policy_revision_id
    from app.models import NotificationPolicyRevision

    active = session.get(NotificationPolicyRevision, policy_id)
    draft = update_policy_draft(
        session,
        active.logical_id,
        expected_version=1,
        name="changed name",
        priority=1,
        matchers=[],
        repeat_interval_seconds=300,
        channel_ids=[target.channel_id],
    )
    activate_policy(session, draft.id, expected_version=2)
    session.commit()
    session.refresh(route)
    session.refresh(target)

    assert route.policy_revision_id == policy_id
    assert route.policy_name == "all incidents"
    assert route.policy_version == 1
    assert route.repeat_interval_seconds == 14_400
    assert target.channel_version == 1


async def test_aggregation_regroup_terminates_old_route_without_new_delivery(session) -> None:
    await _configured_policy(session)
    now = datetime(2026, 7, 18, 2, 0, tzinfo=timezone.utc)
    ingest_alerts(session, [_raw("one")], poll_time=now)
    old_route = session.exec(select(NotificationRoute)).one()
    old_delivery = session.exec(select(NotificationDelivery)).one()

    create_aggregation_rule(
        session,
        name="regroup target",
        priority=1,
        enabled=True,
        matchers=[{"label": "alertname", "operator": "=", "value": "TargetDown"}],
        group_by_labels=["cluster", "namespace"],
        changed_at=now + timedelta(minutes=1),
    )
    session.refresh(old_route)
    session.refresh(old_delivery)
    assert old_route.status == "TERMINATED"
    assert old_route.termination_reason == "REGROUP"
    assert old_delivery.state == "CANCELED"
    assert len(session.exec(select(NotificationRoute)).all()) == 1
    assert len(session.exec(select(NotificationDelivery)).all()) == 1


async def test_due_reminder_is_single_and_paused_by_handling_state(session) -> None:
    await _configured_policy(session)
    now = datetime(2026, 7, 18, 2, 0, tzinfo=timezone.utc)
    ingest_alerts(session, [_raw("one")], poll_time=now)
    route = session.exec(select(NotificationRoute)).one()
    target = session.exec(select(NotificationRouteTarget)).one()
    target.opened_success_at = now
    route.next_reminder_at = now + timedelta(hours=4)
    session.add(target)
    session.add(route)
    session.commit()

    due = now + timedelta(hours=8)
    assert len(plan_due_reminders(session, now=due)) == 1
    assert plan_due_reminders(session, now=due + timedelta(hours=8)) == []
    session.commit()
    assert route.repeat_slot == 1

    reminder = session.exec(
        select(NotificationDelivery).where(NotificationDelivery.event_type == "REMINDER")
    ).one()
    reminder.state = "CANCELED"
    session.add(reminder)
    incident = session.exec(select(Incident)).one()
    incident.handling_state = "IN_PROGRESS"
    session.add(incident)
    session.commit()
    assert plan_due_reminders(session, now=due + timedelta(hours=8)) == []


async def test_terminal_reminder_waits_for_the_next_interval(session) -> None:
    """A reminder that fails for good must not re-open on the next worker tick.

    The outstanding-delivery guard only holds while a reminder is still in
    flight. Once it reaches a terminal state the route has to be out of the due
    window on its own, or the 5s delivery tick becomes the effective repeat
    interval.
    """
    await _configured_policy(session)
    now = datetime(2026, 7, 18, 2, 0, tzinfo=timezone.utc)
    ingest_alerts(session, [_raw("one")], poll_time=now)
    route = session.exec(select(NotificationRoute)).one()
    target = session.exec(select(NotificationRouteTarget)).one()
    target.opened_success_at = now
    route.next_reminder_at = now
    session.add(target)
    session.add(route)
    session.commit()

    assert len(plan_due_reminders(session, now=now)) == 1
    session.commit()
    assert route.next_reminder_at is not None

    reminder = session.exec(
        select(NotificationDelivery).where(
            NotificationDelivery.event_type == "REMINDER"
        )
    ).one()
    reminder.state = "PERMANENT_FAILED"
    session.add(reminder)
    session.commit()

    incident = session.exec(select(Incident)).one()
    assert incident.source_state == "firing"
    assert incident.handling_state == "NEW"

    # The next delivery ticks arrive seconds later, well inside the 4h repeat.
    assert plan_due_reminders(session, now=now + timedelta(seconds=5)) == []
    assert plan_due_reminders(session, now=now + timedelta(minutes=30)) == []
    session.commit()
    assert route.repeat_slot == 1

    # The interval itself still works: a genuinely due route opens one more slot.
    assert len(plan_due_reminders(session, now=now + timedelta(hours=4))) == 1
    session.commit()
    assert route.repeat_slot == 2
    assert (
        len(
            session.exec(
                select(NotificationDelivery).where(
                    NotificationDelivery.event_type == "REMINDER"
                )
            ).all()
        )
        == 2
    )


async def test_offline_gap_replays_one_reminder_not_every_missed_slot(session) -> None:
    """A workbench asleep for a day owes one reminder, not a day's worth."""
    await _configured_policy(session)
    now = datetime(2026, 7, 18, 2, 0, tzinfo=timezone.utc)
    ingest_alerts(session, [_raw("one")], poll_time=now)
    route = session.exec(select(NotificationRoute)).one()
    target = session.exec(select(NotificationRouteTarget)).one()
    target.opened_success_at = now
    route.next_reminder_at = now
    session.add(target)
    session.add(route)
    session.commit()

    wake = now + timedelta(hours=24)
    assert len(plan_due_reminders(session, now=wake)) == 1
    session.commit()
    # The catch-up slot is armed from the wake-up, so the backlog is not replayed.
    # SQLite hands the column back naive; compare it as the UTC it was written as.
    armed = route.next_reminder_at.replace(tzinfo=timezone.utc)
    assert armed == wake + timedelta(seconds=route.repeat_interval_seconds)


async def test_channel_disable_suppresses_pending_without_rerouting(session) -> None:
    await _configured_policy(session)
    now = datetime(2026, 7, 18, 2, 0, tzinfo=timezone.utc)
    ingest_alerts(session, [_raw("one")], poll_time=now)
    route = session.exec(select(NotificationRoute)).one()
    target = session.exec(select(NotificationRouteTarget)).one()
    delivery = session.exec(select(NotificationDelivery)).one()

    disable_channel(session, target.channel_id)
    session.commit()
    session.refresh(route)
    session.refresh(delivery)
    assert route.status == "ACTIVE"
    assert delivery.state == "SUPPRESSED"
    assert delivery.suppression_reason == "CHANNEL_DISABLED"
    assert len(session.exec(select(NotificationRouteTarget)).all()) == 1
