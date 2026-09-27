"""Deterministic policy matching, preview, and activation safety."""

from __future__ import annotations

import pytest
from datetime import datetime, timezone
from sqlmodel import select

from app.crypto import SecretBox
from app.models import Incident, NotificationRoute
from app.providers.feishu import FakeFeishuProvider
from app.services.ingest import ingest_alerts
from app.services.notification_channels import (
    activate_channel_revision,
    create_channel,
    test_channel_revision,
)
from app.services.policy_activation import (
    ActivationTokenError,
    ActivationTokenService,
    confirm_activation,
    prepare_activation,
)
from app.services.notification_policies import (
    PolicyCandidate,
    choose_policy,
    incident_route_context,
    matcher_matches,
    preview_policy,
    create_policy_draft,
    validate_policy_matchers,
)
from app.services.source_identity import active_source_id


def _incident(**overrides) -> Incident:
    values = {
        "group_key": "source=am:test|rule=1|cluster=prod-a",
        "title": "TargetDown · prod-a",
        "source_id": "am:test",
        "environment": "prod",
        "severity": "critical",
        "aggregation_rule_id": 1,
        "group_labels": {"cluster": "prod-a", "team": "platform"},
    }
    values.update(overrides)
    return Incident(**values)


@pytest.mark.parametrize(
    ("matcher", "expected"),
    [
        ({"field": "source_id", "operator": "=", "value": "am:test"}, True),
        ({"field": "severity", "operator": "!=", "value": "warning"}, True),
        ({"field": "group.cluster", "operator": "=~", "value": "prod-.*"}, True),
        ({"field": "group.missing", "operator": "=", "value": ""}, True),
        ({"field": "group.team", "operator": "!~", "value": "database"}, True),
    ],
)
def test_matchers_use_only_stable_context(matcher, expected) -> None:
    context = incident_route_context(_incident())
    assert matcher_matches(context, matcher) is expected
    assert "member_labels" not in context


def test_invalid_policy_matchers_fail_before_save() -> None:
    with pytest.raises(ValueError, match="stable Incident route field"):
        validate_policy_matchers(
            [{"field": "environment", "operator": "=", "value": "prod"}]
        )
    with pytest.raises(ValueError, match="field"):
        validate_policy_matchers(
            [{"field": "labels.pod", "operator": "=", "value": "x"}]
        )
    with pytest.raises(ValueError, match="regex"):
        validate_policy_matchers(
            [{"field": "severity", "operator": "=~", "value": "["}]
        )


def test_priority_then_id_first_match_and_catch_all() -> None:
    context = incident_route_context(_incident())
    policies = [
        PolicyCandidate(id=20, logical_id="catch", name="catch", priority=100, matchers=[]),
        PolicyCandidate(
            id=10,
            logical_id="critical",
            name="critical",
            priority=10,
            matchers=[{"field": "severity", "operator": "=", "value": "critical"}],
        ),
        PolicyCandidate(
            id=5,
            logical_id="same-priority",
            name="same-priority",
            priority=10,
            matchers=[{"field": "source_id", "operator": "=", "value": "am:test"}],
        ),
    ]
    assert choose_policy(context, policies).id == 5


def test_preview_reports_direct_shadowed_final_and_unrouted() -> None:
    incidents = [
        _incident(id=1, severity="critical"),
        _incident(id=2, severity="warning"),
        _incident(id=3, source_id="am:qa", severity="info"),
    ]
    active = [
        PolicyCandidate(
            id=1,
            logical_id="higher",
            name="higher",
            priority=1,
            matchers=[{"field": "severity", "operator": "=", "value": "critical"}],
        )
    ]
    candidate = PolicyCandidate(
        id=50,
        logical_id="candidate",
        name="selected source",
        priority=10,
        matchers=[],
        channel_ids=[7],
        scope_mode="SELECTED",
        source_ids=("am:test",),
    )
    result = preview_policy(incidents, candidate=candidate, active=active)
    assert result.direct_match_count == 2
    assert result.shadowed_count == 1
    assert result.final_match_count == 1
    assert result.unrouted_count == 1
    assert result.samples


async def test_activation_defaults_to_no_existing_and_token_binds_exact_set(session) -> None:
    box = SecretBox("policy-channel-key")
    channel, revision = create_channel(
        session,
        name="policy channel",
        webhook_action="REPLACE",
        webhook_value="https://open.feishu.cn/open-apis/bot/v2/hook/policy-token",
        signing_action="CLEAR",
        signing_value=None,
        required_keyword=None,
        mention_mode="NONE",
        mention_users=[],
        mention_on={},
        box=box,
    )
    await test_channel_revision(
        session, revision.id, box=box, provider=FakeFeishuProvider()
    )
    activate_channel_revision(session, revision.id, expected_version=1, box=box)
    ingest_alerts(
        session,
        [
            {
                "fingerprint": "existing",
                "labels": {"alertname": "TargetDown", "severity": "warning"},
                "annotations": {},
            }
        ],
        poll_time=datetime(2026, 7, 18, 2, 0, tzinfo=timezone.utc),
    )
    policy = create_policy_draft(
        session,
        name="catch all",
        priority=100,
        matchers=[],
        repeat_interval_seconds=0,
        channel_ids=[channel.id],
    )
    session.flush()
    token_service = ActivationTokenService(key=b"x" * 32)
    prepared = prepare_activation(
        session,
        policy.id,
        expected_version=1,
        notify_existing=False,
        source_id="am:" + "unused",
        token_service=token_service,
    )
    confirm_activation(
        session,
        policy.id,
        expected_version=1,
        notify_existing=False,
        token=prepared.token,
        source_id="am:" + "unused",
        token_service=token_service,
    )
    session.commit()
    assert session.exec(select(NotificationRoute)).all() == []


def test_activation_token_rejects_changed_incident_set() -> None:
    token_service = ActivationTokenService(key=b"y" * 32, ttl_seconds=60)
    revision = type("Revision", (), {"id": 3, "version": 2})()
    token, _ = token_service.issue(
        revision=revision,
        notify_existing=True,
        incident_ids=[1, 2],
        now_epoch=100,
    )
    with pytest.raises(ActivationTokenError, match="set changed"):
        token_service.verify(
            token,
            revision=revision,
            notify_existing=True,
            incident_ids=[1, 2, 3],
            now_epoch=110,
        )


async def test_explicit_existing_activation_creates_exact_route_set(session) -> None:
    box = SecretBox("policy-explicit-key")
    channel, revision = create_channel(
        session,
        name="explicit channel",
        webhook_action="REPLACE",
        webhook_value="https://open.feishu.cn/open-apis/bot/v2/hook/explicit-token",
        signing_action="CLEAR",
        signing_value=None,
        required_keyword=None,
        mention_mode="NONE",
        mention_users=[],
        mention_on={},
        box=box,
    )
    await test_channel_revision(
        session, revision.id, box=box, provider=FakeFeishuProvider()
    )
    activate_channel_revision(session, revision.id, expected_version=1, box=box)
    ingest_alerts(
        session,
        [
            {
                "fingerprint": "eligible-existing",
                "labels": {"alertname": "TargetDown", "severity": "critical"},
                "annotations": {},
            }
        ],
        poll_time=datetime(2026, 7, 18, 2, 0, tzinfo=timezone.utc),
    )
    policy = create_policy_draft(
        session,
        name="explicit catch all",
        priority=100,
        matchers=[],
        repeat_interval_seconds=0,
        channel_ids=[channel.id],
    )
    session.flush()
    token_service = ActivationTokenService(key=b"z" * 32)
    prepared = prepare_activation(
        session,
        policy.id,
        expected_version=1,
        notify_existing=True,
        source_id=active_source_id(),
        token_service=token_service,
    )
    assert prepared.eligible_incident_count == 1
    confirm_activation(
        session,
        policy.id,
        expected_version=1,
        notify_existing=True,
        token=prepared.token,
        source_id=active_source_id(),
        token_service=token_service,
        observed_at=datetime(2026, 7, 18, 2, 1, tzinfo=timezone.utc),
    )
    session.commit()
    assert len(session.exec(select(NotificationRoute)).all()) == 1
