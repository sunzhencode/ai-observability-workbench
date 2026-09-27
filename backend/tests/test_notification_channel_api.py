"""HTTP surface for creating channels of each kind.

Before F24 there was no test at this level at all -- the service was covered but
the endpoint was not, which is how the request shape could have changed without
anything noticing.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fastapi.exceptions import RequestValidationError

from app.api import notification_channels
from app.config import settings
from app.config_schemas import safe_validation_exception_handler
from app.db import get_session
from app.providers import egress

FEISHU_WEBHOOK = (
    "https://open.feishu.cn/open-apis/bot/v2/hook/"
    "00000000-0000-0000-0000-000000000001"
)


@pytest.fixture(autouse=True)
def _local_key(monkeypatch):
    monkeypatch.setattr(settings, "master_key", "channel-api-master-key")
    monkeypatch.setattr(egress, "default_resolver", lambda host: ["93.184.216.34"])


def _client(session) -> TestClient:
    app = FastAPI()
    # The same validation handler main.py installs. Without it a 422 body echoes
    # the rejected input, which is how a private address would come back out.
    app.add_exception_handler(
        RequestValidationError, safe_validation_exception_handler
    )
    app.include_router(notification_channels.router, prefix="/api")
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def _create(session, name: str, config: dict):
    return _client(session).post(
        "/api/notification-channels", json={"name": name, "config": config}
    )


FEISHU = {
    "kind": "FEISHU_CUSTOM_BOT",
    "webhook": {"action": "REPLACE", "value": FEISHU_WEBHOOK},
    "signing_secret": {"action": "CLEAR"},
}
SMTP = {
    "kind": "SMTP",
    "host": "smtp.example.com",
    "port": 587,
    "tls_mode": "STARTTLS",
    "username": "bot",
    "password": {"action": "REPLACE", "value": "hunter2"},
    "from_addr": "bot@example.com",
    "to_addrs": ["ops@example.com"],
}
WEBHOOK = {
    "kind": "GENERIC_WEBHOOK",
    "url": "https://hooks.example.com/services/abc",
    "headers": {"action": "REPLACE", "value": '{"Authorization": "Bearer t"}'},
    "signing_secret": {"action": "CLEAR"},
}


class TestCreate:
    def test_each_kind_can_be_created(self, session) -> None:
        for index, config in enumerate([FEISHU, SMTP, WEBHOOK]):
            response = _create(session, f"channel-{index}", config)
            assert response.status_code == 201, response.text
            body = response.json()
            revision = body["revisions"][0]
            assert revision["provider"] == config["kind"]
            assert revision["state"] == "DRAFT"

    def test_the_kind_decides_which_fields_are_accepted(self, session) -> None:
        # An SMTP channel carrying a webhook is not a slightly wrong SMTP
        # channel; it is a different kind, and the discriminator says so.
        bad = {**SMTP, "webhook": {"action": "CLEAR"}}
        assert _create(session, "mixed", bad).status_code == 422

    def test_an_unknown_kind_is_refused(self, session) -> None:
        assert _create(session, "slack", {"kind": "SLACK", "url": "x"}).status_code == 422

    def test_a_private_webhook_target_is_refused_at_save(self, session) -> None:
        response = _create(
            session, "internal", {**WEBHOOK, "url": "http://10.0.0.5/hook"}
        )
        assert response.status_code in (400, 422)
        assert "10.0.0.5" not in response.text


class TestSummary:
    def test_the_summary_identifies_a_channel_without_exposing_it(
        self, session
    ) -> None:
        _create(session, "mail", SMTP)
        _create(session, "hook", WEBHOOK)
        body = _client(session).get("/api/notification-channels").json()
        summaries = {
            item["name"]: item["revisions"][0]["config_summary"] for item in body
        }
        assert summaries["mail"]["target"] == "smtp.example.com:587"
        assert summaries["mail"]["detail"] == "1 个收件人"
        assert summaries["mail"]["secret_configured"] is True
        # Host only: enough to recognise, not enough to reuse.
        assert summaries["hook"]["target"] == "hooks.example.com"
        assert "/services/abc" not in str(summaries["hook"])

    def test_no_secret_reaches_the_response(self, session) -> None:
        _create(session, "mail", SMTP)
        _create(session, "hook", WEBHOOK)
        text = _client(session).get("/api/notification-channels").text
        assert "hunter2" not in text
        assert "Bearer t" not in text
        assert FEISHU_WEBHOOK not in text


class TestUpdate:
    def test_keep_preserves_a_secret_the_user_cannot_read_back(self, session) -> None:
        created = _create(session, "mail", SMTP).json()
        channel_id = created["id"]
        version = created["revisions"][0]["version"]

        response = _client(session).put(
            f"/api/notification-channels/{channel_id}/draft",
            json={
                "expected_version": version,
                "config": {
                    **SMTP,
                    "password": {"action": "KEEP"},
                    "subject_prefix": "[prod]",
                },
            },
        )
        assert response.status_code == 200, response.text
        summary = response.json()["revisions"][-1]["config_summary"]
        assert summary["target"] == "smtp.example.com:587"

    def test_the_kind_cannot_be_changed(self, session) -> None:
        created = _create(session, "mail", SMTP).json()
        response = _client(session).put(
            f"/api/notification-channels/{created['id']}/draft",
            json={
                "expected_version": created["revisions"][0]["version"],
                "config": FEISHU,
            },
        )
        # A different kind is a different destination, like a different group.
        assert response.status_code == 422
        assert "cannot be changed" in response.text
