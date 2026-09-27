"""Feishu custom-bot protocol boundary tests."""

from __future__ import annotations

import base64
import hashlib
import hmac

import httpx
import pytest

from app.providers.feishu import (
    FeishuConfig,
    FeishuProvider,
    build_signature,
    is_valid_open_id,
    validate_feishu_webhook,
)


def test_signature_uses_official_timestamp_newline_secret_vector() -> None:
    timestamp = 1_700_000_000
    secret = "signing-secret"
    expected = base64.b64encode(
        hmac.new(f"{timestamp}\n{secret}".encode(), digestmod=hashlib.sha256).digest()
    ).decode()
    assert build_signature(timestamp, secret) == expected


@pytest.mark.parametrize(
    "url",
    [
        "http://open.feishu.cn/open-apis/bot/v2/hook/token",
        "https://example.com/open-apis/bot/v2/hook/token",
        "https://open.feishu.cn/open-apis/bot/v1/hook/token",
        "https://open.feishu.cn/open-apis/bot/v2/hook/token?leak=yes",
        # `urlsplit().hostname` drops the port, so the host allowlist alone
        # would let any port on the official host through.
        "https://open.feishu.cn:8443/open-apis/bot/v2/hook/token",
    ],
)
def test_webhook_must_be_https_official_v2_without_query(url: str) -> None:
    with pytest.raises(ValueError):
        validate_feishu_webhook(url)


def test_explicit_default_https_port_is_still_official() -> None:
    url = "https://open.feishu.cn:443/open-apis/bot/v2/hook/token"
    assert validate_feishu_webhook(url) == url


@pytest.mark.parametrize(
    "value",
    [
        # The id lands inside `<at id=...></at>` in a lark_md card, so a value
        # carrying `>` can close the tag and open an `@all` after it.
        "x></at><at id=all",
        "ou_good><b",
        "has space",
        "",
        "x" * 129,
    ],
)
def test_open_id_rejects_anything_that_could_escape_the_mention_tag(
    value: str,
) -> None:
    assert is_valid_open_id(value) is False


def test_open_id_accepts_the_real_feishu_shape() -> None:
    assert is_valid_open_id("ou_c245b0a7dff2725cfa2fb104f8b48b9d") is True


@pytest.mark.asyncio
async def test_provider_classifies_business_error_without_redirect() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.read()
        return httpx.Response(200, json={"code": 11232, "msg": "rate limited"})

    provider = FeishuProvider(transport=httpx.MockTransport(handler))
    result = await provider.send(
        {"msg_type": "text", "content": {"text": "safe test"}},
        FeishuConfig(
            webhook="https://open.feishu.cn/open-apis/bot/v2/hook/test-token",
            signing_secret="sign",
        ),
        timestamp=1_700_000_000,
    )

    assert result.ok is False
    assert result.transient is True
    assert result.code == "FEISHU_11232"
    assert b'"timestamp":"1700000000"' in seen["body"]
    assert b'"sign":' in seen["body"]


@pytest.mark.asyncio
async def test_provider_rejects_oversized_body_before_network() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"code": 0})

    result = await FeishuProvider(transport=httpx.MockTransport(handler)).send(
        {"msg_type": "text", "content": {"text": "x" * 20_000}},
        FeishuConfig(
            webhook="https://open.feishu.cn/open-apis/bot/v2/hook/test-token"
        ),
    )
    assert result.code == "PAYLOAD_TOO_LARGE"
    assert called is False
