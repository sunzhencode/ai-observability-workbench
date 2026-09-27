"""F20 source-scope helpers shared by rules, policies, and queries."""

from __future__ import annotations

from typing import Iterable, Literal

from sqlalchemy import delete, inspect
from sqlmodel import Session, select

from app.registry_models import (
    AggregationRuleSource,
    EventSource,
    NotificationPolicySource,
)

ScopeMode = Literal["ALL", "SELECTED"]


def f20_table_available(session: Session, table_name: str) -> bool:
    return bool(inspect(session.connection()).has_table(table_name))


def has_event_sources(session: Session) -> bool:
    """Whether the managed registry is in use at all.

    Archived rows do not count. Migration 5 leaves ARCHIVED placeholders for
    pre-F20 `.env` config, and counting them made this return True on a
    workbench whose only real source was the `.env` fallback -- which then
    resolved to an empty visible set and hid every incident. See
    docs/superpowers/specs/archive/HISTORY.md §3.3.
    """
    if not f20_table_available(session, "eventsource"):
        return False
    return (
        session.exec(
            select(EventSource.id).where(EventSource.lifecycle_state != "ARCHIVED")
        ).first()
        is not None
    )


def non_enabled_source_ids(session: Session) -> set[str]:
    """Sources that must not look fresh: disabled or archived.

    Freshness is evidence about a source, so anything creating an Incident
    outside a live poll has to ask. The model default is FRESH, which is right
    for ingest and wrong for a regroup that only reshuffles alerts we already
    had.
    """
    if not f20_table_available(session, "eventsource"):
        return set()
    return {
        item
        for item in session.exec(
            select(EventSource.id).where(EventSource.lifecycle_state != "ENABLED")
        ).all()
    }


def source_name_map(session: Session) -> dict[str, str]:
    if not f20_table_available(session, "eventsource"):
        return {}
    return {
        item.id: item.name
        for item in session.exec(select(EventSource).order_by(EventSource.name)).all()
    }


def normalize_requested_source_ids(values: Iterable[str] | str | None) -> list[str]:
    if values is None:
        return []
    raw_values = [values] if isinstance(values, str) else list(values)
    result: list[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        for part in str(raw or "").split(","):
            source_id = part.strip()
            if source_id and source_id not in seen:
                result.append(source_id)
                seen.add(source_id)
    return result



def stored_source_ids(session: Session) -> list[str]:
    """Distinct source ids actually present in the data.

    Only used for pre-F20 databases, where there is no registry to filter by.
    """
    from app.models import Incident

    return sorted(
        {
            str(row)
            for row in session.exec(select(Incident.source_id)).all()
            if row is not None and str(row).strip()
        }
    )


def visible_source_ids(
    session: Session,
    *,
    requested: Iterable[str] | str | None = None,
    include_archived: bool = False,
) -> list[str]:
    requested_ids = normalize_requested_source_ids(requested)
    if not f20_table_available(session, "eventsource"):
        # No registry tables at all means a database that predates F20. Filtering
        # by a registry that cannot exist would hide every row, so show what is
        # stored instead.
        return requested_ids or stored_source_ids(session)
    if not has_event_sources(session):
        # F21: the tables exist but nothing is configured, so nothing is visible.
        # There is no `.env` source to fall back to and inventing one would
        # resurrect the dual path. Rows left by the retired legacy path are
        # repointed by migration v6 or age out with retention. See ADR 0004.
        return []
    sources = {
        item.id: item
        for item in session.exec(
            select(EventSource).order_by(EventSource.name, EventSource.id)
        ).all()
    }
    if requested_ids:
        candidates = [source_id for source_id in requested_ids if source_id in sources]
    else:
        candidates = [
            source.id
            for source in sources.values()
            if source.lifecycle_state == "ENABLED"
        ]
    if not include_archived:
        candidates = [
            source_id
            for source_id in candidates
            if sources[source_id].lifecycle_state != "ARCHIVED"
        ]
    return candidates


def validate_source_scope(
    session: Session,
    raw_scope: dict | None,
) -> tuple[str, list[str]]:
    raw_scope = dict(raw_scope or {})
    mode = str(raw_scope.get("mode") or "ALL").upper()
    if mode not in {"ALL", "SELECTED"}:
        raise ValueError("source scope mode must be ALL or SELECTED")
    source_ids = normalize_requested_source_ids(raw_scope.get("source_ids") or [])
    if mode == "ALL":
        return "ALL", []
    if not source_ids:
        raise ValueError("SELECTED source scope requires at least one source")
    if not has_event_sources(session):
        raise ValueError("source scope requires EventSource registry")
    known = set(source_name_map(session))
    missing = [source_id for source_id in source_ids if source_id not in known]
    if missing:
        raise ValueError("source scope references unknown EventSource")
    return "SELECTED", source_ids


def source_scope_dict(mode: str, source_ids: Iterable[str]) -> dict:
    normalized_mode = "SELECTED" if mode == "SELECTED" else "ALL"
    ids = normalize_requested_source_ids(source_ids)
    return {
        "mode": normalized_mode,
        "source_ids": ids if normalized_mode == "SELECTED" else [],
    }


def scope_contains(mode: str, source_ids: Iterable[str], source_id: str) -> bool:
    if mode != "SELECTED":
        return True
    return source_id in set(source_ids)


def _selected_rule_sources(session: Session, rule_id: int | None) -> list[str]:
    if rule_id is None or not f20_table_available(session, "aggregationrulesource"):
        return []
    return [
        item.source_id
        for item in session.exec(
            select(AggregationRuleSource)
            .where(AggregationRuleSource.rule_id == rule_id)
            .order_by(AggregationRuleSource.source_id)
        ).all()
    ]


def _selected_policy_sources(session: Session, revision_id: int | None) -> list[str]:
    if revision_id is None or not f20_table_available(
        session, "notificationpolicysource"
    ):
        return []
    return [
        item.source_id
        for item in session.exec(
            select(NotificationPolicySource)
            .where(NotificationPolicySource.policy_revision_id == revision_id)
            .order_by(NotificationPolicySource.source_id)
        ).all()
    ]


def aggregation_rule_scope(session: Session, rule_id: int | None, mode: str) -> dict:
    return source_scope_dict(mode, _selected_rule_sources(session, rule_id))


def notification_policy_scope(
    session: Session, revision_id: int | None, mode: str
) -> dict:
    return source_scope_dict(mode, _selected_policy_sources(session, revision_id))


def aggregation_scope_map(
    session: Session, rule_ids: Iterable[int | None]
) -> dict[int, tuple[str, ...]]:
    """Selected source ids per rule. The mode stays on the rule row.

    This used to also return a mode, hardcoded to "SELECTED" for every entry --
    both callers discarded it and read `rule.scope_mode` instead, which is the
    only place the mode is actually stored. Anyone who had trusted the returned
    mode would have made every ALL-scoped rule match nothing.
    """
    if not f20_table_available(session, "aggregationrulesource"):
        return {}
    result: dict[int, tuple[str, ...]] = {}
    for raw_id in rule_ids:
        if raw_id is None:
            continue
        result[int(raw_id)] = tuple(_selected_rule_sources(session, int(raw_id)))
    return result


def replace_aggregation_rule_sources(
    session: Session, rule_id: int, mode: str, source_ids: Iterable[str]
) -> None:
    if not f20_table_available(session, "aggregationrulesource"):
        if mode == "SELECTED":
            raise ValueError("source scope requires EventSource registry")
        return
    session.exec(
        delete(AggregationRuleSource).where(AggregationRuleSource.rule_id == rule_id)
    )
    session.flush()
    if mode == "SELECTED":
        for source_id in normalize_requested_source_ids(source_ids):
            session.add(AggregationRuleSource(rule_id=rule_id, source_id=source_id))
    session.flush()


def replace_notification_policy_sources(
    session: Session, revision_id: int, mode: str, source_ids: Iterable[str]
) -> None:
    if not f20_table_available(session, "notificationpolicysource"):
        if mode == "SELECTED":
            raise ValueError("source scope requires EventSource registry")
        return
    session.exec(
        delete(NotificationPolicySource).where(
            NotificationPolicySource.policy_revision_id == revision_id
        )
    )
    session.flush()
    if mode == "SELECTED":
        for source_id in normalize_requested_source_ids(source_ids):
            session.add(
                NotificationPolicySource(
                    policy_revision_id=revision_id,
                    source_id=source_id,
                )
            )
    session.flush()
