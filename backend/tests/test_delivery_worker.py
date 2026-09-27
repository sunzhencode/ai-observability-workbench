"""Lease, retry, restart, and local rate-limit worker tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from app.config import settings
from app.crypto import SecretBox
from app.models import (
    Incident,
    NotificationAttempt,
    NotificationDelivery,
    NotificationRoute,
    NotificationRouteTarget,
)
from app.providers.feishu import FakeFeishuProvider, ProviderResult
from app.services.delivery_worker import (
    DeliveryWorker,
    claim_deliveries,
    next_rate_limit_permit,
    prepare_send,
)
from app.services.ingest import ingest_alerts
from app.services.notification_channels import (
    activate_channel_revision,
    create_channel,
    test_channel_revision,
)
from app.services.notification_policies import activate_policy, create_policy_draft


def _raw(fingerprint: str, severity: str = "warning") -> dict:
    return {
        "fingerprint": fingerprint,
        "labels": {"alertname": "TargetDown", "severity": severity},
        "annotations": {"summary": f"target {fingerprint} down"},
    }


async def _setup(
    session, monkeypatch, *, alert_count: int = 1, channel_count: int = 1
) -> datetime:
    monkeypatch.setattr(settings, "master_key", "worker-master-key")
    box = SecretBox(settings.master_key)
    channel_ids: list[int] = []
    for index in range(channel_count):
        channel, revision = create_channel(
            session,
            name=f"worker channel {index}",
            webhook_action="REPLACE",
            webhook_value=(
                "https://open.feishu.cn/open-apis/bot/v2/hook/"
                f"worker-token-{index}"
            ),
            signing_action="CLEAR",
            signing_value=None,
            required_keyword="Alert Workbench",
            mention_mode="NONE",
            mention_users=[],
            mention_on={},
            box=box,
        )
        await test_channel_revision(
            session, revision.id, box=box, provider=FakeFeishuProvider()
        )
        activate_channel_revision(session, revision.id, expected_version=1, box=box)
        channel_ids.append(channel.id)
    policy = create_policy_draft(
        session,
        name="worker catch all",
        priority=100,
        matchers=[],
        repeat_interval_seconds=3600,
        channel_ids=channel_ids,
    )
    activate_policy(session, policy.id, expected_version=1)
    session.commit()
    now = datetime(2026, 7, 18, 3, 0, tzinfo=timezone.utc)
    ingest_alerts(
        session,
        [_raw(f"worker-{index}") for index in range(alert_count)],
        poll_time=now,
    )
    return now


async def test_worker_success_updates_attempt_target_and_next_reminder(
    session, monkeypatch
) -> None:
    now = await _setup(session, monkeypatch)
    provider = FakeFeishuProvider()
    worker = DeliveryWorker(session.get_bind(), provider)

    assert await worker.run_once(now=now + timedelta(seconds=1)) == 1
    session.expire_all()
    delivery = session.exec(select(NotificationDelivery)).one()
    attempt = session.exec(select(NotificationAttempt)).one()
    route = session.exec(select(NotificationRoute)).one()
    target = session.exec(select(NotificationRouteTarget)).one()
    assert delivery.state == "SUCCEEDED"
    assert attempt.outcome == "SUCCESS"
    assert target.opened_success_at is not None
    assert route.next_reminder_at == datetime(2026, 7, 18, 4, 0, 1)
    assert len(provider.calls) == 1


async def test_worker_cancels_superseded_incident_without_provider_call(
    session, monkeypatch
) -> None:
    now = await _setup(session, monkeypatch)
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
    session.commit()

    provider = FakeFeishuProvider()
    worker = DeliveryWorker(session.get_bind(), provider)

    assert await worker.run_once(now=now + timedelta(seconds=1)) == 0
    session.expire_all()
    delivery = session.exec(select(NotificationDelivery)).one()
    assert delivery.state == "CANCELED"
    assert delivery.suppression_reason == "INCIDENT_SUPERSEDED"
    assert provider.calls == []


async def test_transient_failure_retries_and_succeeds(session, monkeypatch) -> None:
    now = await _setup(session, monkeypatch)
    provider = FakeFeishuProvider(
        [
            ProviderResult(False, "HTTP_500", transient=True, http_status=500),
            ProviderResult(True, "OK", http_status=200),
        ]
    )
    worker = DeliveryWorker(session.get_bind(), provider)
    await worker.run_once(now=now)
    session.expire_all()
    delivery = session.exec(select(NotificationDelivery)).one()
    assert delivery.state == "RETRY_WAIT"
    retry_at = delivery.next_attempt_at.replace(tzinfo=timezone.utc)

    await worker.run_once(now=retry_at)
    session.expire_all()
    assert session.exec(select(NotificationDelivery)).one().state == "SUCCEEDED"
    attempts = session.exec(
        select(NotificationAttempt).order_by(NotificationAttempt.attempt_no)
    ).all()
    assert [item.outcome for item in attempts] == ["TRANSIENT_FAILURE", "SUCCESS"]


async def test_expired_lease_marks_ambiguous_and_can_be_reclaimed(
    session, monkeypatch
) -> None:
    now = await _setup(session, monkeypatch)
    claim = claim_deliveries(session, now=now, lease_seconds=10)[0]
    session.commit()
    prepared = prepare_send(session, claim, now=now)
    session.commit()
    assert prepared is not None

    reclaimed = claim_deliveries(session, now=now + timedelta(seconds=11))
    session.commit()
    assert len(reclaimed) == 1
    assert reclaimed[0].lease_token != claim.lease_token
    attempt = session.exec(select(NotificationAttempt)).one()
    assert attempt.outcome == "AMBIGUOUS"
    assert attempt.error_code == "LEASE_EXPIRED"


async def test_rate_limit_queues_fifth_send_without_counting_attempt(
    session, monkeypatch
) -> None:
    now = await _setup(session, monkeypatch, alert_count=5)
    provider = FakeFeishuProvider()
    worker = DeliveryWorker(session.get_bind(), provider, batch_size=10)
    assert await worker.run_once(now=now) == 4
    session.expire_all()
    deliveries = session.exec(
        select(NotificationDelivery).order_by(NotificationDelivery.id)
    ).all()
    assert sum(item.state == "SUCCEEDED" for item in deliveries) == 4
    queued = [item for item in deliveries if item.state == "PENDING"]
    assert len(queued) == 1
    assert queued[0].attempt_count == 0
    assert queued[0].next_attempt_at > now.replace(tzinfo=None)


async def test_partial_channel_success_merges_escalation_into_unopened_target(
    session, monkeypatch
) -> None:
    now = await _setup(session, monkeypatch, channel_count=2)
    provider = FakeFeishuProvider(
        [
            ProviderResult(True, "OK"),
            ProviderResult(False, "HTTP_500", transient=True),
        ]
    )
    worker = DeliveryWorker(session.get_bind(), provider)
    assert await worker.run_once(now=now) == 2
    session.expire_all()
    targets = session.exec(
        select(NotificationRouteTarget).order_by(NotificationRouteTarget.id)
    ).all()
    assert targets[0].opened_success_at is not None
    assert targets[1].opened_success_at is None

    ingest_alerts(
        session,
        [_raw("worker-0", severity="critical")],
        poll_time=now + timedelta(minutes=1),
    )
    deliveries = session.exec(
        select(NotificationDelivery).order_by(NotificationDelivery.id)
    ).all()
    first_target_events = [
        item.event_type for item in deliveries if item.route_target_id == targets[0].id
    ]
    second_target = [
        item for item in deliveries if item.route_target_id == targets[1].id
    ]
    assert first_target_events == ["FIRING_OPENED", "SEVERITY_ESCALATED"]
    assert [item.event_type for item in second_target] == [
        "FIRING_OPENED",
        "FIRING_OPENED",
    ]
    assert second_target[0].state == "CANCELED"
    assert second_target[1].payload_snapshot_json["severity"] == "critical"


async def test_fifth_transient_failure_becomes_permanent(session, monkeypatch) -> None:
    now = await _setup(session, monkeypatch)
    provider = FakeFeishuProvider(
        [ProviderResult(False, "HTTP_500", transient=True) for _ in range(5)]
    )
    worker = DeliveryWorker(session.get_bind(), provider)
    current = now
    for _ in range(5):
        await worker.run_once(now=current)
        session.expire_all()
        delivery = session.exec(select(NotificationDelivery)).one()
        current = delivery.next_attempt_at.replace(tzinfo=timezone.utc)
    assert delivery.state == "PERMANENT_FAILED"
    assert delivery.attempt_count == 5
    assert len(session.exec(select(NotificationAttempt)).all()) == 5


async def test_minute_budget_defers_ninety_first_attempt(session, monkeypatch) -> None:
    now = await _setup(session, monkeypatch)
    delivery = session.exec(select(NotificationDelivery)).one()
    target = session.exec(select(NotificationRouteTarget)).one()
    for attempt_no in range(1, 91):
        session.add(
            NotificationAttempt(
                delivery_id=delivery.id,
                attempt_no=attempt_no,
                started_at=now - timedelta(seconds=30),
                finished_at=now - timedelta(seconds=29),
                outcome="SUCCESS",
            )
        )
    session.commit()
    permit = next_rate_limit_permit(
        session, channel_id=target.channel_id, now=now
    )
    assert permit == now + timedelta(seconds=30)


async def test_worker_sends_through_the_provider_the_route_recorded(
    session, monkeypatch
) -> None:
    """F24: the implementation comes from the route, not from the worker.

    A route locks its destination when the occurrence is first routed, and that
    includes how the message gets there -- so a channel later rebuilt on another
    kind must not silently redirect an in-flight delivery.
    """

    now = await _setup(session, monkeypatch)
    asked: list[str] = []
    provider = FakeFeishuProvider()

    def resolve(kind: str):
        asked.append(kind)
        return provider

    worker = DeliveryWorker(session.get_bind(), resolve_provider=resolve)

    assert await worker.run_once(now=now + timedelta(seconds=1)) == 1
    assert asked == ["FEISHU_CUSTOM_BOT"]
    assert len(provider.calls) == 1


async def test_a_kind_this_build_cannot_send_fails_permanently(
    session, monkeypatch
) -> None:
    """A route can name a kind this build has no implementation for.

    It happens while a kind is declared but unbuilt, and it would happen to a
    database carried back to an older build. Either way the delivery must stop,
    not spin through five retries against something that will never exist here.
    """

    now = await _setup(session, monkeypatch)
    target = session.exec(select(NotificationRouteTarget)).one()
    target.provider = "SLACK"
    session.add(target)
    session.commit()

    worker = DeliveryWorker(session.get_bind())

    # run_once returns deliveries processed, not deliveries delivered.
    assert await worker.run_once(now=now + timedelta(seconds=1)) == 1
    session.expire_all()
    delivery = session.exec(select(NotificationDelivery)).one()
    attempt = session.exec(select(NotificationAttempt)).one()
    assert delivery.state == "PERMANENT_FAILED"
    assert attempt.outcome != "SUCCESS"
    assert attempt.error_code == "UNSUPPORTED_PROVIDER"
