"""Reviewed request-shape additions for exact official model targets."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

MOONSHOT_BASE_URL = "https://api.moonshot.cn/v1"
MOONSHOT_NON_THINKING_MODELS = frozenset({"kimi-k2.5", "kimi-k2.6"})


def chat_completion_extra_body(base_url: str, model: str) -> dict[str, Any]:
    """Return provider-specific Chat fields without matching custom gateways.

    Kimi K2.5/K2.6 enable thinking by default. The investigation protocol needs
    a model to either choose one closed read-only tool or return one strict JSON
    object, so the official Moonshot API documents disabling thinking for this
    mode. Match the complete target and exact model IDs: a custom gateway named
    like Moonshot must never inherit provider-specific request fields.
    """

    parsed = urlsplit(str(base_url or "").strip())
    official_moonshot = (
        parsed.scheme.lower() == "https"
        and (parsed.hostname or "").lower() == "api.moonshot.cn"
        and (parsed.port or 443) == 443
        and parsed.path.rstrip("/") == "/v1"
        and not parsed.query
        and not parsed.fragment
    )
    model_id = str(model or "").strip().lower()
    if official_moonshot and model_id in MOONSHOT_NON_THINKING_MODELS:
        return {"thinking": {"type": "disabled"}}
    return {}


__all__ = [
    "MOONSHOT_BASE_URL",
    "MOONSHOT_NON_THINKING_MODELS",
    "chat_completion_extra_body",
]
