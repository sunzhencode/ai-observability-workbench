"""Known OpenAI-compatible services and their base URLs.

A vendor is **not** a kind. The kind stays `OPENAI_COMPATIBLE` and carries the
egress rules; a vendor is only a preset that fills in an address, so adding one
changes what is convenient and nothing about what is permitted.

The list exists because the base URL is the field a user is most likely to get
wrong and least able to check: a typo produces a connection failure, a missing
`/v1` produces a 404, and both look like "the service is broken". Picking from a
list removes a whole class of support question.

`CUSTOM` is deliberately present. Sooner or later someone points this at a
self-hosted gateway, and a closed list would mean editing code to configure a
service the protocol already supports.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.platform.model_protocols import MOONSHOT_BASE_URL

CUSTOM_VENDOR = "CUSTOM"


@dataclass(frozen=True)
class ModelVendor:
    id: str
    label: str
    #: Empty for CUSTOM, where the user supplies it.
    base_url: str
    #: Shown next to the key field, because "where do I get one" is the next
    #: question after "which vendor" and the answer is never the same place.
    key_hint: str = ""
    #: Small, reviewed starting choices. This is not the account's full model
    #: catalogue; the optional `/models` read can add account-specific names.
    recommended_models: tuple[str, ...] = ()


#: Order is the order shown. OpenAI first because it is the reference
#: implementation and the one the docs everywhere assume.
MODEL_VENDORS: tuple[ModelVendor, ...] = (
    ModelVendor(
        id="OPENAI",
        label="OpenAI",
        base_url="https://api.openai.com/v1",
        key_hint="platform.openai.com 的 API keys 页面",
        recommended_models=("gpt-5.5",),
    ),
    ModelVendor(
        id="DEEPSEEK",
        label="DeepSeek 深度求索",
        base_url="https://api.deepseek.com/v1",
        key_hint="platform.deepseek.com",
        recommended_models=("deepseek-v4-flash", "deepseek-v4-pro"),
    ),
    ModelVendor(
        id="DASHSCOPE",
        label="阿里云百炼 / DashScope",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        key_hint="百炼控制台的 API-KEY",
        recommended_models=("qwen3.8-max", "qwen3.7-plus", "qwen3.7-flash"),
    ),
    ModelVendor(
        id="ZHIPU",
        label="智谱 GLM",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        key_hint="bigmodel.cn 的 API keys",
        recommended_models=("glm-5.2",),
    ),
    ModelVendor(
        id="MOONSHOT",
        label="月之暗面 Kimi",
        base_url=MOONSHOT_BASE_URL,
        key_hint="platform.moonshot.cn",
        recommended_models=("kimi-k2.6", "kimi-k2.5"),
    ),
    ModelVendor(
        id=CUSTOM_VENDOR,
        label="自定义（其它 OpenAI 兼容服务）",
        base_url="",
        key_hint="由该服务提供",
    ),
)

_BY_ID = {vendor.id: vendor for vendor in MODEL_VENDORS}


def vendor_for(vendor_id: str) -> ModelVendor | None:
    return _BY_ID.get(str(vendor_id or "").strip().upper())


def base_url_for(vendor_id: str, custom_base_url: str = "") -> str:
    """The address a vendor choice resolves to.

    `CUSTOM` passes the user's string through; everything else ignores it, so a
    stale value left in the form cannot override a preset that was chosen after.
    """
    vendor = vendor_for(vendor_id)
    if vendor is None:
        return str(custom_base_url or "").strip()
    if vendor.id == CUSTOM_VENDOR:
        return str(custom_base_url or "").strip()
    return vendor.base_url


__all__ = [
    "CUSTOM_VENDOR",
    "MODEL_VENDORS",
    "MOONSHOT_BASE_URL",
    "ModelVendor",
    "base_url_for",
    "vendor_for",
]
