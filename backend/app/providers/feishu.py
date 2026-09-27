"""Bounded Feishu custom-bot V2 provider."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

MAX_REQUEST_BYTES = 18 * 1024
# A signed channel appends `timestamp` and `sign` to the body after rendering.
# Rendering has to leave room for them, or a card that renders fine turns into a
# permanent delivery failure on signed channels only.
SIGNATURE_OVERHEAD_BYTES = 128
ALLOWED_HOSTS = {"open.feishu.cn", "open.larksuite.com"}
ALLOWED_PORTS = {None, 443}
WEBHOOK_PREFIX = "/open-apis/bot/v2/hook/"
# Feishu open_id is `ou_` + hex in practice. Pinning the character set matters
# beyond tidiness: the id is interpolated into `<at id=...></at>` inside a
# lark_md card, so a value carrying `>` can close the tag and open another one --
# turning a channel configured to mention one person into an `@all`.
OPEN_ID_PATTERN = re.compile(r"\A[A-Za-z0-9_-]{1,128}\Z")


def is_valid_open_id(value: Any) -> bool:
    return bool(OPEN_ID_PATTERN.fullmatch(str(value or "")))


@dataclass(frozen=True)
class FeishuConfig:
    webhook: str
    signing_secret: str = ""
    required_keyword: str | None = None


@dataclass(frozen=True)
class ProviderResult:
    ok: bool
    code: str
    transient: bool = False
    http_status: int | None = None
    request_id: str | None = None
    error_summary: str | None = None


def validate_feishu_webhook(value: str) -> str:
    url = str(value or "").strip()
    parts = urlsplit(url)
    token = parts.path.removeprefix(WEBHOOK_PREFIX)
    try:
        port = parts.port
    except ValueError:
        raise ValueError("webhook must be an official HTTPS Feishu/Lark V2 bot URL")
    if (
        parts.scheme != "https"
        or parts.hostname not in ALLOWED_HOSTS
        # `hostname` drops the port, so without this an arbitrary port on the
        # official host would pass.
        or port not in ALLOWED_PORTS
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or not parts.path.startswith(WEBHOOK_PREFIX)
        or not token
        or "/" in token
        or len(token) > 256
        or not all(char.isalnum() or char in "_-" for char in token)
    ):
        raise ValueError("webhook must be an official HTTPS Feishu/Lark V2 bot URL")
    return url


def build_signature(timestamp: int, secret: str) -> str:
    key = f"{int(timestamp)}\n{secret}".encode("utf-8")
    digest = hmac.new(key, digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


class FeishuProvider:
    # The channel kind this implements. `providers/registry.py` looks it up by
    # this name, which is also what a RouteSnapshot records at routing time.
    kind = "FEISHU_CUSTOM_BOT"

    def __init__(
        self,
        *,
        timeout_seconds: float = 8.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.timeout_seconds = min(max(float(timeout_seconds), 1.0), 15.0)
        self.transport = transport

    async def send(
        self,
        payload: dict[str, Any],
        config: FeishuConfig,
        *,
        timestamp: int | None = None,
        purpose: str | None = None,
    ) -> ProviderResult:
        del purpose
        try:
            webhook = validate_feishu_webhook(config.webhook)
        except ValueError:
            return ProviderResult(False, "INVALID_WEBHOOK", error_summary="invalid webhook")

        body = dict(payload)
        if config.signing_secret:
            current = int(time.time()) if timestamp is None else int(timestamp)
            body["timestamp"] = str(current)
            body["sign"] = build_signature(current, config.signing_secret)
        encoded = json.dumps(
            body, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > MAX_REQUEST_BYTES:
            return ProviderResult(False, "PAYLOAD_TOO_LARGE")

        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                transport=self.transport,
                follow_redirects=False,
            ) as client:
                response = await client.post(
                    webhook,
                    content=encoded,
                    headers={"Content-Type": "application/json"},
                )
        except httpx.TimeoutException:
            return ProviderResult(False, "TIMEOUT", transient=True)
        except httpx.HTTPError:
            return ProviderResult(False, "NETWORK", transient=True)

        request_id = response.headers.get("x-request-id")
        if 300 <= response.status_code < 400:
            return ProviderResult(
                False,
                "REDIRECT_REJECTED",
                http_status=response.status_code,
                request_id=request_id,
            )
        if response.status_code == 429 or response.status_code >= 500:
            return ProviderResult(
                False,
                f"HTTP_{response.status_code}",
                transient=True,
                http_status=response.status_code,
                request_id=request_id,
            )
        if response.status_code >= 400:
            return ProviderResult(
                False,
                f"HTTP_{response.status_code}",
                http_status=response.status_code,
                request_id=request_id,
            )
        if len(response.content) > 1024 * 1024:
            return ProviderResult(
                False,
                "RESPONSE_TOO_LARGE",
                http_status=response.status_code,
                request_id=request_id,
            )
        try:
            result = response.json()
        except ValueError:
            return ProviderResult(
                False,
                "INVALID_RESPONSE",
                http_status=response.status_code,
                request_id=request_id,
            )
        code = result.get("code", result.get("StatusCode")) if isinstance(result, dict) else None
        if code in (0, "0"):
            return ProviderResult(
                True,
                "OK",
                http_status=response.status_code,
                request_id=request_id,
            )
        safe_code = str(code) if code is not None else "UNKNOWN"
        transient = safe_code == "11232"
        return ProviderResult(
            False,
            f"FEISHU_{safe_code}",
            transient=transient,
            http_status=response.status_code,
            request_id=request_id,
            error_summary="provider rejected request",
        )


class FakeFeishuProvider:
    """Deterministic provider for tests/mock; never performs network I/O."""

    kind = "FEISHU_CUSTOM_BOT"

    def __init__(self, results: list[ProviderResult] | None = None) -> None:
        self.results = list(results or [ProviderResult(True, "OK")])
        self.calls: list[tuple[dict[str, Any], FeishuConfig]] = []

    async def send(
        self,
        payload: dict[str, Any],
        config: FeishuConfig,
        **_kwargs: Any,
    ) -> ProviderResult:
        self.calls.append((payload, config))
        if len(self.results) > 1:
            return self.results.pop(0)
        return self.results[0]
