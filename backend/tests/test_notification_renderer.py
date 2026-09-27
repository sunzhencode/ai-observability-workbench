"""Safe self-contained card rendering tests."""

from __future__ import annotations

import json

from app.models import NotificationDelivery, NotificationRouteTarget
from app.services.notification_renderer import render_feishu_payload


def _target() -> NotificationRouteTarget:
    return NotificationRouteTarget(
        id=1,
        route_id=1,
        channel_id=1,
        routed_channel_revision_id=1,
        channel_name="oncall",
        provider="FEISHU_CUSTOM_BOT",
        channel_version=1,
        mention_mode="USERS",
        mention_users=[{"open_id": "ou_user"}],
        mention_on={"FIRING_OPENED": True},
    )


def _delivery() -> NotificationDelivery:
    return NotificationDelivery(
        id=1,
        event_key="render-key",
        incident_id=7,
        route_id=1,
        route_target_id=1,
        event_type="FIRING_OPENED",
        incident_change_version=1,
        payload_snapshot_json={
            "incident_id": 7,
            "occurrence_no": 2,
            "title": "TargetDown · qa-a",
            "severity": "critical",
            "source_state": "firing",
            "environment": "qa",
            "group_labels": {"cluster": "qa-a", "namespace": "payments"},
            "member_count": 6,
            "members_truncated": 1,
            "members": [
                {
                    "alertname": "TargetDown",
                    "severity": "critical",
                    "namespace": "payments",
                    "service": "checkout",
                    "summary": "target unavailable",
                }
            ],
        },
    )


def test_card_contains_keyword_mentions_and_no_callback_or_localhost_link() -> None:
    payload = render_feishu_payload(
        _delivery(),
        _target(),
        required_keyword="Alert Workbench",
        workbench_url="http://127.0.0.1:5173",
    )
    rendered = json.dumps(payload, ensure_ascii=False)
    assert "Alert Workbench" in rendered
    assert "ou_user" in rendered
    assert "其余 1 条已折叠" in rendered
    assert "127.0.0.1" not in rendered
    assert "callback" not in rendered.lower()
    assert len(rendered.encode("utf-8")) < 18 * 1024


def test_recovered_does_not_mention_when_snapshot_switch_is_false() -> None:
    delivery = _delivery()
    delivery.event_type = "RECOVERED"
    rendered = json.dumps(
        render_feishu_payload(delivery, _target()), ensure_ascii=False
    )
    assert "ou_user" not in rendered


def test_malformed_stored_open_id_cannot_smuggle_an_at_all() -> None:
    """A row saved before the open_id format was pinned must not render.

    Validation now rejects these on save, but the database still holds whatever
    earlier releases accepted, and `<at id=...>` sits inside lark_md: a value
    ending the tag early turns "mention one person" into "mention everyone".
    """
    target = _target()
    target.mention_users = [
        {"open_id": "x></at><at id=all"},
        {"open_id": "ou_legit"},
    ]
    rendered = json.dumps(
        render_feishu_payload(_delivery(), target), ensure_ascii=False
    )
    assert "<at id=all></at>" not in rendered
    assert "ou_legit" in rendered


def test_render_budget_leaves_room_for_the_signature_fields() -> None:
    """Rendering must guarantee sendability on signed channels too.

    The provider appends `timestamp` and `sign` after rendering, so a card
    measured against the raw 18 KB could pass here and then fail permanently --
    but only for channels that sign.
    """
    from app.providers.feishu import MAX_REQUEST_BYTES, SIGNATURE_OVERHEAD_BYTES
    from app.services.notification_renderer import MAX_RENDERED_BYTES

    assert MAX_RENDERED_BYTES == MAX_REQUEST_BYTES - SIGNATURE_OVERHEAD_BYTES

    delivery = _delivery()
    payload = render_feishu_payload(delivery, _target())
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    body = dict(payload)
    body["timestamp"] = "1700000000"
    body["sign"] = "A" * 44
    signed = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    assert len(encoded.encode("utf-8")) <= MAX_RENDERED_BYTES
    assert len(signed.encode("utf-8")) <= MAX_REQUEST_BYTES


def test_message_context_carries_the_facts_without_a_provider() -> None:
    """F24: every renderer works from this, so it must hold what they all need."""

    from app.services.notification_renderer import build_message_context

    delivery = NotificationDelivery(
        route_id=1,
        route_target_id=1,
        incident_id=42,
        channel_id=1,
        event_type="FIRING_OPENED",
        event_key="k",
        idempotency_key="i",
        state="PENDING",
        payload_snapshot_json={
            "title": "TargetDown",
            "severity": "critical",
            "source_state": "firing",
            "source_name": "prod",
            "occurrence_no": 2,
            "member_count": 3,
            "members": [{"alertname": "TargetDown", "severity": "critical"}],
            "group_labels": {"cluster": "a"},
        },
    )

    context = build_message_context(delivery, workbench_url="https://wb.example")

    assert context.severity == "CRITICAL"
    assert context.title == "TargetDown"
    assert context.source == "prod"
    assert context.occurrence_no == 2
    assert context.member_count == 3
    assert context.group_labels == {"cluster": "a"}
    assert context.incident_id == 42
    assert context.event_title  # readable, not the raw enum
    assert context.incident_url == "https://wb.example/incidents/42"


def test_message_context_survives_a_snapshot_missing_everything() -> None:
    from app.services.notification_renderer import build_message_context

    delivery = NotificationDelivery(
        route_id=1,
        route_target_id=1,
        incident_id=7,
        channel_id=1,
        event_type="RECOVERED",
        event_key="k",
        idempotency_key="i",
        state="PENDING",
        payload_snapshot_json={},
    )

    context = build_message_context(delivery)

    assert context.severity == "UNKNOWN"
    assert context.members == []
    assert context.member_count == 0
    assert context.occurrence_no == 1
    assert context.incident_url is None
