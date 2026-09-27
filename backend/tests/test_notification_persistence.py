"""Notification configuration, route, Outbox, retry and UoW contracts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.adapters.notifications.providers import NotificationProviderRegistry, ScriptedFakeNotificationProvider
from app.adapters.persistence.incidents import SqlAlchemyIncidentStore
from app.adapters.persistence.notifications import (
    NotificationAttemptRecord,
    NotificationDeliveryRecord,
    NotificationPolicyAuditRecord,
    NotificationRouteRecord,
    SqlAlchemyNotificationStore,
)
from app.adapters.persistence.sources import IncidentRecord, SqlAlchemySourceStore
from app.api.v1.cursor import SignedCursorCodec
from app.application.notifications import (
    ActivationTokenService,
    ChannelDraft,
    ConfirmPolicyActivation,
    DeliverNotifications,
    PolicyDraft,
    PreparePolicyActivation,
    ProviderResult,
    SecretChange,
    TestNotificationChannel,
)
from app.application.sources import EndpointDraft, SourceDraft
from app.domains.notifications.models import (
    IncidentNotificationFact,
    Matcher,
    MatcherOperator,
    NotificationDeepLink,
    NotificationEnrichment,
    NotificationChange,
    NotificationMetricEvidence,
)
from app.domains.sources.models import EndpointObservation, merge_endpoint_observations
from app.platform.persistence.database import SqliteDatabaseConfig, create_session_factory, create_sqlite_engine
from app.platform.persistence.migrations import upgrade_database

UTC = timezone.utc


def _stores(tmp_path: Path, *, enrichment_reader=None):
    engine = create_sqlite_engine(SqliteDatabaseConfig(path=tmp_path / "incident-operations.db"))
    upgrade_database(engine)
    sessions = create_session_factory(engine)
    secrets: dict[str, str] = {}

    def encrypt(value: str) -> str:
        key = f"encrypted-{len(secrets) + 1}"
        secrets[key] = value
        return key

    source = SqlAlchemySourceStore(sessions)
    source.create_source(
        SourceDraft("source-a", "Primary", (EndpointDraft(0, "https://am.invalid"),)),
        now=datetime(2026, 8, 13, tzinfo=UTC),
    )
    source.seed_alert_for_characterization(
        source_id="source-a",
        raw={
            "fingerprint": "cpu-high",
            "labels": {"alertname": "CPUHigh", "severity": "warning", "cluster": "prod"},
            "annotations": {"summary": "CPU is above threshold", "token": "do-not-copy"},
            "startsAt": "2026-08-13T00:00:00Z",
        },
        observed_at=datetime(2026, 8, 13, 0, 1, tzinfo=UTC),
    )
    notifications = SqlAlchemyNotificationStore(
        sessions,
        encrypt_secret=encrypt,
        decrypt_secret=secrets.__getitem__,
        enrichment_reader=enrichment_reader,
        workbench_url="https://workbench.example.invalid",
    )
    return source, notifications, sessions, engine


async def _active_channel(store: SqlAlchemyNotificationStore, fake: ScriptedFakeNotificationProvider):
    channel = store.create_channel(
        ChannelDraft(
            "Primary Feishu",
            "FEISHU_CUSTOM_BOT",
            {"mention_mode": "NONE"},
            {"webhook": SecretChange("REPLACE", "https://open.feishu.cn/open-apis/bot/v2/hook/test-token")},
        ),
        now=datetime(2026, 8, 13, tzinfo=UTC),
    )
    tested = await TestNotificationChannel(store, NotificationProviderRegistry(fake=fake)).execute(
        channel.id,
        expected_revision=1,
        now=datetime(2026, 8, 13, 0, 2, tzinfo=UTC),
    )
    assert tested.last_test_code == "OK"
    return store.activate_channel(channel.id, expected_revision=1, now=datetime(2026, 8, 13, 0, 3, tzinfo=UTC))


def _fact(sessions) -> IncidentNotificationFact:
    with sessions() as session:
        incident = session.scalar(select(IncidentRecord))
        assert incident is not None
        return IncidentNotificationFact(
            incident.id,
            incident.occurrence_no,
            incident.source_id,
            "Primary",
            incident.title,
            incident.severity,
            incident.source_state,
            incident.freshness_state,
            incident.aggregation_rule_id,
            {"cluster": "prod"},
            1,
            ({"alertname": "CPUHigh", "severity": "warning", "source_state": "FIRING", "summary": "CPU is above threshold"},),
        )


async def _active_policy(store, channel_id: str):
    policy = store.create_policy(
        PolicyDraft(
            "Production critical",
            10,
            (Matcher("group.cluster", MatcherOperator.EQUAL, "prod"),),
            300,
            (channel_id,),
            "SELECTED",
            ("source-a",),
        ),
        now=datetime(2026, 8, 13, 0, 4, tzinfo=UTC),
    )
    tokens = ActivationTokenService(key=b"notification-test-key-at-least-32")
    prepared = PreparePolicyActivation(store, tokens).execute(
        policy.revision_id, expected_version=1, notify_existing=False
    )
    return ConfirmPolicyActivation(store, tokens).execute(
        policy.revision_id,
        expected_version=1,
        notify_existing=False,
        confirm_token=prepared.confirm_token,
        now=datetime(2026, 8, 13, 0, 5, tzinfo=UTC),
    )


async def test_channel_policy_route_and_retry_flow_is_persistent_and_secret_safe(tmp_path: Path) -> None:
    _source, store, sessions, _engine = _stores(tmp_path)
    fake = ScriptedFakeNotificationProvider(channel_test_script="OK", delivery_script="HTTP_500,OK")
    channel = await _active_channel(store, fake)
    await _active_policy(store, channel.id)
    fact = _fact(sessions)
    change = NotificationChange(None, "FIRING", None, "warning", None, "NEW", 1, "LIVE_POLL", datetime(2026, 8, 13, 0, 6, tzinfo=UTC))
    assert store.plan_change(fact, change) is not None
    assert len(store.list_deliveries()) == 1

    worker = DeliverNotifications(store, NotificationProviderRegistry(fake=fake))
    assert await worker.execute(now=datetime(2026, 8, 13, 0, 6, tzinfo=UTC)) == 1
    retrying = store.list_deliveries()[0]
    assert retrying.state == "RETRY_WAIT" and retrying.attempt_count == 1
    assert retrying.next_attempt_at > datetime(2026, 8, 13, 0, 6, tzinfo=UTC)
    assert await worker.execute(now=retrying.next_attempt_at) == 1
    succeeded = store.get_delivery(retrying.id)
    assert succeeded.state == "SUCCEEDED"
    assert [item.outcome for item in succeeded.attempts] == ["TRANSIENT_FAILURE", "SUCCESS"]
    assert "do-not-copy" not in repr(succeeded.payload_snapshot)
    database = (tmp_path / "incident-operations.db").read_bytes()
    assert b"test-token" not in database
    with sessions() as session:
        actions = tuple(
            session.scalars(
                select(NotificationPolicyAuditRecord.action).order_by(
                    NotificationPolicyAuditRecord.sequence
                )
            )
        )
    assert actions == ("CREATE_DRAFT", "ACTIVATE")


async def test_open_notification_uses_existing_metric_evidence_and_occurrence_deep_links(
    tmp_path: Path,
) -> None:
    def enrichment_reader(session, incident_id, occurrence_no, observed_at, include_evidence):
        del session, incident_id, occurrence_no, observed_at
        assert include_evidence is True
        return NotificationEnrichment(
            occurrence_id=42,
            metric_evidence=(
                NotificationMetricEvidence(
                    metric_id="checkout_http_error_ratio",
                    display_name="Checkout HTTP error ratio",
                    unit="ratio",
                    latest=0.08,
                    minimum=0.02,
                    maximum=0.08,
                ),
            ),
            deep_links=(
                NotificationDeepLink(
                    label="Checkout API / Error ratio",
                    url="https://grafana.example.invalid/d/checkout?viewPanel=7",
                ),
            ),
            evidence_status="AVAILABLE",
        )

    _source, store, sessions, _engine = _stores(
        tmp_path, enrichment_reader=enrichment_reader
    )
    channel = await _active_channel(store, ScriptedFakeNotificationProvider())
    await _active_policy(store, channel.id)
    opened_at = datetime(2026, 8, 13, 0, 6, tzinfo=UTC)
    store.plan_change(
        _fact(sessions),
        NotificationChange(
            None,
            "FIRING",
            None,
            "warning",
            None,
            "NEW",
            1,
            "LIVE_POLL",
            opened_at,
        ),
    )

    delivery = store.list_deliveries()[0]
    assert delivery.payload_snapshot["operational_occurrence_id"] == 42
    assert delivery.payload_snapshot["evidence_status"] == "AVAILABLE"
    assert delivery.payload_snapshot["metric_evidence"] == [
        {
            "display_name": "Checkout HTTP error ratio",
            "latest": 0.08,
            "maximum": 0.08,
            "metric_id": "checkout_http_error_ratio",
            "minimum": 0.02,
            "unit": "ratio",
        }
    ]
    claim = store.claim_deliveries(now=opened_at, batch_size=1, lease_seconds=60)[0]
    prepared = store.prepare_delivery(claim, now=opened_at)
    assert prepared is not None
    assert "工作台：https://workbench.example.invalid/incidents/42" in prepared.payload["text"]
    assert "Checkout HTTP error ratio：最新 0.08 · 范围 0.02–0.08 ratio" in prepared.payload["text"]
    assert "Grafana · Checkout API / Error ratio" in prepared.payload["text"]


async def test_evidence_projection_failure_never_delays_open_notification(
    tmp_path: Path,
) -> None:
    def broken_reader(session, incident_id, occurrence_no, observed_at, include_evidence):
        del session, incident_id, occurrence_no, observed_at, include_evidence
        raise RuntimeError("database detail must not escape")

    _source, store, sessions, _engine = _stores(
        tmp_path, enrichment_reader=broken_reader
    )
    channel = await _active_channel(store, ScriptedFakeNotificationProvider())
    await _active_policy(store, channel.id)
    opened_at = datetime(2026, 8, 13, 0, 6, tzinfo=UTC)
    store.plan_change(
        _fact(sessions),
        NotificationChange(
            None,
            "FIRING",
            None,
            "warning",
            None,
            "NEW",
            1,
            "LIVE_POLL",
            opened_at,
        ),
    )

    delivery = store.list_deliveries()[0]
    assert delivery.state == "PENDING"
    assert delivery.payload_snapshot["evidence_status"] == "UNAVAILABLE"
    assert delivery.payload_snapshot["evidence_safe_code"] == "NOTIFICATION_EVIDENCE_PROJECTION_FAILED"
    assert "database detail" not in repr(delivery.payload_snapshot)
    claim = store.claim_deliveries(now=opened_at, batch_size=1, lease_seconds=60)[0]
    prepared = store.prepare_delivery(claim, now=opened_at)
    assert prepared is not None
    assert "指标证据：本次未能附加；通知仍按时发送" in prepared.payload["text"]


async def test_reminder_reuses_prior_evidence_without_requerying_sources(
    tmp_path: Path,
) -> None:
    calls: list[bool] = []

    def enrichment_reader(session, incident_id, occurrence_no, observed_at, include_evidence):
        del session, incident_id, occurrence_no, observed_at
        calls.append(include_evidence)
        if not include_evidence:
            return NotificationEnrichment(occurrence_id=42, evidence_status="NOT_REQUESTED")
        return NotificationEnrichment(
            occurrence_id=42,
            metric_evidence=(
                NotificationMetricEvidence(
                    "checkout_http_error_ratio",
                    "Checkout error ratio",
                    "ratio",
                    0.08,
                    0.02,
                    0.08,
                ),
            ),
            evidence_status="AVAILABLE",
        )

    _source, store, sessions, _engine = _stores(
        tmp_path, enrichment_reader=enrichment_reader
    )
    fake = ScriptedFakeNotificationProvider()
    channel = await _active_channel(store, fake)
    await _active_policy(store, channel.id)
    opened_at = datetime(2026, 8, 13, 0, 6, tzinfo=UTC)
    store.plan_change(
        _fact(sessions),
        NotificationChange(
            None, "FIRING", None, "warning", None, "NEW", 1, "LIVE_POLL", opened_at
        ),
    )
    assert await DeliverNotifications(
        store, NotificationProviderRegistry(fake=fake)
    ).execute(now=opened_at) == 1

    assert store.plan_due_reminders(now=opened_at + timedelta(seconds=301)) == 1
    reminder = store.list_deliveries(event_type="REMINDER")[0]
    assert calls == [True, False]
    assert reminder.payload_snapshot["evidence_status"] == "REUSED"
    assert reminder.payload_snapshot["metric_evidence"] == [
        {
            "display_name": "Checkout error ratio",
            "latest": 0.08,
            "maximum": 0.08,
            "metric_id": "checkout_http_error_ratio",
            "minimum": 0.02,
            "unit": "ratio",
        }
    ]


async def test_route_locks_channel_revision_and_new_draft_does_not_change_delivery(tmp_path: Path) -> None:
    _source, store, sessions, _engine = _stores(tmp_path)
    fake = ScriptedFakeNotificationProvider()
    channel = await _active_channel(store, fake)
    await _active_policy(store, channel.id)
    store.plan_change(
        _fact(sessions),
        NotificationChange(None, "FIRING", None, "warning", None, "NEW", 1, "LIVE_POLL", datetime(2026, 8, 13, 0, 6, tzinfo=UTC)),
    )
    draft = store.update_channel(
        channel.id,
        ChannelDraft(
            channel.name,
            channel.provider,
            {"mention_mode": "ALL"},
            {"webhook": SecretChange("REPLACE", "https://open.feishu.cn/open-apis/bot/v2/hook/next-token")},
        ),
        expected_revision=1,
        now=datetime(2026, 8, 13, 0, 7, tzinfo=UTC),
    )
    assert draft.revision_no == 2 and channel.revision_no == 1
    claim = store.claim_deliveries(now=datetime(2026, 8, 13, 0, 8, tzinfo=UTC), batch_size=1, lease_seconds=60)[0]
    prepared = store.prepare_delivery(claim, now=datetime(2026, 8, 13, 0, 8, tzinfo=UTC))
    assert prepared is not None
    assert prepared.config["mention_mode"] == "NONE"
    assert prepared.secrets["webhook"].endswith("/test-token")


async def test_planner_can_share_outer_transaction_and_roll_back_outbox(tmp_path: Path) -> None:
    _source, store, sessions, _engine = _stores(tmp_path)
    channel = await _active_channel(store, ScriptedFakeNotificationProvider())
    await _active_policy(store, channel.id)
    fact = _fact(sessions)
    change = NotificationChange(None, "FIRING", None, "warning", None, "NEW", 1, "LIVE_POLL", datetime(2026, 8, 13, 0, 6, tzinfo=UTC))
    with pytest.raises(RuntimeError, match="rollback"):
        with sessions.begin() as session:
            store.plan_change_in_session(session, fact, change)
            raise RuntimeError("rollback")
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(NotificationRouteRecord)) == 0
        assert session.scalar(select(func.count()).select_from(NotificationDeliveryRecord)) == 0


async def test_source_incident_and_notification_outbox_share_one_committed_change(
    tmp_path: Path,
) -> None:
    _source, notifications, sessions, _engine = _stores(tmp_path)
    channel = await _active_channel(notifications, ScriptedFakeNotificationProvider())
    await _active_policy(notifications, channel.id)
    incidents = SqlAlchemyIncidentStore(
        sessions,
        cursor_codec=SignedCursorCodec(
            b"incident-notification-uow-key-at-least-32-bytes"
        ),
        notifications=notifications,
    )
    sources = SqlAlchemySourceStore(sessions, incident_reconciler=incidents)
    sources.publish_rule(
        name="memory by cluster",
        priority=10,
        enabled=True,
        matchers=(("alertname", "=", "MemoryHigh"),),
        group_by_labels=("cluster",),
        source_ids=("source-a",),
        now=datetime(2026, 8, 13, 0, 5, tzinfo=UTC),
    )
    snapshot = sources.load_snapshot("source-a", expected_version=1)
    raw = {
        "fingerprint": "memory-high",
        "labels": {
            "alertname": "MemoryHigh",
            "severity": "critical",
            "cluster": "prod",
        },
        "annotations": {"summary": "memory pressure"},
        "startsAt": "2026-08-13T00:06:00Z",
    }
    outcome = merge_endpoint_observations(
        (EndpointObservation(snapshot.endpoints[0], "SUCCESS", (raw,), 2),)
    )

    assert sources.apply_collection(
        snapshot,
        outcome,
        observed_at=datetime(2026, 8, 13, 0, 6, tzinfo=UTC),
    ).committed
    with sessions() as session:
        created = session.scalars(
            select(IncidentRecord).order_by(IncidentRecord.id.desc())
        ).first()
        assert created is not None and created.title.startswith("MemoryHigh")
        route = session.scalar(
            select(NotificationRouteRecord).where(
                NotificationRouteRecord.incident_id == created.id
            )
        )
        delivery = session.scalar(
            select(NotificationDeliveryRecord).where(
                NotificationDeliveryRecord.incident_id == created.id
            )
        )
        assert route is not None and route.occurrence_no == 1
        assert delivery is not None and delivery.event_type == "FIRING_OPENED"


async def test_expired_lease_records_ambiguous_attempt_and_manual_retry_is_explicit(tmp_path: Path) -> None:
    _source, store, sessions, _engine = _stores(tmp_path)
    channel = await _active_channel(store, ScriptedFakeNotificationProvider())
    await _active_policy(store, channel.id)
    store.plan_change(
        _fact(sessions),
        NotificationChange(None, "FIRING", None, "warning", None, "NEW", 1, "LIVE_POLL", datetime(2026, 8, 13, 0, 6, tzinfo=UTC)),
    )
    claim = store.claim_deliveries(now=datetime(2026, 8, 13, 0, 7, tzinfo=UTC), batch_size=1, lease_seconds=10)[0]
    prepared = store.prepare_delivery(claim, now=datetime(2026, 8, 13, 0, 7, tzinfo=UTC))
    assert prepared is not None
    reclaimed = store.claim_deliveries(now=datetime(2026, 8, 13, 0, 8, tzinfo=UTC), batch_size=1, lease_seconds=10)
    assert len(reclaimed) == 1
    assert store.finish_delivery(prepared, ProviderResult(True, "OK"), now=datetime(2026, 8, 13, 0, 8, tzinfo=UTC)) is False
    second = store.prepare_delivery(reclaimed[0], now=datetime(2026, 8, 13, 0, 8, tzinfo=UTC))
    assert second is not None
    store.finish_delivery(second, ProviderResult(False, "HTTP_400"), now=datetime(2026, 8, 13, 0, 8, tzinfo=UTC))
    failed = store.get_delivery(second.delivery_id)
    assert failed.state == "PERMANENTLY_FAILED"
    retried = store.retry_delivery(failed.id, now=datetime(2026, 8, 13, 0, 9, tzinfo=UTC))
    assert retried.state == "PENDING"


async def test_response_ack_pauses_reminders_but_escalation_and_recovery_remain_visible(tmp_path: Path) -> None:
    _source, store, sessions, _engine = _stores(tmp_path)
    fake = ScriptedFakeNotificationProvider()
    channel = await _active_channel(store, fake)
    await _active_policy(store, channel.id)
    fact = _fact(sessions)
    store.plan_change(
        fact,
        NotificationChange(None, "FIRING", None, "warning", None, "NEW", 1, "LIVE_POLL", datetime(2026, 8, 13, 0, 6, tzinfo=UTC)),
    )
    await DeliverNotifications(store, NotificationProviderRegistry(fake=fake)).execute(
        now=datetime(2026, 8, 13, 0, 6, tzinfo=UTC)
    )
    with sessions.begin() as session:
        store.apply_occurrence_response_in_session(
            session,
            incident_id=fact.incident_id,
            occurrence_no=fact.occurrence_no,
            response_state="IN_PROGRESS",
            observed_at=datetime(2026, 8, 13, 0, 7, tzinfo=UTC),
        )
    store.plan_change(
        fact,
        NotificationChange("FIRING", "FIRING", "warning", "critical", "NEW", "NEW", 2, "LIVE_POLL", datetime(2026, 8, 13, 0, 7, tzinfo=UTC)),
    )
    assert store.plan_due_reminders(now=datetime(2026, 8, 13, 1, tzinfo=UTC)) == 0
    escalated = store.list_deliveries(event_type="SEVERITY_ESCALATED")
    assert len(escalated) == 1
    recovered_fact = IncidentNotificationFact(
        fact.incident_id, fact.occurrence_no, fact.source_id, fact.source_name,
        fact.title, "critical", "RECOVERED", fact.freshness_state,
        fact.aggregation_rule_id, fact.group_labels, fact.member_count, fact.members,
    )
    store.plan_change(
        recovered_fact,
        NotificationChange("FIRING", "RECOVERED", "critical", "critical", "IN_PROGRESS", "IN_PROGRESS", 3, "LIVE_POLL", datetime(2026, 8, 13, 1, 1, tzinfo=UTC)),
    )
    assert len(store.list_deliveries(event_type="RECOVERED")) == 1


async def test_start_handling_cancels_an_already_planned_reminder_only(tmp_path: Path) -> None:
    _source, store, sessions, _engine = _stores(tmp_path)
    fake = ScriptedFakeNotificationProvider()
    channel = await _active_channel(store, fake)
    await _active_policy(store, channel.id)
    fact = _fact(sessions)
    opened_at = datetime(2026, 8, 13, 0, 6, tzinfo=UTC)
    store.plan_change(
        fact,
        NotificationChange(None, "FIRING", None, "warning", None, "NEW", 1, "LIVE_POLL", opened_at),
    )
    await DeliverNotifications(store, NotificationProviderRegistry(fake=fake)).execute(now=opened_at)
    with sessions.begin() as session:
        route = session.scalar(select(NotificationRouteRecord))
        assert route is not None
        route.next_reminder_at = opened_at.replace(tzinfo=None)
    assert store.plan_due_reminders(now=opened_at + timedelta(seconds=1)) == 1
    reminder = store.list_deliveries(event_type="REMINDER")[0]
    assert reminder.state == "PENDING"

    with sessions.begin() as session:
        store.apply_occurrence_response_in_session(
            session,
            incident_id=fact.incident_id,
            occurrence_no=fact.occurrence_no,
            response_state="IN_PROGRESS",
            observed_at=opened_at + timedelta(seconds=2),
        )

    canceled = store.get_delivery(reminder.id)
    assert canceled.state == "CANCELED"
    assert canceled.suppression_reason == "RESPONSE_IN_PROGRESS"
    store.plan_change(
        fact,
        NotificationChange("FIRING", "FIRING", "warning", "critical", "NEW", "NEW", 2, "LIVE_POLL", opened_at + timedelta(seconds=3)),
    )
    assert len(store.list_deliveries(event_type="SEVERITY_ESCALATED")) == 1


async def test_claimed_reminder_is_rechecked_against_current_response_before_send(tmp_path: Path) -> None:
    _source, store, sessions, _engine = _stores(tmp_path)
    fake = ScriptedFakeNotificationProvider()
    channel = await _active_channel(store, fake)
    await _active_policy(store, channel.id)
    fact = _fact(sessions)
    opened_at = datetime(2026, 8, 13, 0, 6, tzinfo=UTC)
    store.plan_change(
        fact,
        NotificationChange(None, "FIRING", None, "warning", None, "NEW", 1, "LIVE_POLL", opened_at),
    )
    await DeliverNotifications(store, NotificationProviderRegistry(fake=fake)).execute(now=opened_at)
    with sessions.begin() as session:
        route = session.scalar(select(NotificationRouteRecord))
        assert route is not None
        route.next_reminder_at = opened_at.replace(tzinfo=None)
    store.plan_due_reminders(now=opened_at + timedelta(seconds=1))
    claim = store.claim_deliveries(
        now=opened_at + timedelta(seconds=1), batch_size=1, lease_seconds=60
    )[0]

    with sessions.begin() as session:
        store.apply_occurrence_response_in_session(
            session,
            incident_id=fact.incident_id,
            occurrence_no=fact.occurrence_no,
            response_state="IN_PROGRESS",
            observed_at=opened_at + timedelta(seconds=2),
        )

    assert store.prepare_delivery(claim, now=opened_at + timedelta(seconds=3)) is None
    canceled = store.get_delivery(claim.delivery_id)
    assert canceled.state == "CANCELED"
    assert canceled.suppression_reason == "RESPONSE_IN_PROGRESS"


async def test_resolved_route_rejects_late_recovery_and_new_occurrence_gets_new_route(tmp_path: Path) -> None:
    _source, store, sessions, _engine = _stores(tmp_path)
    channel = await _active_channel(store, ScriptedFakeNotificationProvider())
    await _active_policy(store, channel.id)
    fact = _fact(sessions)
    opened_at = datetime(2026, 8, 13, 0, 6, tzinfo=UTC)
    first_route_id = store.plan_change(
        fact,
        NotificationChange(None, "FIRING", None, "warning", None, "NEW", 1, "LIVE_POLL", opened_at),
    )
    assert first_route_id is not None
    with sessions.begin() as session:
        store.apply_occurrence_response_in_session(
            session,
            incident_id=fact.incident_id,
            occurrence_no=fact.occurrence_no,
            response_state="RESOLVED",
            observed_at=opened_at + timedelta(minutes=1),
        )
    recovered_fact = IncidentNotificationFact(
        fact.incident_id, fact.occurrence_no, fact.source_id, fact.source_name,
        fact.title, fact.severity, "RECOVERED", fact.freshness_state,
        fact.aggregation_rule_id, fact.group_labels, fact.member_count, fact.members,
    )
    assert store.plan_change(
        recovered_fact,
        NotificationChange("FIRING", "RECOVERED", "warning", "warning", "NEW", "NEW", 2, "LIVE_POLL", opened_at + timedelta(minutes=2)),
    ) == first_route_id
    assert store.list_deliveries(event_type="RECOVERED") == ()

    next_fact = IncidentNotificationFact(
        fact.incident_id, fact.occurrence_no + 1, fact.source_id, fact.source_name,
        fact.title, "critical", "FIRING", fact.freshness_state,
        fact.aggregation_rule_id, fact.group_labels, fact.member_count, fact.members,
    )
    next_route_id = store.plan_change(
        next_fact,
        NotificationChange("RECOVERED", "FIRING", "warning", "critical", "NEW", "NEW", 3, "LIVE_POLL", opened_at + timedelta(minutes=3)),
    )
    assert next_route_id is not None and next_route_id != first_route_id
    with sessions() as session:
        routes = tuple(session.scalars(select(NotificationRouteRecord).order_by(NotificationRouteRecord.id)))
    assert [(route.occurrence_no, route.status) for route in routes] == [
        (fact.occurrence_no, "TERMINATED"),
        (fact.occurrence_no + 1, "ACTIVE"),
    ]


async def test_channel_rate_limit_requeues_without_creating_an_attempt(tmp_path: Path) -> None:
    _source, store, sessions, _engine = _stores(tmp_path)
    channel = await _active_channel(store, ScriptedFakeNotificationProvider())
    await _active_policy(store, channel.id)
    store.plan_change(
        _fact(sessions),
        NotificationChange(None, "FIRING", None, "warning", None, "NEW", 1, "LIVE_POLL", datetime(2026, 8, 13, 0, 6, tzinfo=UTC)),
    )
    delivery = store.list_deliveries()[0]
    now = datetime(2026, 8, 13, 0, 7, tzinfo=UTC)
    with sessions.begin() as session:
        session.add_all(
            NotificationAttemptRecord(
                delivery_id=delivery.id,
                attempt_no=100 + index,
                trigger="AUTO",
                started_at=(now - timedelta(milliseconds=100 * index)).replace(tzinfo=None),
                finished_at=now.replace(tzinfo=None),
                outcome="SUCCESS",
                http_status=200,
                provider_request_id=None,
                error_code=None,
            )
            for index in range(4)
        )
    claim = store.claim_deliveries(now=now, batch_size=1, lease_seconds=60)[0]
    assert store.prepare_delivery(claim, now=now) is None
    queued = store.get_delivery(delivery.id)
    assert queued.state == "PENDING" and queued.attempt_count == 0
    assert len(queued.attempts) == 4


async def test_delivery_filters_apply_before_limit_and_cursor(tmp_path: Path) -> None:
    """An older matching delivery remains discoverable behind a busy channel."""
    _source, store, sessions, engine = _stores(tmp_path)
    channel = await _active_channel(store, ScriptedFakeNotificationProvider())
    await _active_policy(store, channel.id)
    now = datetime(2026, 8, 13, 0, 6, tzinfo=UTC)
    store.plan_change(_fact(sessions), NotificationChange(
        None, "FIRING", None, "warning", None, "NEW", 1, "LIVE_POLL", now,
    ))
    original = store.list_deliveries()[0]
    from app.adapters.persistence.notifications import NotificationRouteTargetRecord
    second = store.create_channel(ChannelDraft(
        "Other channel", "GENERIC_WEBHOOK", {"url": "https://other.invalid/hook"}, {},
    ), now=now)
    with sessions.begin() as session:
        row = session.get(NotificationDeliveryRecord, original.id)
        assert row is not None
        target = session.get(NotificationRouteTargetRecord, row.route_target_id)
        assert target is not None
        other_target = NotificationRouteTargetRecord(**{
            column.name: getattr(target, column.name)
            for column in target.__table__.columns if column.name != "id"
        })
        other_target.channel_id = second.id
        session.add(other_target)
        session.flush()
        incident = session.get(IncidentRecord, original.incident_id)
        assert incident is not None
        other_incident = IncidentRecord(**{
            column.name: getattr(incident, column.name)
            for column in incident.__table__.columns if column.name != "id"
        })
        other_incident.group_key = "other-group"
        session.add(other_incident)
        session.flush()
        for index in range(205):
            clone = NotificationDeliveryRecord(**{
                column.name: getattr(row, column.name)
                for column in row.__table__.columns if column.name != "id"
            })
            clone.event_key = f"unrelated-{index}"
            clone.incident_id = other_incident.id
            clone.route_target_id = other_target.id
            session.add(clone)
    assert [item.id for item in store.list_deliveries(channel_id=channel.id)] == [original.id]
    assert [item.id for item in store.list_deliveries(incident_id=original.incident_id)] == [original.id]
    seen: list[int] = []
    before = None
    while True:
        page = store.list_deliveries(limit=37, before_id=before)
        if not page:
            break
        seen.extend(item.id for item in page)
        before = page[-1].id
    assert len(seen) == len(set(seen)) == 206
    assert seen == sorted(seen, reverse=True)
    engine.dispose()
