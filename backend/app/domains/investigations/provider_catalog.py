"""Explicit provider capabilities shared by configuration and runtime adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from app.domains.investigations.runtime import (
    ProtocolProfile,
    ProviderId,
    ProviderProfile,
    ProviderSupportLevel,
)


@dataclass(frozen=True, slots=True)
class ProviderDefinition:
    provider_id: ProviderId
    label: str
    protocol: ProtocolProfile
    support_level: ProviderSupportLevel
    base_url: str
    key_hint: str
    recommended_models: tuple[str, ...]


PROVIDER_CATALOG: Mapping[ProviderId, ProviderDefinition] = {
    ProviderId.OPENAI: ProviderDefinition(
        ProviderId.OPENAI,
        "OpenAI",
        ProtocolProfile.RESPONSES,
        ProviderSupportLevel.REVIEWED,
        "https://api.openai.com/v1",
        "platform.openai.com API keys",
        ("gpt-5.5",),
    ),
    ProviderId.DEEPSEEK: ProviderDefinition(
        ProviderId.DEEPSEEK,
        "DeepSeek 深度求索",
        ProtocolProfile.CHAT_COMPLETIONS,
        ProviderSupportLevel.REVIEWED,
        "https://api.deepseek.com",
        "platform.deepseek.com",
        ("deepseek-v4-flash", "deepseek-v4-pro"),
    ),
    ProviderId.DASHSCOPE: ProviderDefinition(
        ProviderId.DASHSCOPE,
        "阿里云百炼 / DashScope",
        ProtocolProfile.CHAT_COMPLETIONS,
        ProviderSupportLevel.COMPATIBLE,
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "百炼控制台 API-KEY",
        ("qwen3.8-max", "qwen3.7-plus", "qwen3.7-flash"),
    ),
    ProviderId.ZHIPU: ProviderDefinition(
        ProviderId.ZHIPU,
        "智谱 GLM",
        ProtocolProfile.CHAT_COMPLETIONS,
        ProviderSupportLevel.REVIEWED,
        "https://open.bigmodel.cn/api/paas/v4",
        "bigmodel.cn API keys",
        ("glm-5.2",),
    ),
    ProviderId.MOONSHOT: ProviderDefinition(
        ProviderId.MOONSHOT,
        "月之暗面 Kimi",
        ProtocolProfile.CHAT_COMPLETIONS,
        ProviderSupportLevel.REVIEWED,
        "https://api.moonshot.cn/v1",
        "platform.moonshot.cn",
        ("kimi-k2.6", "kimi-k2.5"),
    ),
    ProviderId.CUSTOM: ProviderDefinition(
        ProviderId.CUSTOM,
        "自定义 OpenAI 兼容服务",
        ProtocolProfile.CHAT_COMPLETIONS,
        ProviderSupportLevel.BEST_EFFORT,
        "",
        "由该服务提供",
        (),
    ),
}


def provider_settings(provider_id: ProviderId, model_id: str) -> dict[str, object]:
    settings: dict[str, object] = {"parallel_tool_calls": False}
    if provider_id is ProviderId.OPENAI:
        settings.update({"openai_store": False, "openai_truncation": "disabled"})
    if provider_id is ProviderId.MOONSHOT and model_id.lower() in {"kimi-k2.5", "kimi-k2.6"}:
        settings["extra_body"] = {"thinking": {"type": "disabled"}}
    return settings


def build_provider_profile(
    provider_id: ProviderId,
    *,
    model_id: str,
    custom_base_url: str = "",
) -> ProviderProfile:
    definition = PROVIDER_CATALOG[provider_id]
    base_url = custom_base_url if provider_id is ProviderId.CUSTOM else definition.base_url
    if not base_url.strip():
        raise ValueError("MODEL_BASE_URL_REQUIRED")
    return ProviderProfile(
        provider_id=provider_id,
        protocol=definition.protocol,
        support_level=definition.support_level,
        base_url=base_url,
        model_id=model_id,
        settings=provider_settings(provider_id, model_id),
    )


__all__ = [
    "PROVIDER_CATALOG",
    "ProviderDefinition",
    "build_provider_profile",
    "provider_settings",
]
