"""Mock provider selection and fault-script isolation tests."""

from __future__ import annotations

import pytest

from app.config import settings
from app.providers.feishu import FeishuConfig, FeishuProvider
from app.providers.runtime import (
    ScriptedFakeFeishuProvider,
    get_notification_provider,
    reset_notification_provider_cache,
)


@pytest.fixture(autouse=True)
def reset_provider() -> None:
    reset_notification_provider_cache()
    yield
    reset_notification_provider_cache()


async def test_fake_provider_has_separate_test_and_delivery_fault_scripts(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "notification_provider_mode", "FAKE")
    monkeypatch.setattr(
        settings, "notification_fake_channel_test_script", "HTTP_400,OK"
    )
    monkeypatch.setattr(
        settings, "notification_fake_delivery_script", "HTTP_500,OK"
    )
    provider = get_notification_provider()
    assert isinstance(provider, ScriptedFakeFeishuProvider)
    config = FeishuConfig(
        webhook="https://open.feishu.cn/open-apis/bot/v2/hook/not-opened-by-fake"
    )

    channel_test = await provider.send({}, config, purpose="CHANNEL_TEST")
    delivery = await provider.send({}, config, purpose="DELIVERY")
    assert (channel_test.code, channel_test.transient) == ("HTTP_400", False)
    assert (delivery.code, delivery.transient) == ("HTTP_500", True)
    assert (await provider.send({}, config, purpose="CHANNEL_TEST")).ok
    assert (await provider.send({}, config, purpose="DELIVERY")).ok
    assert [call[0] for call in provider.calls] == [
        "CHANNEL_TEST",
        "DELIVERY",
        "CHANNEL_TEST",
        "DELIVERY",
    ]


def test_real_mode_keeps_bounded_feishu_provider(monkeypatch) -> None:
    monkeypatch.setattr(settings, "notification_provider_mode", "FEISHU")
    assert type(get_notification_provider()) is FeishuProvider


def test_unknown_fake_script_code_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(settings, "notification_provider_mode", "FAKE")
    monkeypatch.setattr(settings, "notification_fake_delivery_script", "SURPRISE")
    with pytest.raises(ValueError, match="unsupported fake provider result"):
        get_notification_provider()
