"""Logical channel/revision activation and explicit-test behavior."""

from __future__ import annotations

import pytest
from sqlmodel import select

from app.crypto import SecretBox
from app.models import ConfigAudit, NotificationChannelRevision, NotificationDelivery
from app.providers.feishu import ProviderResult
from app.services.notification_channels import (
    activate_channel_revision,
    create_channel,
    disable_channel,
    resolve_active_channel_config,
    test_channel_revision,
    update_channel_draft,
)


class FakeProvider:
    def __init__(self, result: ProviderResult) -> None:
        self.result = result
        self.calls: list[tuple[dict, object]] = []

    async def send(self, payload, config, **kwargs) -> ProviderResult:
        self.calls.append((payload, config))
        return self.result


@pytest.mark.asyncio
async def test_explicit_test_writes_audit_but_never_delivery(session) -> None:
    box = SecretBox("channel-master-key")
    channel, revision = create_channel(
        session,
        name="platform oncall",
        webhook_action="REPLACE",
        webhook_value="https://open.feishu.cn/open-apis/bot/v2/hook/test-token",
        signing_action="CLEAR",
        signing_value=None,
        required_keyword="Alert Workbench",
        mention_mode="USERS",
        mention_users=[{"open_id": "ou_test_user"}],
        mention_on={"FIRING_OPENED": True},
        box=box,
    )
    provider = FakeProvider(ProviderResult(ok=True, code="OK"))

    result = await test_channel_revision(
        session, revision.id, box=box, provider=provider
    )
    session.commit()

    assert result.ok is True
    assert len(provider.calls) == 1
    assert provider.calls[0][1].webhook.endswith("test-token")
    assert "Alert Workbench" in repr(provider.calls[0][0])
    assert session.exec(select(NotificationDelivery)).all() == []
    assert session.exec(
        select(ConfigAudit).where(ConfigAudit.action == "TEST")
    ).one().result == "SUCCESS"
    assert channel.active_revision_id is None


@pytest.mark.asyncio
async def test_failed_draft_does_not_replace_active_channel(session) -> None:
    box = SecretBox("channel-master-key")
    channel, first = create_channel(
        session,
        name="database oncall",
        webhook_action="REPLACE",
        webhook_value="https://open.feishu.cn/open-apis/bot/v2/hook/first-token",
        signing_action="CLEAR",
        signing_value=None,
        required_keyword=None,
        mention_mode="NONE",
        mention_users=[],
        mention_on={},
        box=box,
    )
    await test_channel_revision(
        session,
        first.id,
        box=box,
        provider=FakeProvider(ProviderResult(ok=True, code="OK")),
    )
    activate_channel_revision(session, first.id, expected_version=1, box=box)

    draft = update_channel_draft(
        session,
        channel.id,
        expected_version=1,
        webhook_action="REPLACE",
        webhook_value="https://open.feishu.cn/open-apis/bot/v2/hook/second-token",
        signing_action="KEEP",
        signing_value=None,
        required_keyword=None,
        mention_mode="NONE",
        mention_users=[],
        mention_on={},
        box=box,
    )
    await test_channel_revision(
        session,
        draft.id,
        box=box,
        provider=FakeProvider(
            ProviderResult(ok=False, code="FEISHU_19001", transient=False)
        ),
    )
    with pytest.raises(ValueError, match="successful test"):
        activate_channel_revision(session, draft.id, expected_version=2, box=box)
    session.commit()

    stored = session.exec(
        select(NotificationChannelRevision).where(
            NotificationChannelRevision.channel_id == channel.id,
            NotificationChannelRevision.state == "ACTIVE",
        )
    ).one()
    assert stored.id == first.id
    disable_channel(session, channel.id)
    session.commit()
    assert channel.state == "DISABLED"


@pytest.mark.asyncio
async def test_same_logical_channel_resolves_rotated_active_credentials(session) -> None:
    box = SecretBox("channel-master-key")
    channel, first = create_channel(
        session,
        name="rotation channel",
        webhook_action="REPLACE",
        webhook_value="https://open.feishu.cn/open-apis/bot/v2/hook/old-token",
        signing_action="CLEAR",
        signing_value=None,
        required_keyword=None,
        mention_mode="NONE",
        mention_users=[],
        mention_on={},
        box=box,
    )
    ok = FakeProvider(ProviderResult(ok=True, code="OK"))
    await test_channel_revision(session, first.id, box=box, provider=ok)
    activate_channel_revision(session, first.id, expected_version=1, box=box)
    second = update_channel_draft(
        session,
        channel.id,
        expected_version=1,
        webhook_action="REPLACE",
        webhook_value="https://open.feishu.cn/open-apis/bot/v2/hook/new-token",
        signing_action="KEEP",
        signing_value=None,
        required_keyword=None,
        mention_mode="NONE",
        mention_users=[],
        mention_on={},
        box=box,
    )
    await test_channel_revision(session, second.id, box=box, provider=ok)
    activate_channel_revision(session, second.id, expected_version=2, box=box)
    session.commit()

    resolved_revision, config = resolve_active_channel_config(
        session, channel.id, box=box
    )
    assert resolved_revision.id == second.id
    assert config.webhook.endswith("new-token")
