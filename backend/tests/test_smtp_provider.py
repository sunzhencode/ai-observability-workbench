"""SMTP provider: classification, TLS, the egress guard, and rendering.

No test opens a socket. `SmtpConfig.sender` is the injection point, and the
guard's resolver is patched, so the whole file runs offline.
"""

from __future__ import annotations

import smtplib

import pytest

from app.providers import egress
from app.providers.smtp import SmtpConfig, SmtpProvider, _classify
from app.services.notification_renderer import MessageContext, render_smtp_payload


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    monkeypatch.setattr(egress, "default_resolver", lambda host: ["93.184.216.34"])


class RecordingSender:
    """Stands in for smtplib: records what would have gone out, sends nothing."""

    def __init__(self) -> None:
        self.sent: list = []

    def __call__(self, message, config):
        from app.providers.feishu import ProviderResult

        self.sent.append((message, config))
        return ProviderResult(True, "OK")


def _config(**overrides) -> tuple[SmtpConfig, RecordingSender]:
    sender = RecordingSender()
    values = dict(
        host="smtp.example.com",
        port=587,
        tls_mode="STARTTLS",
        from_addr="bot@example.com",
        to_addrs=("ops@example.com",),
        sender=sender,
    )
    values.update(overrides)
    return SmtpConfig(**values), sender


PAYLOAD = {"subject": "TargetDown", "text": "body", "html": "<p>body</p>"}


class TestEgress:
    async def test_a_host_resolving_private_is_refused_before_connecting(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(egress, "default_resolver", lambda host: ["127.0.0.1"])
        config, _ = _config()
        result = await SmtpProvider().send(PAYLOAD, config)
        assert result.ok is False
        assert result.code == "EGRESS_PRIVATE_ADDRESS"

    async def test_the_guard_runs_on_every_send_not_only_at_save(
        self, monkeypatch
    ) -> None:
        provider = SmtpProvider()
        config, _ = _config()
        assert (await provider.send(PAYLOAD, config)).ok is True
        # The record changes underneath a channel that was fine yesterday.
        monkeypatch.setattr(egress, "default_resolver", lambda host: ["10.0.0.9"])
        assert (await provider.send(PAYLOAD, config)).code == "EGRESS_PRIVATE_ADDRESS"


class TestMessage:
    async def test_it_addresses_every_recipient(self) -> None:
        config, sender = _config(to_addrs=("a@example.com", "b@example.com"))
        await SmtpProvider().send(PAYLOAD, config)
        message, _ = sender.sent[0]
        assert message["To"] == "a@example.com, b@example.com"
        assert message["From"] == "bot@example.com"

    async def test_the_subject_prefix_is_applied(self) -> None:
        config, sender = _config(subject_prefix="[prod]")
        await SmtpProvider().send(PAYLOAD, config)
        message, _ = sender.sent[0]
        assert message["Subject"] == "[prod] TargetDown"

    async def test_an_oversized_body_fails_before_connecting(self) -> None:
        config, sender = _config()
        huge = {"subject": "x", "text": "y" * (300 * 1024)}
        result = await SmtpProvider().send(huge, config)
        assert result.code == "PAYLOAD_TOO_LARGE"
        assert sender.sent == []


class TestClassification:
    def test_a_4xx_is_worth_retrying_and_a_5xx_is_not(self) -> None:
        transient = _classify(smtplib.SMTPResponseException(451, b"try later"))
        permanent = _classify(smtplib.SMTPResponseException(550, b"no such user"))
        assert transient.transient is True
        assert permanent.transient is False

    def test_authentication_failure_is_configuration_not_weather(self) -> None:
        result = _classify(smtplib.SMTPAuthenticationError(535, b"bad password"))
        assert result.code == "SMTP_AUTH"
        assert result.transient is False

    def test_a_dropped_connection_is_retried(self) -> None:
        assert _classify(smtplib.SMTPServerDisconnected()).transient is True
        assert _classify(TimeoutError()).transient is True

    def test_the_password_never_reaches_the_result(self) -> None:
        result = _classify(smtplib.SMTPAuthenticationError(535, b"bad password"))
        assert "hunter2" not in str(result)


class TestRendering:
    def _context(self, **overrides) -> MessageContext:
        values = dict(
            event_type="FIRING_OPENED",
            severity="CRITICAL",
            title="TargetDown",
            source="prod",
            source_state="firing",
            occurrence_no=2,
            member_count=7,
            members=[{"alertname": f"A{i}", "severity": "warning"} for i in range(7)],
            group_labels={"cluster": "a"},
            incident_id=42,
            workbench_url="https://wb.example",
        )
        values.update(overrides)
        return MessageContext(**values)

    def test_it_carries_the_same_facts_as_a_card(self) -> None:
        payload = render_smtp_payload(self._context())
        assert "CRITICAL" in payload["subject"]
        assert "TargetDown" in payload["subject"]
        assert "prod" in payload["text"]
        assert "cluster=a" in payload["text"]
        assert "https://wb.example/incidents/42" in payload["text"]

    def test_it_summarises_rather_than_listing_every_member(self) -> None:
        payload = render_smtp_payload(self._context())
        assert "另有 2 条成员告警" in payload["text"]

    def test_html_is_escaped(self) -> None:
        payload = render_smtp_payload(self._context(title="<script>x</script>"))
        assert "<script>" not in payload["html"]
        assert "&lt;script&gt;" in payload["html"]

    def test_it_works_without_a_workbench_url(self) -> None:
        payload = render_smtp_payload(self._context(workbench_url=""))
        assert "打开工作台" not in payload["text"]
