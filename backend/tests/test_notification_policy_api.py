"""Policy API preview and two-step activation regressions."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import notification_policies
from app.crypto import SecretBox
from app.db import get_session
from app.providers.feishu import FakeFeishuProvider
from app.services.notification_channels import (
    activate_channel_revision,
    create_channel,
    test_channel_revision,
)


def _client(session) -> TestClient:
    app = FastAPI()
    app.include_router(notification_policies.router, prefix="/api")
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


async def test_policy_preview_create_prepare_confirm_defaults_to_future_only(session) -> None:
    box = SecretBox("policy-api-key")
    channel, revision = create_channel(
        session,
        name="policy api channel",
        webhook_action="REPLACE",
        webhook_value="https://open.feishu.cn/open-apis/bot/v2/hook/policy-api-token",
        signing_action="CLEAR",
        signing_value=None,
        required_keyword=None,
        mention_mode="NONE",
        mention_users=[],
        mention_on={},
        box=box,
    )
    await test_channel_revision(
        session, revision.id, box=box, provider=FakeFeishuProvider()
    )
    activate_channel_revision(session, revision.id, expected_version=1, box=box)
    session.commit()
    payload = {
        "name": "critical prod",
        "priority": 10,
        "matchers": [
            {"field": "severity", "operator": "=", "value": "critical"}
        ],
        "repeat_interval_seconds": 14400,
        "channel_ids": [channel.id],
    }
    client = _client(session)
    preview = client.post("/api/notification-policies/preview", json=payload)
    assert preview.status_code == 200, preview.text
    assert preview.json()["example_card"]["preview_only"] is True

    created = client.post("/api/notification-policies", json=payload)
    assert created.status_code == 201, created.text
    revision_id = created.json()["id"]
    prepared = client.post(
        f"/api/notification-policies/{revision_id}/prepare-activation",
        json={"expected_version": 1, "notify_existing": False},
    )
    assert prepared.status_code == 200, prepared.text
    assert prepared.json()["eligible_incident_count"] == 0
    activated = client.post(
        f"/api/notification-policies/{revision_id}/activate",
        json={
            "expected_version": 1,
            "notify_existing": False,
            "confirm_token": prepared.json()["confirm_token"],
        },
    )
    assert activated.status_code == 200, activated.text
    assert activated.json()["state"] == "ACTIVE"


def test_policy_rejects_repeat_between_zero_and_five_minutes(session) -> None:
    client = _client(session)
    response = client.post(
        "/api/notification-policies/preview",
        json={
            "name": "bad repeat",
            "priority": 10,
            "matchers": [],
            "repeat_interval_seconds": 60,
            "channel_ids": [999],
        },
    )
    assert response.status_code == 422
