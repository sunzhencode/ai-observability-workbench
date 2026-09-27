"""Notification egress, provider and deterministic fake contracts."""

from __future__ import annotations

from email.message import EmailMessage
import json

import httpx

from app.adapters.notifications.providers import (
    MAX_RESPONSE_BYTES,
    GenericWebhookNotificationProvider,
    FeishuNotificationProvider,
    NotificationProviderRegistry,
    ScriptedFakeNotificationProvider,
    SmtpNotificationProvider,
    assert_public_https,
)
from app.application.notifications import ProviderResult


def test_notification_egress_rejects_mixed_public_private_dns() -> None:
    try:
        assert_public_https(
            "https://hooks.example.invalid/notify",
            resolver=lambda _host: ("8.8.8.8", "10.0.0.8"),
        )
    except ValueError as exc:
        assert str(exc) == "NOTIFICATION_EGRESS_PRIVATE_ADDRESS"
    else:
        raise AssertionError("mixed DNS answer crossed notification egress")


def test_notification_https_guard_rejects_smtp_ports() -> None:
    try:
        assert_public_https(
            "https://hooks.example.invalid:587/notify",
            resolver=lambda _host: ("8.8.8.8",),
        )
    except ValueError as exc:
        assert str(exc) == "NOTIFICATION_EGRESS_PORT"
    else:
        raise AssertionError("SMTP port crossed the HTTPS notification guard")


class _CountingStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.chunks_seen = 0

    async def __aiter__(self):
        for chunk in (
            b"a" * (MAX_RESPONSE_BYTES // 2),
            b"b" * (MAX_RESPONSE_BYTES // 2 + 1),
            b"must-not-be-read",
        ):
            self.chunks_seen += 1
            yield chunk


async def test_generic_webhook_stops_reading_an_oversized_stream() -> None:
    stream = _CountingStream()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    provider = GenericWebhookNotificationProvider(
        transport=httpx.MockTransport(handler),
        resolver=lambda _host: ("8.8.8.8",),
    )
    result = await provider.send(
        {"text": "test"},
        {"url": "https://hooks.example.invalid/notify"},
        {},
        purpose="DELIVERY",
    )

    assert result.code == "RESPONSE_TOO_LARGE"
    assert stream.chunks_seen == 2


async def test_generic_webhook_rechecks_dns_and_never_follows_redirects() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})

    answers = [("8.8.8.8",), ("10.0.0.8",)]
    provider = GenericWebhookNotificationProvider(
        transport=httpx.MockTransport(handler), resolver=lambda _host: answers.pop(0)
    )
    config = {"url": "https://hooks.example.invalid/notify", "timeout_seconds": 2}
    first = await provider.send({"text": "test"}, config, {}, purpose="DELIVERY")
    second = await provider.send({"text": "test"}, config, {}, purpose="DELIVERY")
    assert first.code == "REDIRECT_REJECTED"
    assert second.code == "NOTIFICATION_EGRESS_PRIVATE_ADDRESS"
    assert len(requests) == 1


async def test_feishu_uses_official_v2_shape_and_rechecks_resolved_address() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"code": 0})

    answers = [("8.8.8.8",), ("10.0.0.8",)]
    provider = FeishuNotificationProvider(
        transport=httpx.MockTransport(handler), resolver=lambda _host: answers.pop(0)
    )
    secrets = {
        "webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/test-token"
    }
    first = await provider.send(
        {"subject": "Incident", "text": "fact"},
        {"mention_mode": "NONE"},
        secrets,
        purpose="DELIVERY",
    )
    second = await provider.send(
        {"subject": "Incident", "text": "fact"},
        {"mention_mode": "NONE"},
        secrets,
        purpose="DELIVERY",
    )
    assert first.ok is True
    assert second.code == "NOTIFICATION_EGRESS_PRIVATE_ADDRESS"
    assert [(item.method, item.url.path) for item in requests] == [
        ("POST", "/open-apis/bot/v2/hook/test-token")
    ]


async def test_feishu_mentions_only_for_explicitly_configured_event() -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"code": 0})

    provider = FeishuNotificationProvider(
        transport=httpx.MockTransport(handler), resolver=lambda _host: ("8.8.8.8",)
    )
    secrets = {"webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/test-token"}
    config = {
        "mention_mode": "ALL",
        "mention_on": {"SEVERITY_ESCALATED": True},
    }
    await provider.send(
        {"event_type": "FIRING_OPENED", "text": "opened"},
        config,
        secrets,
        purpose="DELIVERY",
    )
    await provider.send(
        {"event_type": "SEVERITY_ESCALATED", "text": "escalated"},
        config,
        secrets,
        purpose="DELIVERY",
    )
    first = bodies[0]["card"]["elements"][0]["content"]  # type: ignore[index]
    second = bodies[1]["card"]["elements"][0]["content"]  # type: ignore[index]
    assert "<at id=all>" not in first
    assert "<at id=all>" in second


async def test_smtp_guard_runs_before_injected_sender() -> None:
    sent: list[EmailMessage] = []

    def sender(message, _config, _secrets):
        sent.append(message)
        return ProviderResult(True, "OK")

    answers = [("8.8.8.8",), ("127.0.0.1",)]
    provider = SmtpNotificationProvider(
        resolver=lambda _host: answers.pop(0), sender=sender
    )
    config = {
        "host": "smtp.example.invalid",
        "port": 587,
        "tls_mode": "STARTTLS",
        "from_addr": "alerts@example.invalid",
        "to_addrs": ["ops@example.invalid"],
    }
    assert (await provider.send({"subject": "Incident", "text": "fact"}, config, {}, purpose="DELIVERY")).ok
    assert (await provider.send({"subject": "Incident", "text": "fact"}, config, {}, purpose="DELIVERY")).code == "NOTIFICATION_EGRESS_PRIVATE_ADDRESS"
    assert len(sent) == 1


async def test_fake_fault_script_is_shared_by_all_kinds_without_network() -> None:
    fake = ScriptedFakeNotificationProvider(delivery_script="HTTP_500,OK")
    registry = NotificationProviderRegistry(fake=fake)
    first = await registry("SMTP").send({}, {}, {}, purpose="DELIVERY")
    second = await registry("GENERIC_WEBHOOK").send({}, {}, {}, purpose="DELIVERY")
    assert first.transient is True and first.code == "HTTP_500"
    assert second.ok is True
    assert len(fake.calls) == 2
