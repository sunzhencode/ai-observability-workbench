from __future__ import annotations

from dataclasses import replace

import pytest

from app.domains.investigations.runtime import (
    ProtocolProfile,
    ProviderId,
    ProviderSupportLevel,
)
from app.adapters.models.investigator import ProviderProfileRegistry
from app.adapters.models.investigator import SecureModelFactory
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel


def test_reviewed_provider_profiles_are_explicit_and_not_url_inferred() -> None:
    registry = ProviderProfileRegistry()
    expected = {
        ProviderId.OPENAI: ("https://api.openai.com/v1", ProtocolProfile.RESPONSES),
        ProviderId.DEEPSEEK: ("https://api.deepseek.com", ProtocolProfile.CHAT_COMPLETIONS),
        ProviderId.MOONSHOT: ("https://api.moonshot.cn/v1", ProtocolProfile.CHAT_COMPLETIONS),
        ProviderId.ZHIPU: ("https://open.bigmodel.cn/api/paas/v4", ProtocolProfile.CHAT_COMPLETIONS),
    }
    for provider_id, (base_url, protocol) in expected.items():
        profile = registry.resolve(provider_id)
        assert profile.base_url == base_url
        assert profile.protocol is protocol
        assert profile.support_level is ProviderSupportLevel.REVIEWED


def test_kimi_thinking_is_disabled_without_affecting_custom_gateways() -> None:
    registry = ProviderProfileRegistry()
    kimi = registry.model_settings(ProviderId.MOONSHOT, "kimi-k2.5")
    custom = registry.model_settings(ProviderId.CUSTOM, "kimi-k2.5")
    assert kimi["extra_body"] == {"thinking": {"type": "disabled"}}
    assert "extra_body" not in custom


def test_compatibility_tiers_are_honest() -> None:
    registry = ProviderProfileRegistry()
    assert registry.resolve(ProviderId.DASHSCOPE).support_level is ProviderSupportLevel.COMPATIBLE
    assert registry.resolve(ProviderId.CUSTOM).support_level is ProviderSupportLevel.BEST_EFFORT


@pytest.mark.asyncio
async def test_secure_factory_selects_protocol_and_forces_sdk_zero_retries() -> None:
    registry = ProviderProfileRegistry()
    factory = SecureModelFactory(resolver=lambda _host: ("8.8.8.8",))
    openai = registry.resolve(ProviderId.OPENAI)
    responses = factory.create(
        replace(openai, model_id="gpt-test"),
        api_key="test-key",
    )
    kimi = registry.resolve(ProviderId.MOONSHOT)
    chat = factory.create(
        replace(kimi, model_id="kimi-test"),
        api_key="test-key",
    )
    try:
        assert isinstance(responses.model, OpenAIResponsesModel)
        assert isinstance(chat.model, OpenAIChatModel)
        assert responses.model.client.max_retries == 0
        assert chat.model.client.max_retries == 0
    finally:
        await responses.http_client.aclose()
        await chat.http_client.aclose()
