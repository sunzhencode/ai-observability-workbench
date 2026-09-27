"""Deterministic Incident routing policies and draft revision services."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlmodel import Session, select

from app.crypto import redact_sensitive
from app.models import (
    ConfigAudit,
    Incident,
    NotificationChannel,
    NotificationChannelRevision,
    NotificationPolicyChannel,
    NotificationPolicyRevision,
)
from app.services.source_scope import (
    notification_policy_scope,
    replace_notification_policy_sources,
    scope_contains,
    validate_source_scope,
)

ALLOWED_OPERATORS = {"=", "!=", "=~", "!~"}
RESERVED_FIELDS = {"aggregation_rule_id", "severity"}
GROUP_FIELD_RE = re.compile(r"^group\.[A-Za-z_][A-Za-z0-9_.-]{0,126}$")


@dataclass(frozen=True)
class PolicyCandidate:
    id: int
    logical_id: str
    name: str
    priority: int
    matchers: list[dict[str, str]]
    repeat_interval_seconds: int = 14_400
    channel_ids: list[int] = field(default_factory=list)
    version: int = 1
    scope_mode: str = "ALL"
    source_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class PolicyPreviewSample:
    incident_id: int
    context: dict[str, Any]
    winner_policy_id: int | None
    winner_policy_name: str | None
    channel_ids: list[int]


@dataclass(frozen=True)
class PolicyPreviewResult:
    direct_match_count: int
    shadowed_count: int
    final_match_count: int
    firing_match_count: int
    unrouted_count: int
    disabled_channel_count: int
    samples: list[PolicyPreviewSample]


def validate_policy_matchers(
    matchers: list[dict[str, str]],
) -> list[dict[str, str]]:
    if len(matchers) > 20:
        raise ValueError("policy supports at most 20 matchers")
    normalized: list[dict[str, str]] = []
    for raw in matchers:
        field_name = str(raw.get("field") or "").strip()
        operator = str(raw.get("operator") or "").strip()
        value = str(raw.get("value") or "")
        if field_name not in RESERVED_FIELDS and not GROUP_FIELD_RE.fullmatch(field_name):
            raise ValueError("matcher field is not a stable Incident route field")
        if operator not in ALLOWED_OPERATORS:
            raise ValueError("matcher operator must be =, !=, =~, or !~")
        if len(value) > 512:
            raise ValueError("matcher value is too long")
        if operator in {"=~", "!~"}:
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError("matcher regex is invalid") from exc
        normalized.append({"field": field_name, "operator": operator, "value": value})
    return normalized


def validate_repeat_interval(value: int) -> int:
    parsed = int(value)
    if parsed != 0 and parsed < 300:
        raise ValueError("repeat interval must be 0 or at least 300 seconds")
    if parsed > 2_592_000:
        raise ValueError("repeat interval is too large")
    return parsed


def incident_route_context(incident: Incident) -> dict[str, Any]:
    return {
        "aggregation_rule_id": (
            str(incident.aggregation_rule_id)
            if incident.aggregation_rule_id is not None
            else ""
        ),
        "source_id": incident.source_id or "",
        "severity": incident.severity or "",
        "group": {str(key): str(value) for key, value in incident.group_labels.items()},
    }


def _context_value(context: dict[str, Any], field_name: str) -> str:
    if field_name.startswith("group."):
        group = context.get("group")
        return str(group.get(field_name[6:], "")) if isinstance(group, dict) else ""
    return str(context.get(field_name, ""))


def matcher_matches(context: dict[str, Any], matcher: dict[str, str]) -> bool:
    actual = _context_value(context, matcher["field"])
    expected = matcher["value"]
    operator = matcher["operator"]
    if operator == "=":
        return actual == expected
    if operator == "!=":
        return actual != expected
    matched = re.fullmatch(expected, actual) is not None
    return matched if operator == "=~" else not matched


def policy_matches(context: dict[str, Any], policy: PolicyCandidate) -> bool:
    if not scope_contains(
        policy.scope_mode,
        policy.source_ids,
        str(context.get("source_id") or ""),
    ):
        return False
    return all(matcher_matches(context, matcher) for matcher in policy.matchers)


def choose_policy(
    context: dict[str, Any], policies: list[PolicyCandidate]
) -> PolicyCandidate | None:
    ordered = sorted(policies, key=lambda item: (item.priority, item.id))
    return next((item for item in ordered if policy_matches(context, item)), None)


def preview_policy(
    incidents: list[Incident],
    *,
    candidate: PolicyCandidate,
    active: list[PolicyCandidate],
    disabled_channel_ids: set[int] | None = None,
) -> PolicyPreviewResult:
    disabled_channel_ids = disabled_channel_ids or set()
    active_without_same = [
        item for item in active if item.logical_id != candidate.logical_id
    ]
    combined = active_without_same + [candidate]
    direct = shadowed = final = firing = unrouted = 0
    samples: list[PolicyPreviewSample] = []
    for incident in incidents:
        context = incident_route_context(incident)
        direct_match = policy_matches(context, candidate)
        winner = choose_policy(context, combined)
        if direct_match:
            direct += 1
        if direct_match and (winner is None or winner.id != candidate.id):
            shadowed += 1
        if winner is not None and winner.id == candidate.id:
            final += 1
            if incident.source_state == "firing":
                firing += 1
        if winner is None:
            unrouted += 1
        if len(samples) < 10 and (direct_match or winner is None):
            samples.append(
                PolicyPreviewSample(
                    incident_id=int(incident.id or 0),
                    context=context,
                    winner_policy_id=winner.id if winner else None,
                    winner_policy_name=winner.name if winner else None,
                    channel_ids=list(winner.channel_ids) if winner else [],
                )
            )
    return PolicyPreviewResult(
        direct_match_count=direct,
        shadowed_count=shadowed,
        final_match_count=final,
        firing_match_count=firing,
        unrouted_count=unrouted,
        disabled_channel_count=len(
            set(candidate.channel_ids).intersection(disabled_channel_ids)
        ),
        samples=samples,
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _validate_name_priority(name: str, priority: int) -> tuple[str, int]:
    clean_name = str(name or "").strip()
    if not clean_name or len(clean_name) > 120:
        raise ValueError("policy name must contain 1-120 characters")
    parsed_priority = int(priority)
    if not -1_000_000 <= parsed_priority <= 1_000_000:
        raise ValueError("policy priority is outside its allowed range")
    return clean_name, parsed_priority


def _validate_channel_ids(session: Session, channel_ids: list[int]) -> list[int]:
    ordered: list[int] = []
    seen: set[int] = set()
    for raw in channel_ids:
        channel_id = int(raw)
        if channel_id in seen:
            continue
        if session.get(NotificationChannel, channel_id) is None:
            raise ValueError("notification channel does not exist")
        ordered.append(channel_id)
        seen.add(channel_id)
    if not ordered:
        raise ValueError("policy requires at least one channel")
    if len(ordered) > 20:
        raise ValueError("policy supports at most 20 channels")
    return ordered


def _replace_bindings(
    session: Session, revision: NotificationPolicyRevision, channel_ids: list[int]
) -> None:
    if revision.id is None:
        session.flush()
    old = session.exec(
        select(NotificationPolicyChannel).where(
            NotificationPolicyChannel.policy_revision_id == revision.id
        )
    ).all()
    for item in old:
        session.delete(item)
    session.flush()
    for order, channel_id in enumerate(channel_ids):
        session.add(
            NotificationPolicyChannel(
                policy_revision_id=revision.id,
                channel_id=channel_id,
                order=order,
            )
        )
    session.flush()


def _audit(
    session: Session,
    revision: NotificationPolicyRevision,
    action: str,
    result: str,
    changes: dict[str, Any] | None = None,
) -> None:
    session.add(
        ConfigAudit(
            resource_type="NOTIFICATION_POLICY",
            resource_id=revision.logical_id,
            action=action,
            result=result,
            redacted_diff_json=redact_sensitive(changes or {}),
        )
    )


def create_policy_draft(
    session: Session,
    *,
    name: str,
    priority: int,
    matchers: list[dict[str, str]],
    repeat_interval_seconds: int,
    channel_ids: list[int],
    source_scope: dict | None = None,
) -> NotificationPolicyRevision:
    clean_name, parsed_priority = _validate_name_priority(name, priority)
    scope_mode, source_ids = validate_source_scope(session, source_scope)
    revision = NotificationPolicyRevision(
        logical_id=str(uuid4()),
        version=1,
        name=clean_name,
        state="DRAFT",
        priority=parsed_priority,
        matchers=validate_policy_matchers(matchers),
        scope_mode=scope_mode,
        repeat_interval_seconds=validate_repeat_interval(repeat_interval_seconds),
    )
    session.add(revision)
    session.flush()
    session.refresh(revision)
    ids = _validate_channel_ids(session, channel_ids)
    _replace_bindings(session, revision, ids)
    replace_notification_policy_sources(
        session, int(revision.id), scope_mode, source_ids
    )
    _audit(session, revision, "CREATE_DRAFT", "SUCCESS", {"channel_ids": ids})
    session.flush()
    return revision


def update_policy_draft(
    session: Session,
    logical_id: str,
    *,
    expected_version: int,
    name: str,
    priority: int,
    matchers: list[dict[str, str]],
    repeat_interval_seconds: int,
    channel_ids: list[int],
    source_scope: dict | None = None,
) -> NotificationPolicyRevision:
    revisions = session.exec(
        select(NotificationPolicyRevision)
        .where(NotificationPolicyRevision.logical_id == logical_id)
        .order_by(NotificationPolicyRevision.version.desc())
    ).all()
    if not revisions:
        raise LookupError("policy not found")
    current = revisions[0]
    if current.version != expected_version:
        raise FileExistsError("policy revision conflict")
    draft = next((item for item in revisions if item.state == "DRAFT"), None)
    clean_name, parsed_priority = _validate_name_priority(name, priority)
    normalized_matchers = validate_policy_matchers(matchers)
    repeat = validate_repeat_interval(repeat_interval_seconds)
    scope_mode, source_ids = validate_source_scope(session, source_scope)
    if draft is None:
        draft = NotificationPolicyRevision(
            logical_id=logical_id,
            version=current.version + 1,
            name=clean_name,
            state="DRAFT",
            priority=parsed_priority,
            matchers=normalized_matchers,
            scope_mode=scope_mode,
            repeat_interval_seconds=repeat,
        )
    else:
        draft.name = clean_name
        draft.priority = parsed_priority
        draft.matchers = normalized_matchers
        draft.scope_mode = scope_mode
        draft.repeat_interval_seconds = repeat
        draft.updated_at = _now()
    session.add(draft)
    session.flush()
    session.refresh(draft)
    ids = _validate_channel_ids(session, channel_ids)
    _replace_bindings(session, draft, ids)
    replace_notification_policy_sources(
        session, int(draft.id), scope_mode, source_ids
    )
    _audit(session, draft, "UPDATE_DRAFT", "SUCCESS", {"channel_ids": ids})
    session.flush()
    return draft


def policy_channel_ids(
    session: Session, revision_id: int | None
) -> list[int]:
    return [
        item.channel_id
        for item in session.exec(
            select(NotificationPolicyChannel)
            .where(NotificationPolicyChannel.policy_revision_id == revision_id)
            .order_by(NotificationPolicyChannel.order, NotificationPolicyChannel.id)
        ).all()
    ]


def policy_candidate(
    session: Session, revision: NotificationPolicyRevision
) -> PolicyCandidate:
    if revision.id is None:
        raise RuntimeError("persisted policy has no id")
    return PolicyCandidate(
        id=revision.id,
        logical_id=revision.logical_id,
        name=revision.name,
        priority=revision.priority,
        matchers=list(revision.matchers),
        repeat_interval_seconds=revision.repeat_interval_seconds,
        channel_ids=policy_channel_ids(session, revision.id),
        version=revision.version,
        scope_mode=revision.scope_mode,
        source_ids=tuple(
            notification_policy_scope(
                session, revision.id, revision.scope_mode
            )["source_ids"]
        ),
    )


def active_policy_candidates(session: Session) -> list[PolicyCandidate]:
    revisions = session.exec(
        select(NotificationPolicyRevision).where(
            NotificationPolicyRevision.state == "ACTIVE"
        )
    ).all()
    return sorted(
        [policy_candidate(session, item) for item in revisions],
        key=lambda item: (item.priority, item.id),
    )


def activate_policy(
    session: Session,
    revision_id: int | None,
    *,
    expected_version: int,
) -> NotificationPolicyRevision:
    revision = session.get(NotificationPolicyRevision, revision_id)
    if revision is None or revision.state != "DRAFT":
        raise LookupError("draft policy revision not found")
    if revision.version != expected_version:
        raise FileExistsError("policy revision conflict")
    channel_ids = policy_channel_ids(session, revision.id)
    for channel_id in channel_ids:
        channel = session.get(NotificationChannel, channel_id)
        if (
            channel is None
            or channel.state != "ENABLED"
            or channel.active_revision_id is None
        ):
            raise ValueError("all policy channels must be enabled and active")
        active_revision = session.get(
            NotificationChannelRevision, channel.active_revision_id
        )
        if active_revision is None or active_revision.state != "ACTIVE":
            raise ValueError("all policy channels must have an active revision")
    old = session.exec(
        select(NotificationPolicyRevision).where(
            NotificationPolicyRevision.logical_id == revision.logical_id,
            NotificationPolicyRevision.state == "ACTIVE",
        )
    ).all()
    for item in old:
        item.state = "RETIRED"
        item.updated_at = _now()
        session.add(item)
    session.flush()
    revision.state = "ACTIVE"
    revision.activated_at = _now()
    revision.updated_at = _now()
    session.add(revision)
    _audit(session, revision, "ACTIVATE", "SUCCESS", {"version": revision.version})
    session.flush()
    return revision


def disable_policy(
    session: Session, revision_id: int | None
) -> NotificationPolicyRevision:
    revision = session.get(NotificationPolicyRevision, revision_id)
    if revision is None or revision.state != "ACTIVE":
        raise LookupError("active policy revision not found")
    revision.state = "DISABLED"
    revision.updated_at = _now()
    session.add(revision)
    _audit(session, revision, "DISABLE", "SUCCESS")
    session.flush()
    return revision


def policy_public_dict(
    session: Session, revision: NotificationPolicyRevision
) -> dict[str, Any]:
    return {
        "id": revision.id,
        "logical_id": revision.logical_id,
        "version": revision.version,
        "name": revision.name,
        "state": revision.state,
        "priority": revision.priority,
        "matchers": list(revision.matchers),
        "repeat_interval_seconds": revision.repeat_interval_seconds,
        "channel_ids": policy_channel_ids(session, revision.id),
        "source_scope": notification_policy_scope(
            session, revision.id, revision.scope_mode
        ),
        "created_at": revision.created_at,
        "updated_at": revision.updated_at,
        "activated_at": revision.activated_at,
    }
