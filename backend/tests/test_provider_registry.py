"""The lookup that will replace the delivery worker's single provider instance.

F24 阶段 1 only establishes the seam; nothing dispatches through it yet, so
these tests pin the two properties that must not drift while the rest is built:
a kind with no implementation cannot be offered, and mock mode cannot reach the
network through any kind.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.providers.feishu import FeishuProvider, ProviderResult
from app.providers.registry import (
    FEISHU_CUSTOM_BOT,
    GENERIC_WEBHOOK,
    KNOWN_KINDS,
    SMTP,
    SUPPORTED_KINDS,
    NotificationProvider,
    UnsupportedProviderKind,
    get_provider,
    supported_kinds,
)
from app.providers.runtime import (
    ScriptedFakeFeishuProvider,
    reset_notification_provider_cache,
)


@pytest.fixture(autouse=True)
def _restore_provider_mode():
    original = settings.notification_provider_mode
    reset_notification_provider_cache()
    yield
    settings.notification_provider_mode = original
    reset_notification_provider_cache()


def test_only_implemented_kinds_are_offered() -> None:
    # A kind joins this list when something can send it, never before: the API
    # must not offer a channel nothing implements.
    assert supported_kinds() == (FEISHU_CUSTOM_BOT, SMTP, GENERIC_WEBHOOK)
    # Unimplemented kinds stay known, so historical rows naming them are readable.
    assert set(SUPPORTED_KINDS) <= set(KNOWN_KINDS)


def test_an_unimplemented_kind_is_refused_by_name() -> None:
    for kind in ("SLACK", "DINGTALK"):
        with pytest.raises(UnsupportedProviderKind) as caught:
            get_provider(kind)
        assert caught.value.kind == kind


def test_a_blank_kind_reads_as_the_feishu_default() -> None:
    # Rows written before F24 carry the column default; they must keep working.
    settings.notification_provider_mode = "FEISHU"
    reset_notification_provider_cache()
    assert isinstance(get_provider(""), FeishuProvider)


def test_real_mode_returns_the_bounded_feishu_client() -> None:
    settings.notification_provider_mode = "FEISHU"
    reset_notification_provider_cache()
    provider = get_provider(FEISHU_CUSTOM_BOT)
    assert isinstance(provider, FeishuProvider)
    assert provider.kind == FEISHU_CUSTOM_BOT


def test_mock_mode_cannot_reach_the_network_through_any_kind() -> None:
    settings.notification_provider_mode = "FAKE"
    reset_notification_provider_cache()
    for kind in SUPPORTED_KINDS:
        assert isinstance(get_provider(kind), ScriptedFakeFeishuProvider)


def test_real_mode_returns_each_kind_its_own_implementation() -> None:
    from app.providers.smtp import SmtpProvider

    settings.notification_provider_mode = "FEISHU"
    reset_notification_provider_cache()
    assert isinstance(get_provider(FEISHU_CUSTOM_BOT), FeishuProvider)
    assert isinstance(get_provider(SMTP), SmtpProvider)
    from app.providers.generic_webhook import GenericWebhookProvider

    assert isinstance(get_provider(GENERIC_WEBHOOK), GenericWebhookProvider)


def test_implementations_satisfy_the_protocol() -> None:
    settings.notification_provider_mode = "FEISHU"
    reset_notification_provider_cache()
    assert isinstance(get_provider(FEISHU_CUSTOM_BOT), NotificationProvider)
    settings.notification_provider_mode = "FAKE"
    reset_notification_provider_cache()
    assert isinstance(get_provider(FEISHU_CUSTOM_BOT), NotificationProvider)


@pytest.mark.asyncio
async def test_the_fake_still_answers_with_a_provider_result() -> None:
    settings.notification_provider_mode = "FAKE"
    reset_notification_provider_cache()
    provider = get_provider(FEISHU_CUSTOM_BOT)
    result = await provider.send({}, None, purpose="DELIVERY")
    assert isinstance(result, ProviderResult)
    assert result.ok is True
