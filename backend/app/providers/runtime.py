"""Runtime notification provider selection.

Production/local-real mode uses the bounded Feishu provider. The bundled mock
mode uses a deterministic in-memory provider and therefore cannot perform any
network I/O, even when a channel contains a syntactically valid webhook URL.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from app.config import settings
from app.providers.feishu import (
    FeishuConfig,
    FeishuProvider,
    ProviderResult,
)


def _script_result(token: str) -> ProviderResult:
    code = str(token or "OK").strip().upper()
    if code == "OK":
        return ProviderResult(True, "OK", http_status=200)
    if code in {"TIMEOUT", "NETWORK"}:
        return ProviderResult(False, code, transient=True)
    if code.startswith("HTTP_"):
        try:
            status = int(code.removeprefix("HTTP_"))
        except ValueError as exc:
            raise ValueError(f"unsupported fake provider result: {token}") from exc
        return ProviderResult(
            False,
            code,
            transient=status == 429 or status >= 500,
            http_status=status,
            error_summary="scripted mock provider failure",
        )
    if code.startswith("FEISHU_"):
        provider_code = code.removeprefix("FEISHU_")
        return ProviderResult(
            False,
            code,
            transient=provider_code == "11232",
            http_status=200,
            error_summary="scripted mock provider rejection",
        )
    raise ValueError(f"unsupported fake provider result: {token}")


def _parse_script(value: str) -> list[ProviderResult]:
    tokens = [item.strip() for item in str(value or "").split(",") if item.strip()]
    return [_script_result(item) for item in (tokens or ["OK"])]


class ScriptedFakeFeishuProvider:
    """Purpose-separated deterministic fake; never validates or opens a URL."""

    kind = "FEISHU_CUSTOM_BOT"

    def __init__(self, channel_test_script: str, delivery_script: str) -> None:
        self._results = {
            "CHANNEL_TEST": _parse_script(channel_test_script),
            "DELIVERY": _parse_script(delivery_script),
        }
        self.calls: list[tuple[str, dict[str, Any], FeishuConfig]] = []

    async def send(
        self,
        payload: dict[str, Any],
        config: FeishuConfig,
        **kwargs: Any,
    ) -> ProviderResult:
        purpose = str(kwargs.get("purpose") or "DELIVERY").upper()
        queue = self._results.get(purpose, self._results["DELIVERY"])
        self.calls.append((purpose, payload, config))
        if len(queue) > 1:
            return queue.pop(0)
        return queue[0]


@lru_cache(maxsize=4)
def _provider_for(
    mode: str,
    channel_test_script: str,
    delivery_script: str,
) -> FeishuProvider | ScriptedFakeFeishuProvider:
    if mode == "FAKE":
        return ScriptedFakeFeishuProvider(channel_test_script, delivery_script)
    if mode != "FEISHU":
        raise ValueError("notification_provider_mode must be FEISHU or FAKE")
    return FeishuProvider()


def get_notification_provider() -> FeishuProvider | ScriptedFakeFeishuProvider:
    return _provider_for(
        settings.notification_provider_mode.strip().upper(),
        settings.notification_fake_channel_test_script,
        settings.notification_fake_delivery_script,
    )


def reset_notification_provider_cache() -> None:
    """Test/developer hook; application code normally keeps one process provider."""

    _provider_for.cache_clear()
