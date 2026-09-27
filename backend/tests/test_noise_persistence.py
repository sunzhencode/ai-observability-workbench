"""Persistence and notification integration contracts for deterministic noise."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import select, text

from app.adapters.notifications.providers import (
    NotificationProviderRegistry,
    ScriptedFakeNotificationProvider,
)
from app.adapters.persistence.incidents import (
    OperationalOccurrenceRecord,
    SqlAlchemyIncidentStore,
)
from app.adapters.persistence.noise import SqlAlchemyNoiseStore
from app.adapters.persistence.notifications import (
    NotificationDeliveryRecord,
    SqlAlchemyNotificationStore,
)
from app.adapters.persistence.sources import SqlAlchemySourceStore
from app.api.v1.cursor import SignedCursorCodec
from app.api.v1.operator_witness import OperatorWitness
from app.application.noise import MaintenanceDraft
from app.application.notifications import (
    ActivationTokenService,
    ChannelDraft,
    ConfirmPolicyActivation,
    PolicyDraft,
    PreparePolicyActivation,
    SecretChange,
    TestNotificationChannel,
)
from app.application.sources import EndpointDraft, SourceDraft
from app.domains.incidents.actors import SystemActor
from app.domains.notifications.models import Matcher, MatcherOperator
from app.domains.sources.models import EndpointObservation, merge_endpoint_observations
from app.platform.persistence.database import (
    SqliteDatabaseConfig,
    create_session_factory,
    create_sqlite_engine,
)
from app.platform.persistence.migrations import upgrade_database


UTC = timezone.utc


def _stores(tmp_path: Path):
    engine = create_sqlite_engine(SqliteDatabaseConfig(path=tmp_path / "noise.db"))
    upgrade_database(engine)
    sessions = create_session_factory(engine)
    secrets: dict[str, str] = {}

    def encrypt(value: str) -> str:
        key = f"encrypted-{len(secrets) + 1}"
        secrets[key] = value
        return key

    noise = SqlAlchemyNoiseStore(sessions)
    notifications = SqlAlchemyNotificationStore(
        sessions,
        encrypt_secret=encrypt,
        decrypt_secret=secrets.__getitem__,
        noise_decider=noise.resolve_notification_noise_in_session,
        storm_summary_claim=noise.claim_storm_summary_in_session,
    )
    incidents = SqlAlchemyIncidentStore(
        sessions,
        cursor_codec=SignedCursorCodec(b"noise-test-cursor-key-at-least-32-bytes"),
        notifications=notifications,
    )
    sources = SqlAlchemySourceStore(
        sessions,
        incident_reconciler=incidents,
        noise_observer=noise,
    )
    sources.create_source(
        SourceDraft(
            "src-a",
            "Primary AM",
            (EndpointDraft(0, "https://am.invalid"),),
            resolution_grace_seconds=0,
        ),
        now=datetime(2026, 8, 24, tzinfo=UTC),
    )
    return engine, sessions, sources, incidents, notifications, noise


def _raw(
    fingerprint: str,
    *,
    cluster: str = "cluster-a",
    severity: str = "warning",
) -> dict[str, object]:
    return {
        "fingerprint": fingerprint,
        "labels": {
            "alertname": "TargetDown",
            "severity": severity,
            "cluster": cluster,
        },
        "annotations": {"summary": f"{cluster} target unavailable"},
        "startsAt": "2026-08-24T00:00:00Z",
    }


def _apply(
    sources: SqlAlchemySourceStore,
    alerts: tuple[dict[str, object], ...],
    *,
    now: datetime,
) -> None:
    snapshot = sources.load_snapshot("src-a", expected_version=1)
    outcome = merge_endpoint_observations(
        (EndpointObservation(snapshot.endpoints[0], "SUCCESS", alerts, 1),)
    )
    assert sources.apply_collection(snapshot, outcome, observed_at=now).committed


async def _activate_notifications(
    store: SqlAlchemyNotificationStore,
    *,
    now: datetime,
) -> None:
    channel = store.create_channel(
        ChannelDraft(
            "Primary Feishu",
            "FEISHU_CUSTOM_BOT",
            {"mention_mode": "NONE"},
            {
                "webhook": SecretChange(
                    "REPLACE",
                    "https://open.feishu.cn/open-apis/bot/v2/hook/noise-test-token",
                )
            },
        ),
        now=now,
    )
    tested = await TestNotificationChannel(
        store,
        NotificationProviderRegistry(fake=ScriptedFakeNotificationProvider()),
    ).execute(channel.id, expected_revision=1, now=now)
    active = store.activate_channel(
        channel.id, expected_revision=tested.revision_no, now=now
    )
    policy = store.create_policy(
        PolicyDraft(
            "All source incidents",
            10,
            (),
            300,
            (active.id,),
            "SELECTED",
            ("src-a",),
        ),
        now=now,
    )
    tokens = ActivationTokenService(key=b"noise-policy-key-at-least-32-bytes")
    prepared = PreparePolicyActivation(store, tokens).execute(
        policy.revision_id, expected_version=1, notify_existing=False
    )
    ConfirmPolicyActivation(store, tokens).execute(
        policy.revision_id,
        expected_version=1,
        notify_existing=False,
        confirm_token=prepared.confirm_token,
        now=now,
    )


def test_system_actor_cannot_mutate_noise_configuration(tmp_path: Path) -> None:
    _engine, _sessions, _sources, _incidents, _notifications, noise = _stores(tmp_path)
    system = cast(Any, SystemActor("AI"))
    now = datetime(2026, 8, 24, 1, tzinfo=UTC)
    with pytest.raises(PermissionError, match="INTERACTIVE_OPERATOR_REQUIRED"):
        noise.update_source_controls(
            "src-a",
            flapping_enabled=False,
            storm_enabled=False,
            storm_alert_threshold=100,
            storm_occurrence_threshold=20,
            expected_version=1,
            actor=system,
            now=now,
        )
    with pytest.raises(PermissionError, match="INTERACTIVE_OPERATOR_REQUIRED"):
        noise.create_maintenance(
            MaintenanceDraft(
                "SOURCE",
                "src-a",
                None,
                None,
                now,
                now + timedelta(hours=1),
                "planned work",
            ),
            actor=system,
            now=now,
        )


def test_flapping_uses_complete_transitions_and_storm_clears_after_two_windows(
    tmp_path: Path,
) -> None:
    engine, _sessions, sources, incidents, _notifications, noise = _stores(tmp_path)
    t0 = datetime(2026, 8, 24, 1, tzinfo=UTC)
    _apply(sources, (_raw("target"),), now=t0)
    # Four trusted FIRING <-> RECOVERED transitions. Recovery requires the
    # existing two-complete-poll grace sequence even when grace is zero.
    _apply(sources, (), now=t0 + timedelta(minutes=1))
    _apply(sources, (), now=t0 + timedelta(minutes=2))
    _apply(sources, (_raw("target"),), now=t0 + timedelta(minutes=3))
    _apply(sources, (), now=t0 + timedelta(minutes=4))
    _apply(sources, (), now=t0 + timedelta(minutes=5))
    _apply(sources, (_raw("target"),), now=t0 + timedelta(minutes=6))
    occurrence = incidents.list_operational_occurrences(
        view="ALL",
        source_ids=(),
        signal_states=(),
        cursor=None,
        limit=50,
        now=t0 + timedelta(minutes=6),
    ).items[0]
    assert noise.occurrence_noise(
        occurrence.id, now=t0 + timedelta(minutes=6)
    ).state == "FLAPPING"

    noise.update_source_controls(
        "src-a",
        flapping_enabled=True,
        storm_enabled=True,
        storm_alert_threshold=10,
        storm_occurrence_threshold=20,
        expected_version=1,
        actor=OperatorWitness().actor(),
        now=t0 + timedelta(minutes=7),
    )
    alerts = tuple(
        _raw(f"storm-{index}", cluster=f"cluster-{index}") for index in range(10)
    )
    _apply(sources, alerts, now=t0 + timedelta(hours=1))
    assert noise.get_source_controls("src-a").storm_active is True
    _apply(sources, alerts, now=t0 + timedelta(hours=1, minutes=6))
    assert noise.get_source_controls("src-a").storm_active is True
    _apply(sources, alerts, now=t0 + timedelta(hours=1, minutes=12))
    assert noise.get_source_controls("src-a").storm_active is False
    with engine.connect() as connection:
        lifecycle = connection.execute(
            text(
                """
                SELECT kind, transition, count(*)
                FROM noise_lifecycle_fact
                GROUP BY kind, transition
                ORDER BY kind, transition
                """
            )
        ).all()
    assert ("FLAPPING", "ACTIVATED", 1) in lifecycle
    assert ("STORM", "ACTIVATED", 1) in lifecycle
    assert ("STORM", "CLEARED", 1) in lifecycle


@pytest.mark.asyncio
async def test_grouping_critical_bypass_and_storm_summary_are_persistent(
    tmp_path: Path,
) -> None:
    _engine, sessions, sources, _incidents, notifications, _noise = _stores(tmp_path)
    t0 = datetime(2026, 8, 24, 1, tzinfo=UTC)
    await _activate_notifications(notifications, now=t0)
    sources.publish_rule(
        name="targets by cluster",
        priority=10,
        enabled=True,
        matchers=(("alertname", "=", "TargetDown"),),
        group_by_labels=("cluster",),
        source_ids=("src-a",),
        now=t0,
        grouping_window_seconds=30,
    )
    _apply(sources, (_raw("grouped"),), now=t0 + timedelta(minutes=1))
    with sessions() as session:
        opening = session.scalar(select(NotificationDeliveryRecord))
        assert opening is not None
        assert opening.state == "PENDING"
        assert opening.scheduled_at == opening.created_at + timedelta(seconds=30)

    _apply(
        sources,
        (_raw("grouped", severity="critical"),),
        now=t0 + timedelta(minutes=1, seconds=10),
    )
    with sessions() as session:
        deliveries = tuple(
            session.scalars(
                select(NotificationDeliveryRecord).order_by(NotificationDeliveryRecord.id)
            )
        )
        assert [item.state for item in deliveries] == ["CANCELED", "PENDING"]
        assert deliveries[1].scheduled_at == deliveries[1].created_at

    # A new store keeps this scenario isolated from the critical incident.
    other = tmp_path / "storm"
    other.mkdir()
    _engine2, sessions2, sources2, _incidents2, notifications2, noise2 = _stores(other)
    await _activate_notifications(notifications2, now=t0)
    sources2.publish_rule(
        name="targets by cluster",
        priority=10,
        enabled=True,
        matchers=(("alertname", "=", "TargetDown"),),
        group_by_labels=("cluster",),
        source_ids=("src-a",),
        now=t0,
        grouping_window_seconds=30,
    )
    noise2.update_source_controls(
        "src-a",
        flapping_enabled=True,
        storm_enabled=True,
        storm_alert_threshold=10,
        storm_occurrence_threshold=20,
        expected_version=1,
        actor=OperatorWitness().actor(),
        now=t0 + timedelta(minutes=1),
    )
    storm_alerts = tuple(
        _raw(f"storm-{index}", cluster=f"cluster-{index}") for index in range(10)
    )
    _apply(sources2, storm_alerts, now=t0 + timedelta(minutes=2))
    with sessions2() as session:
        deliveries = tuple(session.scalars(select(NotificationDeliveryRecord)))
        assert sum(item.state == "PENDING" for item in deliveries) == 1
        assert sum(item.state == "SUPPRESSED" for item in deliveries) == 9
        summary = next(item for item in deliveries if item.state == "PENDING")
        assert '"state":"STORM"' in summary.payload_snapshot_json
        assert '"new_alert_count":10' in summary.payload_snapshot_json
        assert '"new_occurrence_count":10' in summary.payload_snapshot_json
        assert summary.scheduled_at == summary.created_at
        occurrence_id = session.scalar(
            select(OperationalOccurrenceRecord.id).order_by(
                OperationalOccurrenceRecord.id
            )
        )
        assert occurrence_id is not None
    visible = noise2.occurrence_noise(
        occurrence_id, now=t0 + timedelta(minutes=2)
    )
    assert visible.state == "STORM"
    assert visible.reason == (
        "最近 5 分钟新增 10 条告警、10 个事件，平台协作消息已按来源汇总"
    )
