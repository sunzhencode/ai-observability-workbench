"""Three bounded outbound providers with an injectable network-free fake."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable, Mapping
from email.message import EmailMessage
import hashlib
import hmac
import json
import re
import smtplib
import ssl
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.application.notifications import NotificationProvider, ProviderResult
from app.domains.notifications.models import ProviderKind
from app.platform.egress import (
    EgressRejected as CanonicalEgressRejected,
    Resolver,
    assert_public_https as canonical_assert_public_https,
    assert_public_smtp as canonical_assert_public_smtp,
)


MAX_FEISHU_BYTES = 18 * 1024
MAX_WEBHOOK_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
MAX_SMTP_BYTES = 256 * 1024
OPEN_ID = re.compile(r"\A[A-Za-z0-9_-]{1,128}\Z")
FEISHU_HOSTS = frozenset({"open.feishu.cn", "open.larksuite.com"})
FEISHU_PATH = "/open-apis/bot/v2/hook/"


class EgressRejected(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _raise_notification_egress(exc: CanonicalEgressRejected) -> None:
    """Keep the notification API's stable safe codes over the shared guard."""

    raise EgressRejected(f"NOTIFICATION_{exc.code}") from exc


def assert_public_https(url: str, *, resolver: Resolver | None = None) -> None:
    try:
        canonical_assert_public_https(url, resolver=resolver)
    except CanonicalEgressRejected as exc:
        _raise_notification_egress(exc)


def assert_public_smtp_target(
    host: str, port: int, *, resolver: Resolver | None = None
) -> None:
    try:
        canonical_assert_public_smtp(host, port, resolver=resolver)
    except CanonicalEgressRejected as exc:
        _raise_notification_egress(exc)


class ResponseTooLarge(RuntimeError):
    pass


async def _read_bounded_response(response: httpx.Response) -> bytes:
    content_length = response.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > MAX_RESPONSE_BYTES:
                raise ResponseTooLarge
        except ValueError:
            pass
    content = bytearray()
    async for chunk in response.aiter_bytes():
        if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
            raise ResponseTooLarge
        content.extend(chunk)
    return bytes(content)


def _feishu_webhook(value: str) -> str:
    parts = urlsplit(value)
    token = parts.path.removeprefix(FEISHU_PATH)
    if (
        parts.scheme != "https"
        or parts.hostname not in FEISHU_HOSTS
        or (parts.port or 443) != 443
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or not parts.path.startswith(FEISHU_PATH)
        or not token
        or "/" in token
        or len(token) > 256
        or not all(char.isalnum() or char in "_-" for char in token)
    ):
        raise EgressRejected("NOTIFICATION_FEISHU_WEBHOOK_INVALID")
    return value


def _feishu_payload(payload: Mapping[str, object], config: Mapping[str, object]) -> dict[str, object]:
    text = str(payload.get("text") or "")
    keyword = str(config.get("required_keyword") or "").strip()
    if keyword and keyword not in text:
        text = f"{keyword}\n{text}"
    event_type = str(payload.get("event_type") or "")
    mention_on = config.get("mention_on") or {}
    enabled = isinstance(mention_on, Mapping) and mention_on.get(event_type) is True
    mode = str(config.get("mention_mode") or "NONE") if enabled else "NONE"
    if mode == "ALL":
        text += "\n<at id=all></at>"
    elif mode == "USERS":
        users = config.get("mention_users") or []
        if isinstance(users, list):
            mentions = [
                f"<at id={item['open_id']}></at>"
                for item in users
                if isinstance(item, Mapping) and OPEN_ID.fullmatch(str(item.get("open_id") or ""))
            ]
            if mentions:
                text += "\n" + " ".join(mentions)
    return {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": str(payload.get("subject") or "事件通知")[:160]}},
            "elements": [{"tag": "markdown", "content": text}],
        },
    }


class FeishuNotificationProvider:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None, resolver: Resolver | None = None) -> None:
        self._transport = transport
        self._resolver = resolver

    async def send(self, payload: Mapping[str, object], config: Mapping[str, object], secrets: Mapping[str, str], *, purpose: str) -> ProviderResult:
        del purpose
        try:
            webhook = _feishu_webhook(secrets.get("webhook", ""))
            assert_public_https(webhook, resolver=self._resolver)
        except EgressRejected as exc:
            return ProviderResult(False, exc.code)
        body = _feishu_payload(payload, config)
        signing_secret = secrets.get("signing_secret", "")
        if signing_secret:
            timestamp = int(time.time())
            key = f"{timestamp}\n{signing_secret}".encode("utf-8")
            body["timestamp"] = str(timestamp)
            body["sign"] = base64.b64encode(hmac.new(key, digestmod=hashlib.sha256).digest()).decode("ascii")
        encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_FEISHU_BYTES:
            return ProviderResult(False, "PAYLOAD_TOO_LARGE")
        try:
            async with httpx.AsyncClient(timeout=8.0, transport=self._transport, follow_redirects=False) as client:
                async with client.stream(
                    "POST",
                    webhook,
                    content=encoded,
                    headers={"Content-Type": "application/json"},
                ) as response:
                    request_id = response.headers.get("x-request-id")
                    if 300 <= response.status_code < 400:
                        return ProviderResult(False, "REDIRECT_REJECTED", http_status=response.status_code, request_id=request_id)
                    if response.status_code == 429 or response.status_code >= 500:
                        return ProviderResult(False, f"HTTP_{response.status_code}", transient=True, http_status=response.status_code, request_id=request_id)
                    if response.status_code >= 400:
                        return ProviderResult(False, f"HTTP_{response.status_code}", http_status=response.status_code, request_id=request_id)
                    content = await _read_bounded_response(response)
        except httpx.TimeoutException:
            return ProviderResult(False, "TIMEOUT", transient=True)
        except httpx.HTTPError:
            return ProviderResult(False, "NETWORK", transient=True)
        except ResponseTooLarge:
            return ProviderResult(False, "RESPONSE_TOO_LARGE", http_status=response.status_code)
        try:
            result = json.loads(content)
        except (TypeError, ValueError, json.JSONDecodeError):
            return ProviderResult(False, "INVALID_RESPONSE", http_status=response.status_code)
        code = result.get("code", result.get("StatusCode")) if isinstance(result, dict) else None
        if code in (0, "0"):
            return ProviderResult(True, "OK", http_status=response.status_code, request_id=request_id)
        safe = str(code) if code is not None else "UNKNOWN"
        return ProviderResult(False, f"FEISHU_{safe}", transient=safe == "11232", http_status=response.status_code, request_id=request_id)


class GenericWebhookNotificationProvider:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None, resolver: Resolver | None = None) -> None:
        self._transport = transport
        self._resolver = resolver

    async def send(self, payload: Mapping[str, object], config: Mapping[str, object], secrets: Mapping[str, str], *, purpose: str) -> ProviderResult:
        del purpose
        url = str(config.get("url") or "")
        try:
            assert_public_https(url, resolver=self._resolver)
        except EgressRejected as exc:
            return ProviderResult(False, exc.code)
        body = json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(body) > MAX_WEBHOOK_BYTES:
            return ProviderResult(False, "PAYLOAD_TOO_LARGE")
        headers: dict[str, str] = {"Content-Type": "application/json"}
        raw_headers = secrets.get("headers", "")
        if raw_headers:
            try:
                parsed = json.loads(raw_headers)
                if not isinstance(parsed, dict):
                    raise ValueError
                headers.update({str(key): str(value) for key, value in parsed.items()})
            except (TypeError, ValueError, json.JSONDecodeError):
                return ProviderResult(False, "WEBHOOK_HEADERS_INVALID")
        signing = secrets.get("signing_secret", "")
        if signing:
            timestamp = int(time.time())
            headers["X-Workbench-Timestamp"] = str(timestamp)
            headers["X-Workbench-Signature"] = hmac.new(
                signing.encode("utf-8"), f"{timestamp}.".encode("utf-8") + body, hashlib.sha256
            ).hexdigest()
        try:
            async with httpx.AsyncClient(timeout=float(str(config.get("timeout_seconds") or 8.0)), transport=self._transport, follow_redirects=False) as client:
                async with client.stream("POST", url, content=body, headers=headers) as response:
                    if 300 <= response.status_code < 400:
                        return ProviderResult(False, "REDIRECT_REJECTED", http_status=response.status_code)
                    if response.status_code == 429 or response.status_code >= 500:
                        return ProviderResult(False, f"HTTP_{response.status_code}", transient=True, http_status=response.status_code)
                    if response.status_code >= 400:
                        return ProviderResult(False, f"HTTP_{response.status_code}", http_status=response.status_code)
                    await _read_bounded_response(response)
        except httpx.TimeoutException:
            return ProviderResult(False, "TIMEOUT", transient=True)
        except httpx.HTTPError:
            return ProviderResult(False, "NETWORK", transient=True)
        except ResponseTooLarge:
            return ProviderResult(False, "RESPONSE_TOO_LARGE", http_status=response.status_code)
        return ProviderResult(True, "OK", http_status=response.status_code)


def _smtp_result(exc: Exception) -> ProviderResult:
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return ProviderResult(False, "SMTP_AUTH")
    if isinstance(exc, smtplib.SMTPResponseException):
        code = int(exc.smtp_code or 0)
        return ProviderResult(False, f"SMTP_{code}", transient=400 <= code < 500, http_status=code)
    if isinstance(exc, (smtplib.SMTPServerDisconnected, TimeoutError, OSError)):
        return ProviderResult(False, "SMTP_NETWORK", transient=True)
    return ProviderResult(False, "SMTP_ERROR")


class SmtpNotificationProvider:
    def __init__(self, *, resolver: Resolver | None = None, sender: Callable[[EmailMessage, Mapping[str, object], Mapping[str, str]], ProviderResult] | None = None) -> None:
        self._resolver = resolver
        self._sender = sender

    async def send(self, payload: Mapping[str, object], config: Mapping[str, object], secrets: Mapping[str, str], *, purpose: str) -> ProviderResult:
        del purpose
        host = str(config.get("host") or "")
        port = int(str(config.get("port") or 0))
        try:
            assert_public_smtp_target(host, port, resolver=self._resolver)
        except EgressRejected as exc:
            return ProviderResult(False, exc.code)
        message = EmailMessage()
        prefix = str(config.get("subject_prefix") or "").strip()
        subject = str(payload.get("subject") or "事件通知")
        message["Subject"] = f"{prefix} {subject}".strip()
        message["From"] = str(config.get("from_addr") or "")
        raw_recipients = config.get("to_addrs")
        recipients = tuple(
            str(item)
            for item in (raw_recipients if isinstance(raw_recipients, (list, tuple)) else ())
        )
        message["To"] = ", ".join(recipients)
        message.set_content(str(payload.get("text") or ""))
        if len(bytes(message)) > MAX_SMTP_BYTES:
            return ProviderResult(False, "PAYLOAD_TOO_LARGE")
        if self._sender is not None:
            return self._sender(message, config, secrets)

        def send_sync() -> ProviderResult:
            context = ssl.create_default_context()
            try:
                if str(config.get("tls_mode")) == "TLS":
                    client: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=15.0, context=context)
                else:
                    client = smtplib.SMTP(host, port, timeout=15.0)
                with client:
                    if str(config.get("tls_mode")) == "STARTTLS":
                        client.starttls(context=context)
                    username = str(config.get("username") or "")
                    if username:
                        client.login(username, secrets.get("password", ""))
                    client.send_message(message)
            except Exception as exc:  # noqa: BLE001 - converted to stable outcome
                return _smtp_result(exc)
            return ProviderResult(True, "OK")

        return await asyncio.to_thread(send_sync)


def _fake_result(token: str) -> ProviderResult:
    code = token.strip().upper()
    if code == "OK":
        return ProviderResult(True, "OK", http_status=200)
    if code in {"TIMEOUT", "NETWORK"}:
        return ProviderResult(False, code, transient=True)
    if code.startswith("HTTP_"):
        status = int(code.removeprefix("HTTP_"))
        return ProviderResult(False, code, transient=status == 429 or status >= 500, http_status=status)
    if code.startswith("SMTP_"):
        status = int(code.removeprefix("SMTP_"))
        return ProviderResult(False, code, transient=400 <= status < 500, http_status=status)
    if code.startswith("FEISHU_"):
        return ProviderResult(False, code, transient=code == "FEISHU_11232", http_status=200)
    raise ValueError("NOTIFICATION_FAKE_RESULT_INVALID")


class ScriptedFakeNotificationProvider:
    """Purpose-separated deterministic fake; it never opens a socket."""

    def __init__(self, *, channel_test_script: str = "OK", delivery_script: str = "OK") -> None:
        self._queues = {
            "CHANNEL_TEST": [_fake_result(item) for item in channel_test_script.split(",") if item.strip()] or [_fake_result("OK")],
            "DELIVERY": [_fake_result(item) for item in delivery_script.split(",") if item.strip()] or [_fake_result("OK")],
        }
        self.calls: list[tuple[str, Mapping[str, object], Mapping[str, object]]] = []

    async def send(self, payload: Mapping[str, object], config: Mapping[str, object], secrets: Mapping[str, str], *, purpose: str) -> ProviderResult:
        self.calls.append((purpose, payload, config))
        queue = self._queues.get(purpose, self._queues["DELIVERY"])
        if len(queue) > 1:
            return queue.pop(0)
        return queue[0]


class NotificationProviderRegistry:
    def __init__(
        self,
        *,
        fake: ScriptedFakeNotificationProvider | None = None,
        providers: Mapping[str, NotificationProvider] | None = None,
    ) -> None:
        self._fake = fake
        self._providers = dict(providers or {})

    def __call__(self, provider: str) -> NotificationProvider:
        kind = ProviderKind(provider).value
        if self._fake is not None:
            return self._fake
        if kind in self._providers:
            return self._providers[kind]
        if kind == ProviderKind.SMTP.value:
            return SmtpNotificationProvider()
        if kind == ProviderKind.GENERIC_WEBHOOK.value:
            return GenericWebhookNotificationProvider()
        return FeishuNotificationProvider()
