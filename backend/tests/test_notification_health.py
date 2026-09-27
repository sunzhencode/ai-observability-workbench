"""Notification health is independent from Alertmanager source health."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

from app.api import health, incidents
from app.config import settings
from app.crypto import SecretBox
from app.db import get_session
from app.models import NotificationChannelRevision, NotificationDelivery
from app.services.ingest import ingest_alerts
from app.services.notification_channels import (
    activate_channel_revision,
    create_channel,
)
from app.services.notification_policies import activate_policy, create_policy_draft
from app.state import notification_status, poll_status


def _client(session) -> TestClient:
    app = FastAPI()
    app.include_router(health.router, prefix="/api")
    app.include_router(incidents.router, prefix="/api")
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def _activate_notification_config(session, monkeypatch) -> None:
    monkeypatch.setattr(settings, "master_key", "health-master-key")
    box = SecretBox(settings.master_key)
    channel, revision = create_channel(
        session,
        name="health channel",
        webhook_action="REPLACE",
        webhook_value="https://open.feishu.cn/open-apis/bot/v2/hook/health-token",
        signing_action="CLEAR",
        signing_value=None,
        required_keyword="Alert Workbench",
        mention_mode="NONE",
        mention_users=[],
        mention_on={},
        box=box,
    )
    revision.last_test_status = "SUCCESS"
    session.add(revision)
    session.flush()
    activate_channel_revision(
        session, revision.id, expected_version=revision.version, box=box
    )
    policy = create_policy_draft(
        session,
        name="health catch all",
        priority=100,
        matchers=[],
        repeat_interval_seconds=3600,
        channel_ids=[channel.id],
    )
    activate_policy(session, policy.id, expected_version=policy.version)
    session.commit()


def _reset_status() -> None:
    notification_status.last_run_at = None
    notification_status.last_error_code = None


def test_notification_health_is_disabled_without_active_configuration(session) -> None:
    _reset_status()
    body = _client(session).get("/api/health").json()

    assert body["notifications"] == {
        "status": "disabled",
        "worker_last_run_at": None,
        "pending": 0,
        "retrying": 0,
        "permanent_failed_24h": 0,
        "oldest_pending_seconds": None,
        "active_channels": 0,
        "active_policies": 0,
        "last_error_code": None,
    }


def test_notification_health_is_healthy_after_recent_worker_run(
    session, monkeypatch
) -> None:
    _activate_notification_config(session, monkeypatch)
    _reset_status()
    notification_status.last_run_at = datetime.now(timezone.utc)

    notifications = _client(session).get("/api/health").json()["notifications"]

    assert notifications["status"] == "healthy"
    assert notifications["active_channels"] == 1
    assert notifications["active_policies"] == 1
    assert notifications["last_error_code"] is None


def test_notification_health_wrong_key_degrades_without_pollution(
    session, monkeypatch
) -> None:
    _activate_notification_config(session, monkeypatch)
    _reset_status()
    notification_status.last_run_at = datetime.now(timezone.utc)
    monkeypatch.setattr(settings, "master_key", "wrong-health-key")
    poll_status.last_poll_at = datetime.now(timezone.utc)
    poll_status.last_poll_ok = True
    poll_status.last_poll_error = None

    poll_at = poll_status.last_poll_at
    client = _client(session)
    body = client.get("/api/health").json()

    assert body["status"] == "ok"
    assert body["last_poll_ok"] is True
    assert body["last_poll_error"] is None
    assert poll_status.last_poll_at == poll_at
    assert client.get("/api/incidents").status_code == 200
    assert body["notifications"]["status"] == "degraded"
    assert body["notifications"]["last_error_code"] == "SECRET_UNAVAILABLE"
    assert "health-token" not in str(body)


def test_notification_health_corrupt_ciphertext_fails_closed(
    session, monkeypatch
) -> None:
    _activate_notification_config(session, monkeypatch)
    _reset_status()
    notification_status.last_run_at = datetime.now(timezone.utc)
    revision = session.exec(
        select(NotificationChannelRevision).where(
            NotificationChannelRevision.state == "ACTIVE"
        )
    ).one()
    # F24: `config_envelope` is what the send path opens. The five legacy
    # columns are still written (they carry KEEP resolution and the API's
    # "configured" flags) but they are no longer the credential of record, so
    # corrupting them alone would not be a real fail-closed test any more.
    revision.config_envelope = {
        **revision.config_envelope,
        "webhook": {"version": 1, "ciphertext": "not-fernet"},
    }
    revision.webhook_envelope = {"version": 1, "ciphertext": "not-fernet"}
    session.add(revision)
    session.commit()

    notifications = _client(session).get("/api/health").json()["notifications"]

    assert notifications["status"] == "degraded"
    assert notifications["last_error_code"] == "SECRET_UNAVAILABLE"


def test_notification_health_stale_worker_degrades(session, monkeypatch) -> None:
    _activate_notification_config(session, monkeypatch)
    _reset_status()
    monkeypatch.setattr(health, "NOTIFICATION_WORKER_STALE_SECONDS", 30)
    notification_status.last_run_at = datetime.now(timezone.utc) - timedelta(minutes=2)

    notifications = _client(session).get("/api/health").json()["notifications"]

    assert notifications["status"] == "degraded"
    assert notifications["last_error_code"] == "WORKER_STALE"


def test_notification_health_old_backlog_is_counted_and_degraded(
    session, monkeypatch
) -> None:
    _activate_notification_config(session, monkeypatch)
    _reset_status()
    now = datetime.now(timezone.utc)
    ingest_alerts(
        session,
        [
            {
                "fingerprint": "health-backlog",
                "labels": {"alertname": "TargetDown", "severity": "critical"},
                "annotations": {"summary": "health backlog"},
            }
        ],
        poll_time=now,
    )
    delivery = session.exec(select(NotificationDelivery)).one()
    delivery.scheduled_at = now - timedelta(minutes=10)
    session.add(delivery)
    session.commit()
    notification_status.last_run_at = now
    monkeypatch.setattr(health, "NOTIFICATION_BACKLOG_DEGRADED_SECONDS", 300)

    notifications = _client(session).get("/api/health").json()["notifications"]

    assert notifications["pending"] == 1
    assert notifications["retrying"] == 0
    assert notifications["oldest_pending_seconds"] >= 600
    assert notifications["status"] == "degraded"
    assert notifications["last_error_code"] == "BACKLOG_OLD"
