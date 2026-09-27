"""Bounded generic-webhook provider.

This is the kind the original security boundary existed to prevent, so it is the
one that has to carry the guard properly. `assert_public_https` runs **before
every send**, not once at save time: a hostname that resolved to a public
address yesterday can point at 169.254.169.254 today, and the channel would
never be edited in between.

What it does not do, deliberately (ADR 0007): follow redirects, parse the
response body, accept a callback, or send to anything but HTTPS on 443. The
workbench posts once and reads a status code.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.providers.egress import EgressRejected, assert_public_https
from app.providers.feishu import ProviderResult

MAX_REQUEST_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
#: The payload is a contract with whatever the user pointed at. Changing a field
#: means changing this, the same as any other API.
PAYLOAD_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class GenericWebhookConfig:
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    signing_secret: str = ""
    timeout_seconds: float = 8.0


def build_signature(timestamp: int, body: bytes, secret: str) -> str:
    """HMAC over `timestamp.body`, the shape most receivers already expect."""

    message = f"{int(timestamp)}.".encode("utf-8") + body
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


class GenericWebhookProvider:
    kind = "GENERIC_WEBHOOK"

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver=None,
    ) -> None:
        self.transport = transport
        self.resolver = resolver

    async def send(
        self,
        payload: dict[str, Any],
        config: GenericWebhookConfig,
        *,
        purpose: str | None = None,
        timestamp: int | None = None,
    ) -> ProviderResult:
        del purpose
        try:
            assert_public_https(config.url, resolver=self.resolver)
        except EgressRejected as exc:
            return ProviderResult(False, exc.code, error_summary="target refused")

        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        if len(body) > MAX_REQUEST_BYTES:
            return ProviderResult(False, "PAYLOAD_TOO_LARGE")

        headers = {"Content-Type": "application/json", **dict(config.headers or {})}
        if config.signing_secret:
            current = int(time.time()) if timestamp is None else int(timestamp)
            headers["X-Workbench-Timestamp"] = str(current)
            headers["X-Workbench-Signature"] = build_signature(
                current, body, config.signing_secret
            )

        try:
            async with httpx.AsyncClient(
                timeout=config.timeout_seconds,
                transport=self.transport,
                follow_redirects=False,
            ) as client:
                response = await client.post(config.url, content=body, headers=headers)
        except httpx.TimeoutException:
            return ProviderResult(False, "TIMEOUT", transient=True)
        except httpx.HTTPError:
            return ProviderResult(False, "NETWORK", transient=True)

        status = response.status_code
        if 300 <= status < 400:
            # A redirect is the standard way to walk a bounded client somewhere
            # the guard never saw.
            return ProviderResult(False, "REDIRECT_REJECTED", http_status=status)
        if len(response.content) > MAX_RESPONSE_BYTES:
            return ProviderResult(False, "RESPONSE_TOO_LARGE", http_status=status)
        if status == 429 or status >= 500:
            return ProviderResult(
                False, f"HTTP_{status}", transient=True, http_status=status
            )
        if status >= 400:
            return ProviderResult(False, f"HTTP_{status}", http_status=status)
        # Success is a 2xx. The body is not read: this is not ChatOps, and a
        # receiver's response is not something the workbench acts on.
        return ProviderResult(True, "OK", http_status=status)
