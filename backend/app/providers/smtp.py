"""Bounded SMTP provider.

The shape furthest from Feishu, which is why it was chosen as the second kind:
no webhook, no "@ whom", credentials that are a username and a password rather
than a URL, and a message that is a document rather than a card. If the
abstraction survives this it is probably real.

Sending is synchronous (`smtplib`) run on a thread, because there is no stdlib
async SMTP and adding a dependency for one provider is not worth it. The worker
awaits it like any other provider.
"""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any

from app.providers.egress import EgressRejected, assert_public_smtp
from app.providers.feishu import ProviderResult

CONNECT_TIMEOUT_SECONDS = 15.0
#: A mail body is not a card, but it is still not a place for an unbounded dump.
MAX_BODY_BYTES = 256 * 1024


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    tls_mode: str
    from_addr: str
    to_addrs: tuple[str, ...]
    username: str = ""
    password: str = ""
    subject_prefix: str = ""
    #: Injected by tests; production leaves it None and uses smtplib.
    sender: Any = field(default=None, compare=False)


def _build_message(payload: dict[str, Any], config: SmtpConfig) -> EmailMessage:
    message = EmailMessage()
    subject = str(payload.get("subject") or "").strip() or "Alert Workbench"
    prefix = config.subject_prefix.strip()
    message["Subject"] = f"{prefix} {subject}".strip() if prefix else subject
    message["From"] = config.from_addr
    message["To"] = ", ".join(config.to_addrs)
    text = str(payload.get("text") or "")
    html = payload.get("html")
    message.set_content(text)
    if html:
        message.add_alternative(str(html), subtype="html")
    return message


def _classify(exc: Exception) -> ProviderResult:
    """Which SMTP failures are worth another attempt.

    A 4xx is the server saying "not now" -- greylisting, a full mailbox, a rate
    limit -- so the Outbox should come back. A 5xx is a refusal that will be
    refused again, and authentication failures are configuration, not weather.
    """

    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return ProviderResult(False, "SMTP_AUTH", error_summary="authentication rejected")
    if isinstance(exc, smtplib.SMTPResponseException):
        code = int(exc.smtp_code or 0)
        transient = 400 <= code < 500
        return ProviderResult(
            False,
            f"SMTP_{code}",
            transient=transient,
            http_status=code,
            error_summary="server rejected the message",
        )
    if isinstance(exc, (smtplib.SMTPServerDisconnected, TimeoutError, OSError)):
        return ProviderResult(False, "SMTP_NETWORK", transient=True)
    return ProviderResult(False, "SMTP_ERROR", error_summary="send failed")


def _send_sync(message: EmailMessage, config: SmtpConfig) -> ProviderResult:
    context = ssl.create_default_context()
    try:
        if config.tls_mode == "TLS":
            client = smtplib.SMTP_SSL(
                config.host,
                config.port,
                timeout=CONNECT_TIMEOUT_SECONDS,
                context=context,
            )
        else:
            client = smtplib.SMTP(
                config.host, config.port, timeout=CONNECT_TIMEOUT_SECONDS
            )
        with client:
            if config.tls_mode == "STARTTLS":
                client.starttls(context=context)
                # A server that ignores STARTTLS would otherwise get the password
                # in the clear.
                if not client.does_esmtp:
                    return ProviderResult(False, "SMTP_NO_TLS")
            if config.username:
                client.login(config.username, config.password)
            client.send_message(message)
    except Exception as exc:  # noqa: BLE001 - classified, never re-raised
        return _classify(exc)
    return ProviderResult(True, "OK")


class SmtpProvider:
    kind = "SMTP"

    async def send(
        self,
        payload: dict[str, Any],
        config: SmtpConfig,
        *,
        purpose: str | None = None,
    ) -> ProviderResult:
        del purpose
        try:
            # Re-checked on every send: a hostname that was public when the
            # channel was saved can point somewhere else by the time it is used.
            assert_public_smtp(config.host, config.port)
        except EgressRejected as exc:
            return ProviderResult(False, exc.code, error_summary="target refused")

        message = _build_message(payload, config)
        if len(bytes(message)) > MAX_BODY_BYTES:
            return ProviderResult(False, "PAYLOAD_TOO_LARGE")

        sender = config.sender
        if sender is not None:
            return sender(message, config)
        return await asyncio.to_thread(_send_sync, message, config)
