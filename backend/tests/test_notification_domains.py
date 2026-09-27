"""Deterministic notification policy, payload and timing contracts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from dataclasses import replace

import pytest

from app.domains.notifications.models import (
    DeliveryEvent,
    IncidentNotificationFact,
    Matcher,
    MatcherOperator,
    NotificationChange,
    PolicyCandidate,
    choose_policy,
    delivery_event_key,
    next_rate_limit_permit,
    response_notification_policy,
    retry_delay_seconds,
    should_create_route,
    validate_channel_configuration,
    validate_replacement_secret,
)

UTC = timezone.utc


def _fact() -> IncidentNotificationFact:
    return IncidentNotificationFact(
        4,
        2,
        "source-a",
        "Primary",
        "CPU high",
        "warning",
        "FIRING",
        "FRESH",
        7,
        {"cluster": "prod"},
        8,
        tuple(
            {
                "alertname": f"CPUHigh-{index}",
                "severity": "warning",
                "summary": "safe",
                "token": "must-not-leak",
            }
            for index in range(8)
        ),
    )


def _policy(revision_id: int, priority: int, value: str) -> PolicyCandidate:
    return PolicyCandidate(
        revision_id,
        f"policy-{revision_id}",
        f"Policy {revision_id}",
        1,
        priority,
        (Matcher("group.cluster", MatcherOperator.EQUAL, value),),
        "SELECTED",
        ("source-a",),
        ("channel-a",),
        14_400,
    )


def test_first_match_uses_priority_then_revision_and_scope() -> None:
    fact = _fact()
    winner = choose_policy(fact, (_policy(8, 20, "prod"), _policy(7, 20, "prod")))
    assert winner is not None and winner.revision_id == 7
    assert choose_policy(fact, (_policy(1, 1, "staging"),)) is None
    with pytest.raises(ValueError, match="NOTIFICATION_MATCHER_FIELD_INVALID"):
        Matcher("title", MatcherOperator.EQUAL, "CPU high")


def test_route_gate_and_event_key_are_occurrence_and_version_bound() -> None:
    now = datetime(2026, 8, 13, tzinfo=UTC)
    change = NotificationChange(None, "FIRING", None, "warning", None, "NEW", 3, "LIVE_POLL", now)
    assert should_create_route(_fact(), change) is True
    stale = replace(_fact(), freshness_state="STALE")
    assert should_create_route(stale, change) is False
    first = delivery_event_key(incident_id=4, route_id=3, target_id=2, event_type=DeliveryEvent.FIRING_OPENED, change_version=3)
    second = delivery_event_key(incident_id=4, route_id=3, target_id=2, event_type=DeliveryEvent.FIRING_OPENED, change_version=4)
    assert first != second and first == delivery_event_key(incident_id=4, route_id=3, target_id=2, event_type=DeliveryEvent.FIRING_OPENED, change_version=3)


@pytest.mark.parametrize(
    ("response_state", "opened", "escalated", "reminder", "recovered", "terminated"),
    (
        ("UNACKNOWLEDGED", True, True, True, True, False),
        ("IN_PROGRESS", True, True, False, True, False),
        ("RESOLVED", False, False, False, False, True),
    ),
)
def test_response_to_notification_policy_is_one_closed_matrix(
    response_state: str,
    opened: bool,
    escalated: bool,
    reminder: bool,
    recovered: bool,
    terminated: bool,
) -> None:
    policy = response_notification_policy(response_state)

    assert policy.allows(DeliveryEvent.FIRING_OPENED) is opened
    assert policy.allows(DeliveryEvent.SEVERITY_ESCALATED) is escalated
    assert policy.allows(DeliveryEvent.REMINDER) is reminder
    assert policy.allows(DeliveryEvent.RECOVERED) is recovered
    assert policy.terminated is terminated


def test_unknown_response_state_fails_closed_for_notifications() -> None:
    policy = response_notification_policy("OLD_OR_UNKNOWN")

    assert policy.terminated is True
    assert all(policy.allows(event) is False for event in DeliveryEvent)


def test_payload_is_bounded_and_sensitive_member_keys_do_not_cross_outbound_boundary() -> None:
    snapshot = _fact().payload_snapshot(change_version=3)
    assert len(snapshot["members"]) == 5  # type: ignore[arg-type]
    assert snapshot["members_truncated"] == 3
    assert "must-not-leak" not in repr(snapshot)


def test_retry_jitter_is_stable_and_rate_limit_queues_without_attempt() -> None:
    assert retry_delay_seconds("event-a", 1) == retry_delay_seconds("event-a", 1)
    assert 60 <= retry_delay_seconds("event-a", 1) < 66
    now = datetime(2026, 8, 13, tzinfo=UTC)
    four = tuple(now - timedelta(milliseconds=100 * index) for index in range(4))
    assert next_rate_limit_permit(four, now=now) > now
    ninety = tuple(now - timedelta(milliseconds=500 * index) for index in range(90))
    assert next_rate_limit_permit(ninety, now=now) > now


def test_all_three_provider_config_shapes_are_explicit_and_bounded() -> None:
    assert validate_channel_configuration(
        "FEISHU_CUSTOM_BOT", {"mention_mode": "USERS", "mention_users": [{"open_id": "ou_test"}]}
    )["mention_mode"] == "USERS"
    assert validate_channel_configuration(
        "SMTP",
        {
            "host": "smtp.example.invalid",
            "port": 587,
            "tls_mode": "STARTTLS",
            "from_addr": "alerts@example.invalid",
            "to_addrs": ["ops@example.invalid"],
        },
    )["port"] == 587
    assert validate_channel_configuration(
        "GENERIC_WEBHOOK", {"url": "https://hooks.example.invalid/notify"}
    )["timeout_seconds"] == 8.0
    with pytest.raises(ValueError, match="NOTIFICATION_SMTP_TARGET_INVALID"):
        validate_channel_configuration(
            "SMTP",
            {
                "host": "smtp.example.invalid",
                "port": 25,
                "tls_mode": "STARTTLS",
                "from_addr": "alerts@example.invalid",
                "to_addrs": ["ops@example.invalid"],
            },
        )
    with pytest.raises(ValueError, match="NOTIFICATION_POLICY_SCOPE_IDS_FORBIDDEN"):
        replace(_policy(1, 10, "prod"), scope_mode="ALL")


def test_secret_shapes_are_rejected_before_encryption_or_channel_test() -> None:
    validate_replacement_secret(
        "FEISHU_CUSTOM_BOT",
        "webhook",
        "https://open.feishu.cn/open-apis/bot/v2/hook/test-token",
    )
    validate_replacement_secret(
        "GENERIC_WEBHOOK", "headers", '{"Authorization":"Bearer redacted"}'
    )
    with pytest.raises(ValueError, match="NOTIFICATION_FEISHU_WEBHOOK_INVALID"):
        validate_replacement_secret(
            "FEISHU_CUSTOM_BOT", "webhook", "https://example.invalid/hook/token"
        )
    with pytest.raises(ValueError, match="NOTIFICATION_WEBHOOK_HEADERS_INVALID"):
        validate_replacement_secret(
            "GENERIC_WEBHOOK", "headers", '{"X-Test":"bad\\nvalue"}'
        )
