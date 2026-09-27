"""Notification candidate API, fake delivery and default-app isolation."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.adapters.notifications.providers import NotificationProviderRegistry, ScriptedFakeNotificationProvider
from app.adapters.persistence.notifications import NotificationRouteRecord
from app.adapters.persistence.sources import IncidentRecord
from app.bootstrap import create_job_platform_app
from app.domains.sources.models import PollCompleteness
from app.platform.persistence.database import create_session_factory


def _resources(tmp_path: Path):
    key = tmp_path / "master.key"
    key.write_text("existing-test-key\n", encoding="utf-8")
    fake = ScriptedFakeNotificationProvider()
    resources = create_job_platform_app(
        database_path=tmp_path / "incident-operations.db",
        master_key_path=key,
        cursor_secret=b"notification-api-cursor-key-32-bytes",
        notification_provider_factory=NotificationProviderRegistry(fake=fake),
        workbench_url="https://workbench.example.invalid",
    )
    return resources, fake


def _resources_with_script(tmp_path: Path, *, channel_test_script: str):
    key = tmp_path / "master.key"
    key.write_text("existing-test-key\n", encoding="utf-8")
    fake = ScriptedFakeNotificationProvider(channel_test_script=channel_test_script)
    return create_job_platform_app(
        database_path=tmp_path / "incident-operations.db",
        master_key_path=key,
        cursor_secret=b"notification-timeout-cursor-32-bytes",
        notification_provider_factory=NotificationProviderRegistry(fake=fake),
    ), fake


def _source_and_incident(resources, client: TestClient) -> int:
    response = client.post(
        "/api/v1/sources",
        headers={"Idempotency-Key": "source-create-notification-0001"},
        json={"name": "Primary", "endpoints": [{"position": 0, "url": "https://am.invalid"}]},
    )
    assert response.status_code == 201
    source_id = response.json()["id"]
    resources.sources.seed_alert_for_characterization(
        source_id=source_id,
        raw={
            "fingerprint": "cpu-high",
            "labels": {"alertname": "CPUHigh", "severity": "warning", "cluster": "prod"},
            "annotations": {"summary": "CPU is above threshold"},
            "startsAt": "2026-08-13T00:00:00Z",
        },
        observed_at=datetime(2026, 8, 13, tzinfo=timezone.utc),
    )
    return resources.sources.list_incidents()[0].id


def test_workbench_url_is_a_persisted_candidate_setting(tmp_path: Path) -> None:
    resources, _fake = _resources(tmp_path)
    with TestClient(resources.app) as client:
        assert client.get("/api/v1/settings/workbench-url").json() == {
            "url": "https://workbench.example.invalid"
        }
        saved = client.put(
            "/api/v1/settings/workbench-url",
            json={"url": "http://127.0.0.1:8000/"},
        )
        assert saved.status_code == 200
        assert saved.json() == {"url": "http://127.0.0.1:8000"}
        assert client.get("/api/v1/settings/workbench-url").json() == saved.json()

    resources.engine.dispose()


def test_notification_timeout_is_ambiguous_and_never_replayed(tmp_path: Path) -> None:
    resources, fake = _resources_with_script(tmp_path, channel_test_script="TIMEOUT")
    with TestClient(resources.app) as client:
        channel = client.post(
            "/api/v1/notification-channels",
            headers={"Idempotency-Key": "notification-timeout-create"},
            json={
                "name": "Timeout Webhook",
                "provider": "GENERIC_WEBHOOK",
                "config": {"url": "https://example.invalid/hook"},
                "secrets": {},
            },
        ).json()
        path = f"/api/v1/notification-channels/{channel['id']}/test"
        headers = {"Idempotency-Key": "notification-timeout-test"}
        first = client.post(path, headers=headers, json={"expected_revision": 1})
        second = client.post(path, headers=headers, json={"expected_revision": 1})
    assert first.status_code == 409
    assert first.json()["error"]["code"] == "COMMAND_OUTCOME_UNKNOWN"
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "COMMAND_OUTCOME_UNKNOWN"
    assert len(fake.calls) == 1
    resources.engine.dispose()


def test_notification_channel_policy_activation_and_delivery_query(tmp_path: Path) -> None:
    resources, fake = _resources(tmp_path)
    with TestClient(resources.app) as client:
        incident_id = _source_and_incident(resources, client)
        sessions = create_session_factory(resources.engine)
        with sessions.begin() as session:
            incident = session.get(IncidentRecord, incident_id)
            assert incident is not None
            resources.incidents._sync_operational_occurrence(
                session,
                incident,
                observed_at=datetime(2026, 8, 13, 0, 1, tzinfo=timezone.utc),
                completeness=PollCompleteness.COMPLETE,
            )
        occurrence = client.get("/api/v1/occurrences").json()["items"][0]
        started = client.post(
            f"/api/v1/occurrences/{occurrence['id']}/start-handling",
            headers={"Idempotency-Key": "notification-response-start"},
            json={"expected_version": occurrence["version"]},
        )
        assert started.status_code == 200
        channel = client.post(
            "/api/v1/notification-channels",
            headers={"Idempotency-Key": "notification-channel-create-0001"},
            json={
                "name": "Primary Feishu",
                "provider": "FEISHU_CUSTOM_BOT",
                "config": {"mention_mode": "NONE"},
                "secrets": {
                    "webhook": {
                        "action": "REPLACE",
                        "value": "https://open.feishu.cn/open-apis/bot/v2/hook/api-test-token",
                    }
                },
            },
        )
        assert channel.status_code == 201
        assert "api-test-token" not in channel.text
        channel_id = channel.json()["id"]
        rejected = client.post(
            f"/api/v1/notification-channels/{channel_id}/activate",
            json={"expected_revision": 1},
        )
        assert rejected.status_code == 409
        tested = client.post(
            f"/api/v1/notification-channels/{channel_id}/test",
            headers={"Idempotency-Key": "notification-channel-test-0001"},
            json={"expected_revision": 1},
        )
        assert tested.status_code == 200 and tested.json()["last_test_code"] == "OK"
        replayed_test = client.post(
            f"/api/v1/notification-channels/{channel_id}/test",
            headers={"Idempotency-Key": "notification-channel-test-0001"},
            json={"expected_revision": 1},
        )
        assert replayed_test.json() == tested.json()
        assert len(fake.calls) == 1
        active = client.post(
            f"/api/v1/notification-channels/{channel_id}/activate",
            json={"expected_revision": 1},
        )
        assert active.status_code == 200 and active.json()["enabled"] is True
        stale_disable = client.post(
            f"/api/v1/notification-channels/{channel_id}/disable",
            json={"expected_revision": 99},
        )
        assert stale_disable.status_code == 409

        policy = client.post(
            "/api/v1/notification-policies",
            headers={"Idempotency-Key": "notification-policy-create-0001"},
            json={
                "name": "Production warnings",
                "priority": 10,
                "matchers": [{"field": "severity", "operator": "=", "value": "warning"}],
                "repeat_interval_seconds": 300,
                "channel_ids": [channel_id],
                "scope_mode": "ALL",
            },
        )
        assert policy.status_code == 201
        revision_id = policy.json()["revision_id"]
        prepared = client.post(
            f"/api/v1/notification-policies/{revision_id}/prepare-activation",
            json={"expected_version": 1, "notify_existing": True},
        )
        assert prepared.status_code == 200
        assert prepared.json()["eligible_incident_count"] == 1
        activated = client.post(
            f"/api/v1/notification-policies/{revision_id}/activate",
            json={
                "expected_version": 1,
                "notify_existing": True,
                "confirm_token": prepared.json()["confirm_token"],
            },
        )
        assert activated.status_code == 200 and activated.json()["state"] == "ACTIVE"
        with sessions() as session:
            route = session.query(NotificationRouteRecord).one()
            assert route.current_response_state == "IN_PROGRESS"
            assert route.reminders_paused is True
        stale_policy_disable = client.post(
            f"/api/v1/notification-policies/{revision_id}/disable",
            json={"expected_version": 99},
        )
        assert stale_policy_disable.status_code == 409
        deliveries = client.get("/api/v1/notification-deliveries")
        assert deliveries.status_code == 200 and len(deliveries.json()) == 1
        detail = client.get(f"/api/v1/notification-deliveries/{deliveries.json()[0]['id']}")
        assert detail.status_code == 200
        assert detail.json()["payload_snapshot"]["incident_id"] > 0
        incident_notification = client.get(
            f"/api/v1/incidents/{incident_id}/notification"
        )
        assert incident_notification.status_code == 200
        assert incident_notification.json()["route"]["policy_name"] == "Production warnings"
        assert incident_notification.json()["targets"][0]["channel_id"] == channel_id
        resolved = client.post(
            f"/api/v1/occurrences/{occurrence['id']}/resolve",
            headers={"Idempotency-Key": "notification-response-resolve"},
            json={
                "expected_version": started.json()["version"],
                "resolution_code": "NO_ACTION",
                "reason": "platform response closed while the upstream signal remains firing",
            },
        )
        assert resolved.status_code == 200
        terminated = client.get(f"/api/v1/incidents/{incident_id}/notification")
        assert terminated.json()["route"]["status"] == "TERMINATED"
        assert terminated.json()["route"]["termination_reason"] == "RESPONSE_RESOLVED"
        assert terminated.json()["route"]["next_reminder_at"] is None
        assert len(fake.calls) >= 1  # explicit channel test; runner delivery is independently scheduled

    database = (tmp_path / "incident-operations.db").read_bytes()
    assert b"api-test-token" not in database


def test_notification_candidate_does_not_replace_default_app(tmp_path: Path) -> None:
    from app.main import app as current_app

    resources, _fake = _resources(tmp_path)
    candidate_paths = resources.app.openapi()["paths"]
    assert "/api/v1/notification-channels" in candidate_paths
    assert "/api/v1/notification-policies" in candidate_paths
    assert "/api/v1/notification-deliveries" in candidate_paths
    assert "/api/notification-channels" in current_app.openapi()["paths"]
    assert "/api/v1/notification-channels" not in current_app.openapi()["paths"]
    resources.engine.dispose()


def test_delivery_query_forwards_incident_and_cursor(tmp_path: Path) -> None:
    """The HTTP seam must not silently discard filters the UI sends."""
    from unittest.mock import patch
    from app.adapters.persistence.notifications import SqlAlchemyNotificationStore
    resources, _fake = _resources(tmp_path)
    with TestClient(resources.app) as client:
        with patch.object(SqlAlchemyNotificationStore, "list_deliveries", return_value=()) as listing:
            response = client.get('/api/v1/notification-deliveries?incident_id=42&before_id=200&channel_id=archive&limit=25')
            assert response.status_code == 200
            listing.assert_called_once_with(state=None, event_type=None, channel_id='archive', incident_id=42, before_id=200, limit=25)
        assert client.get('/api/v1/notification-deliveries?before_id=0').status_code == 422
        assert client.get('/api/v1/notification-deliveries?incident_id=-1').status_code == 422
    resources.engine.dispose()
