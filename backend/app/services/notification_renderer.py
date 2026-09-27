"""Rendering a notification, per channel kind.

`MessageContext` is the provider-neutral half: the facts a message carries,
extracted once from the delivery's already-redacted snapshot. Each kind renders
from it (F24). The Feishu card below is the first and, until 阶段 4/5, only one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from app.models import NotificationDelivery, NotificationRouteTarget
from app.providers.feishu import (
    MAX_REQUEST_BYTES,
    SIGNATURE_OVERHEAD_BYTES,
    is_valid_open_id,
)

# What a rendered card may occupy. The provider appends `timestamp` and `sign`
# on signed channels, so measuring against the raw 18 KB here would let a card
# pass rendering and then fail to send -- permanently, and only on the channels
# that sign. Reserving the difference makes "it rendered" mean "it can be sent"
# on every channel.
MAX_RENDERED_BYTES = MAX_REQUEST_BYTES - SIGNATURE_OVERHEAD_BYTES


class PayloadTooLargeError(ValueError):
    pass


EVENT_TITLES = {
    "FIRING_OPENED": "首次触发",
    "SEVERITY_ESCALATED": "严重升级",
    "REMINDER": "持续提醒",
    "RECOVERED": "已恢复",
}
EVENT_COLORS = {
    "FIRING_OPENED": "red",
    "SEVERITY_ESCALATED": "red",
    "REMINDER": "orange",
    "RECOVERED": "green",
}


def _safe_text(value: Any, maximum: int) -> str:
    return str(value or "").replace("\x00", "")[:maximum]


def _public_incident_url(base_url: str, incident_id: int) -> str | None:
    value = str(base_url or "").strip().rstrip("/")
    if not value:
        return None
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return None
    host = parts.hostname.lower()
    if host in {"localhost", "127.0.0.1", "::1"}:
        return None
    return f"{value}/incidents/{incident_id}"


def _mentions(target: NotificationRouteTarget, event_type: str) -> str:
    if not target.mention_on.get(event_type, False):
        return ""
    if target.mention_mode == "ALL":
        return "<at id=all></at>"
    if target.mention_mode == "USERS":
        # `_safe_text` bounds the length but not the character set, and the id
        # goes straight into a lark_md tag. Rows saved before the format was
        # pinned can still carry a value that closes the tag, so drop anything
        # that is not a well-formed open_id rather than rendering it.
        return " ".join(
            f'<at id={item["open_id"]}></at>'
            for item in target.mention_users
            if is_valid_open_id(item.get("open_id"))
        )
    return ""


@dataclass(frozen=True)
class MessageContext:
    """The facts a notification carries, with no provider in sight.

    Built once per send from the delivery's payload snapshot, which is already
    redacted -- credentials, ciphertext and remote response bodies never reach
    it. Every renderer works from this, so a new channel kind cannot accidentally
    read something the Feishu card was careful not to.
    """

    event_type: str
    severity: str
    title: str
    source: str
    source_state: str
    occurrence_no: int
    member_count: int
    members: list[dict[str, Any]]
    group_labels: dict[str, str]
    incident_id: int
    workbench_url: str

    @property
    def event_title(self) -> str:
        return EVENT_TITLES.get(self.event_type, self.event_type)

    @property
    def incident_url(self) -> str | None:
        return _public_incident_url(self.workbench_url, self.incident_id)


def build_message_context(
    delivery: NotificationDelivery,
    *,
    workbench_url: str = "",
) -> MessageContext:
    snapshot = dict(delivery.payload_snapshot_json)
    members = snapshot.get("members")
    labels = snapshot.get("group_labels")
    return MessageContext(
        event_type=delivery.event_type,
        severity=_safe_text(snapshot.get("severity"), 32).upper() or "UNKNOWN",
        title=_safe_text(snapshot.get("title"), 320),
        source=_safe_text(snapshot.get("source_name") or snapshot.get("source_id"), 120),
        source_state=_safe_text(snapshot.get("source_state"), 32),
        occurrence_no=int(snapshot.get("occurrence_no") or 1),
        member_count=int(
            snapshot.get("member_count") or (len(members) if isinstance(members, list) else 0)
        ),
        members=[item for item in (members or []) if isinstance(item, dict)],
        group_labels=dict(labels) if isinstance(labels, dict) else {},
        incident_id=int(delivery.incident_id),
        workbench_url=workbench_url,
    )



def _card_content(
    delivery: NotificationDelivery,
    target: NotificationRouteTarget,
    *,
    required_keyword: str | None,
    workbench_url: str,
) -> dict[str, Any]:
    snapshot = dict(delivery.payload_snapshot_json)
    severity = _safe_text(snapshot.get("severity"), 32).upper() or "UNKNOWN"
    event_title = EVENT_TITLES.get(delivery.event_type, delivery.event_type)
    keyword = _safe_text(required_keyword, 64)
    title_prefix = f"{keyword} · " if keyword else ""
    title = f"{title_prefix}[{severity} · {event_title}] {_safe_text(snapshot.get('title'), 320)}"
    lines = [
        f"**状态**: {_safe_text(snapshot.get('source_state'), 32)}  "
        f"**Occurrence**: #{int(snapshot.get('occurrence_no') or 1)}",
        f"**来源**: {_safe_text(snapshot.get('source_name') or snapshot.get('source_id'), 120)}",
    ]
    group_labels = snapshot.get("group_labels")
    if isinstance(group_labels, dict) and group_labels:
        rendered = " · ".join(
            f"{_safe_text(key, 128)}={_safe_text(value, 180)}"
            for key, value in sorted(group_labels.items())
        )
        lines.append(f"**分组**: {rendered}")
    members = snapshot.get("members") if isinstance(snapshot.get("members"), list) else []
    lines.append(f"**成员**: {int(snapshot.get('member_count') or len(members))}")
    for index, member in enumerate(members[:5], 1):
        if not isinstance(member, dict):
            continue
        location = "/".join(
            item
            for item in (
                _safe_text(member.get("namespace"), 120),
                _safe_text(member.get("service") or member.get("job"), 120),
            )
            if item
        )
        line = (
            f"{index}. {_safe_text(member.get('alertname'), 160)} · "
            f"{_safe_text(member.get('severity'), 32)}"
        )
        if location:
            line += f" · {location}"
        summary = _safe_text(member.get("summary"), 240)
        if summary:
            line += f"\n   {summary}"
        lines.append(line)
    truncated = int(snapshot.get("members_truncated") or 0)
    if truncated:
        lines.append(f"其余 {truncated} 条已折叠")
    mention = _mentions(target, delivery.event_type)
    if mention:
        lines.append(mention)
    incident_url = _public_incident_url(
        workbench_url, int(snapshot.get("incident_id") or delivery.incident_id)
    )
    if incident_url:
        lines.append(f"[查看事件]({incident_url})")
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "template": EVENT_COLORS.get(delivery.event_type, "blue"),
                "title": {"tag": "plain_text", "content": title},
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": "\n".join(lines)},
                }
            ],
        },
    }


def render_feishu_payload(
    delivery: NotificationDelivery,
    target: NotificationRouteTarget,
    *,
    required_keyword: str | None = None,
    workbench_url: str = "",
) -> dict[str, Any]:
    payload = _card_content(
        delivery,
        target,
        required_keyword=required_keyword,
        workbench_url=workbench_url,
    )
    encoded = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    if len(encoded) > MAX_RENDERED_BYTES:
        raise PayloadTooLargeError("rendered notification exceeds the card budget")
    return payload


def render_smtp_payload(context: MessageContext) -> dict[str, Any]:
    """A mail is a document, not a card: plain text plus an HTML alternative.

    The same facts as the Feishu card, in the shape a mail client expects. No
    "@ whom" -- SMTP has no such concept, which is exactly why it was the useful
    second kind to build.
    """

    subject = f"[{context.severity} · {context.event_title}] {context.title}"
    lines = [
        f"状态: {context.source_state}   Occurrence: #{context.occurrence_no}",
        f"来源: {context.source}",
    ]
    if context.group_labels:
        rendered = " · ".join(
            f"{_safe_text(key, 128)}={_safe_text(value, 180)}"
            for key, value in sorted(context.group_labels.items())
        )
        lines.append(f"分组: {rendered}")
    lines.append(f"成员: {context.member_count}")
    for index, member in enumerate(context.members[:5], 1):
        lines.append(
            f"  {index}. {_safe_text(member.get('alertname'), 160)} · "
            f"{_safe_text(member.get('severity'), 32)}"
        )
    if context.member_count > 5:
        lines.append(f"  … 另有 {context.member_count - 5} 条成员告警")
    if context.incident_url:
        lines.append("")
        lines.append(f"打开工作台: {context.incident_url}")

    text = "\n".join(lines)
    body = "".join(f"<p>{_html_escape(line)}</p>" for line in lines if line)
    return {
        "subject": subject[:320],
        "text": text,
        "html": f"<html><body><h3>{_html_escape(subject)}</h3>{body}</body></html>",
    }


def _html_escape(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def render_webhook_payload(context: MessageContext) -> dict[str, Any]:
    """A stable JSON document for whatever the user pointed at.

    This is an outward-facing contract, not an internal shape: once someone has
    written a receiver against it, a renamed field breaks their integration.
    `schema_version` is how a change announces itself.
    """

    from app.providers.generic_webhook import PAYLOAD_SCHEMA_VERSION

    return {
        "schema_version": PAYLOAD_SCHEMA_VERSION,
        "event_type": context.event_type,
        "incident": {
            "id": context.incident_id,
            "title": context.title,
            "severity": context.severity,
            "source_state": context.source_state,
            "occurrence_no": context.occurrence_no,
            "group_labels": context.group_labels,
            "member_count": context.member_count,
            "members": [
                {
                    "alertname": _safe_text(member.get("alertname"), 160),
                    "severity": _safe_text(member.get("severity"), 32),
                    "namespace": _safe_text(member.get("namespace"), 120),
                    "service": _safe_text(member.get("service") or member.get("job"), 120),
                }
                for member in context.members[:5]
            ],
        },
        "source": context.source,
        "workbench_url": context.incident_url,
    }
