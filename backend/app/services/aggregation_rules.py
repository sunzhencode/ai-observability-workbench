"""Standalone aggregation-rule validation, matching, preview, and publishing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from sqlmodel import Session, select

from app.models import (
    AggregationRule,
    Alert,
    Incident,
    IncidentAudit,
    NotificationRoute,
)
from app.services.grouping import max_severity
from app.services.group_keys import build_group_key_v2
from app.services.lifecycle import recompute_incident_lifecycle
from app.services.notification_planner import terminate_routes_for_regroup
from app.services.source_scope import (
    aggregation_scope_map,
    non_enabled_source_ids,
    replace_aggregation_rule_sources,
    scope_contains,
    source_name_map,
    validate_source_scope,
    visible_source_ids,
)

WATCHDOG_ALERTNAME = "Watchdog"
LABEL_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
MATCHER_OPERATORS = {"=", "!=", "=~", "!~"}
MAX_RULE_NAME_LENGTH = 128
MAX_LABEL_NAME_LENGTH = 128
MAX_MATCHERS = 16
MAX_GROUP_LABELS = 8
MAX_MATCHER_VALUE_LENGTH = 512
MAX_PRIORITY = 100_000


@dataclass(frozen=True)
class AggregationDecision:
    group_key: str
    title: str
    explanation: str
    missing_labels: list[str]
    group_labels: dict[str, str]
    rule_id: int | None
    rule_name: str | None


@dataclass(frozen=True)
class PreviewGroup:
    group_key: str
    title: str
    fingerprints: list[str]
    missing_labels: list[str]


@dataclass(frozen=True)
class AggregationRulePreview:
    matcher_alert_count: int
    selected_alert_count: int
    proposed_group_count: int
    groups: list[PreviewGroup]
    by_source: list[dict[str, Any]]


def _validate_label_name(value: Any, *, field: str) -> str:
    label = str(value or "").strip()
    if (
        not label
        or len(label) > MAX_LABEL_NAME_LENGTH
        or LABEL_NAME.fullmatch(label) is None
    ):
        raise ValueError(f"invalid {field} label name: {label or '<empty>'}")
    if label in {"environment", "source_id"}:
        raise ValueError(f"{label} is not a rule label; use Source Scope")
    return label


def validate_rule_definition(
    *,
    name: str,
    priority: int,
    matchers: list[dict[str, Any]],
    group_by_labels: list[str],
) -> tuple[str, int, list[dict[str, str]], list[str]]:
    rule_name = str(name or "").strip()
    if not rule_name or len(rule_name) > MAX_RULE_NAME_LENGTH:
        raise ValueError(f"rule name must contain 1-{MAX_RULE_NAME_LENGTH} characters")
    if isinstance(priority, bool) or not 0 <= int(priority) <= MAX_PRIORITY:
        raise ValueError(f"priority must be between 0 and {MAX_PRIORITY}")
    if len(matchers) > MAX_MATCHERS:
        raise ValueError(f"matchers may contain at most {MAX_MATCHERS} entries")
    normalized_matchers: list[dict[str, str]] = []
    for raw in matchers:
        label = _validate_label_name(raw.get("label"), field="matcher")
        operator = str(raw.get("operator") or "").strip()
        value = str(raw.get("value") or "").strip()
        if operator not in MATCHER_OPERATORS:
            raise ValueError(f"invalid matcher operator: {operator or '<empty>'}")
        if len(value) > MAX_MATCHER_VALUE_LENGTH:
            raise ValueError(
                f"matcher value may contain at most {MAX_MATCHER_VALUE_LENGTH} characters"
            )
        if operator in {"=~", "!~"}:
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError(f"invalid regex for {label}: {exc}") from exc
        normalized_matchers.append(
            {"label": label, "operator": operator, "value": value}
        )
    if len(group_by_labels) > MAX_GROUP_LABELS:
        raise ValueError(
            f"group_by_labels may contain at most {MAX_GROUP_LABELS} labels"
        )
    normalized_group_by = [
        _validate_label_name(value, field="group_by") for value in group_by_labels
    ]
    if len(normalized_group_by) != len(set(normalized_group_by)):
        raise ValueError("group_by_labels must not contain duplicate labels")
    return rule_name, int(priority), normalized_matchers, normalized_group_by


def alert_values(fields: dict[str, Any]) -> dict[str, str]:
    labels = fields.get("labels") if isinstance(fields.get("labels"), dict) else {}
    values = {
        str(key): str(value)
        for key, value in labels.items()
        if value is not None and str(key) not in {"environment", "source_id"}
    }
    for name in ("alertname", "severity", "cluster"):
        value = fields.get(name)
        if value is not None:
            values[name] = str(value)
    return values


def rule_matches(fields: dict[str, Any], matchers: list[dict[str, str]]) -> bool:
    values = alert_values(fields)
    for matcher in matchers:
        actual = values.get(matcher["label"], "")
        expected = matcher["value"]
        operator = matcher["operator"]
        if operator == "=" and actual != expected:
            return False
        if operator == "!=" and actual == expected:
            return False
        if operator == "=~" and re.fullmatch(expected, actual) is None:
            return False
        if operator == "!~" and re.fullmatch(expected, actual) is not None:
            return False
    return True


def choose_matching_rule(
    fields: dict[str, Any],
    rules: list[AggregationRule],
    source_scopes: dict[int, tuple[str, ...]] | None = None,
) -> AggregationRule | None:
    source_id = str(fields.get("source_id") or "legacy")
    for rule in sorted(rules, key=lambda item: (item.priority, item.id or 0)):
        scope_ids: tuple[str, ...] = ()
        if source_scopes is not None and rule.id is not None:
            scope_ids = source_scopes.get(int(rule.id), ())
        # The mode lives on the rule; the map only carries the selected ids.
        if not scope_contains(rule.scope_mode, scope_ids, source_id):
            continue
        if rule.enabled and rule_matches(fields, list(rule.matchers)):
            return rule
    return None


def _fields(alert: Alert) -> dict[str, Any]:
    return {
        "source_id": alert.source_id,
        "alertname": alert.alertname,
        "fingerprint": alert.upstream_fingerprint or alert.fingerprint,
        "labels": alert.labels,
        "severity": alert.severity,
        "cluster": alert.cluster,
    }


def compute_aggregation_group(
    fields: dict[str, Any], rule: AggregationRule | None
) -> AggregationDecision:
    source_id = str(fields.get("source_id") or "legacy")
    alertname = str(fields.get("alertname") or "<unnamed>")
    fingerprint = str(fields.get("fingerprint") or "<no-fingerprint>")
    values = alert_values(fields)
    if rule is None or rule.id is None:
        return AggregationDecision(
            group_key=build_group_key_v2(
                source_id=source_id,
                rule_id=None,
                isolation_fingerprint=fingerprint,
            ),
            title=alertname,
            explanation=(
                f"unmatched_rule; isolated by fingerprint={fingerprint}; "
                f"alertname={alertname}"
            ),
            missing_labels=[],
            group_labels={},
            rule_id=None,
            rule_name=None,
        )

    missing = [
        name
        for name in rule.group_by_labels
        if values.get(name) is None or not values[name].strip()
    ]
    if missing:
        return AggregationDecision(
            group_key=build_group_key_v2(
                source_id=source_id,
                rule_id=rule.id,
                isolation_fingerprint=fingerprint,
            ),
            title=rule.name,
            explanation=(
                f"rule_id={rule.id}; rule_name={rule.name}; "
                f"missing_labels={','.join(missing)}; "
                f"isolated by fingerprint={fingerprint}"
            ),
            missing_labels=missing,
            group_labels={
                name: values[name]
                for name in rule.group_by_labels
                if values.get(name) is not None and values[name].strip()
            },
            rule_id=rule.id,
            rule_name=rule.name,
        )

    grouped_values = [(name, values[name]) for name in rule.group_by_labels]
    rendered = " · ".join(value for _, value in grouped_values)
    return AggregationDecision(
        group_key=build_group_key_v2(
            source_id=source_id,
            rule_id=rule.id,
            ordered_group_values=grouped_values,
        ),
        title=f"{rule.name} · {rendered}" if rendered else rule.name,
        explanation=(
            f"rule_id={rule.id}; rule_name={rule.name}; rule_version={rule.version}; "
            f"grouped_by={','.join(rule.group_by_labels) or '<none>'}; "
            + ", ".join(f"{name}={value}" for name, value in grouped_values)
        ).rstrip("; "),
        missing_labels=[],
        group_labels=dict(grouped_values),
        rule_id=rule.id,
        rule_name=rule.name,
    )


def list_aggregation_rules(session: Session, *, enabled_only: bool = False) -> list[AggregationRule]:
    statement = select(AggregationRule)
    if enabled_only:
        statement = statement.where(AggregationRule.enabled == True)  # noqa: E712
    return list(
        session.exec(
            statement.order_by(AggregationRule.priority, AggregationRule.id)
        ).all()
    )


def _draft_rule(
    *,
    rule_id: int,
    name: str,
    priority: int,
    enabled: bool,
    matchers: list[dict[str, str]],
    group_by_labels: list[str],
    version: int,
    scope_mode: str = "ALL",
) -> AggregationRule:
    return AggregationRule(
        id=rule_id,
        name=name,
        priority=priority,
        enabled=enabled,
        matchers=matchers,
        group_by_labels=group_by_labels,
        scope_mode=scope_mode,
        version=version,
    )


def preview_aggregation_rule(
    session: Session,
    *,
    rule_id: int | None,
    name: str,
    priority: int,
    enabled: bool,
    matchers: list[dict[str, Any]],
    group_by_labels: list[str],
    source_scope: dict | None = None,
    source_id: str | None = None,
) -> AggregationRulePreview:
    rule_name, priority, matchers, group_by_labels = validate_rule_definition(
        name=name,
        priority=priority,
        matchers=matchers,
        group_by_labels=group_by_labels,
    )
    existing = list_aggregation_rules(session)
    current = session.get(AggregationRule, rule_id) if rule_id is not None else None
    scope_mode, scope_source_ids = validate_source_scope(session, source_scope)
    synthetic_id = rule_id or max((item.id or 0 for item in existing), default=0) + 1
    draft = _draft_rule(
        rule_id=synthetic_id,
        name=rule_name,
        priority=priority,
        enabled=enabled,
        matchers=matchers,
        group_by_labels=group_by_labels,
        version=(current.version + 1 if current is not None else 1),
        scope_mode=scope_mode,
    )
    candidates = [item for item in existing if item.id != rule_id]
    if enabled:
        candidates.append(draft)
    source_scopes = aggregation_scope_map(session, [item.id for item in candidates])
    if scope_mode == "SELECTED":
        source_scopes[synthetic_id] = tuple(scope_source_ids)
    source_ids = [source_id] if source_id is not None else (
        scope_source_ids
        if scope_mode == "SELECTED"
        else visible_source_ids(session, include_archived=False)
    )
    alerts = session.exec(
        select(Alert)
        .where(
            Alert.alertname != WATCHDOG_ALERTNAME,
            Alert.source_id.in_(source_ids),
        )
        .order_by(Alert.fingerprint)
    ).all()
    matcher_alert_count = sum(rule_matches(_fields(alert), matchers) for alert in alerts)
    selected = [
        alert
        for alert in alerts
        if (chosen := choose_matching_rule(_fields(alert), candidates, source_scopes)) is not None
        and chosen.id == draft.id
    ]
    grouped: dict[str, dict[str, Any]] = {}
    for alert in selected:
        decision = compute_aggregation_group(_fields(alert), draft)
        item = grouped.setdefault(
            decision.group_key,
            {"title": decision.title, "fingerprints": [], "missing_labels": set()},
        )
        item["fingerprints"].append(alert.upstream_fingerprint or alert.fingerprint)
        item["missing_labels"].update(decision.missing_labels)
    groups = [
        PreviewGroup(
            group_key=key,
            title=value["title"],
            fingerprints=sorted(value["fingerprints"]),
            missing_labels=sorted(value["missing_labels"]),
        )
        for key, value in sorted(grouped.items())
    ]
    names = source_name_map(session)
    by_source: list[dict[str, Any]] = []
    for source_id in source_ids:
        source_alerts = [item for item in alerts if item.source_id == source_id]
        source_selected = [item for item in selected if item.source_id == source_id]
        source_groups = {
            compute_aggregation_group(_fields(item), draft).group_key
            for item in source_selected
        }
        by_source.append(
            {
                "source_id": source_id,
                "source_name": names.get(source_id),
                "matcher_alert_count": sum(
                    rule_matches(_fields(alert), matchers) for alert in source_alerts
                ),
                "selected_alert_count": len(source_selected),
                "proposed_group_count": len(source_groups),
            }
        )
    return AggregationRulePreview(
        matcher_alert_count=matcher_alert_count,
        selected_alert_count=len(selected),
        proposed_group_count=len(groups),
        groups=groups,
        by_source=by_source,
    )


def _regroup_all(session: Session, changed_at: datetime) -> None:
    rules = list_aggregation_rules(session, enabled_only=True)
    source_scopes = aggregation_scope_map(session, [item.id for item in rules])
    alerts = session.exec(select(Alert).order_by(Alert.fingerprint)).all()
    stale_sources = non_enabled_source_ids(session)
    old_ids = {alert.incident_id for alert in alerts if alert.incident_id is not None}
    incidents_by_key = {
        incident.group_key: incident for incident in session.exec(select(Incident)).all()
    }
    used_ids: set[int] = set()
    for alert in alerts:
        if alert.alertname == WATCHDOG_ALERTNAME:
            alert.incident_id = None
            session.add(alert)
            continue
        fields = _fields(alert)
        rule = choose_matching_rule(fields, rules, source_scopes)
        decision = compute_aggregation_group(fields, rule)
        incident = incidents_by_key.get(decision.group_key)
        if incident is None:
            incident = Incident(
                source_id=alert.source_id,
                environment=alert.environment,
                group_key=decision.group_key,
                title=decision.title,
                severity=alert.severity,
                policy_version=rule.version if rule is not None else 0,
                grouping_explanation=decision.explanation,
                aggregation_rule_id=decision.rule_id,
                aggregation_rule_version=rule.version if rule is not None else None,
                group_labels=decision.group_labels,
                missing_group_labels=decision.missing_labels,
                change_origin="REGROUP",
                updated_at=changed_at,
                freshness_state=(
                    "STALE" if alert.source_id in stale_sources else "FRESH"
                ),
            )
            session.add(incident)
            session.flush()
            incidents_by_key[decision.group_key] = incident
        rule_version = rule.version if rule is not None else None
        # `updated_at` is what the alert list means by "recently updated", so it
        # may only move when this regroup actually changed the grouping. Stamping
        # every incident made a three-week-old one look like it had just
        # happened -- and since startup regroups, that happened on every boot.
        regrouped = (
            incident.title != decision.title
            or incident.grouping_explanation != decision.explanation
            or incident.aggregation_rule_id != decision.rule_id
            or incident.aggregation_rule_version != rule_version
            or incident.group_labels != decision.group_labels
            or incident.missing_group_labels != decision.missing_labels
        )
        incident.title = decision.title
        incident.policy_version = rule.version if rule is not None else 0
        incident.grouping_explanation = decision.explanation
        incident.aggregation_rule_id = decision.rule_id
        incident.aggregation_rule_version = rule_version
        incident.group_labels = decision.group_labels
        incident.missing_group_labels = decision.missing_labels
        if regrouped:
            incident.change_origin = "REGROUP"
            incident.updated_at = changed_at
        session.add(incident)
        alert.incident_id = incident.id
        session.add(alert)
        if incident.id is not None:
            used_ids.add(incident.id)

    session.flush()
    # Never `mark_fresh` here. Freshness is evidence about the source, and a
    # regroup observes nothing: it reshuffles alerts we already had. Marking
    # FRESH cleared STALE off every disabled source's incidents -- and because
    # startup regroups, restarting the app was enough to do it. Only a trusted
    # COMPLETE live poll may rebuild freshness (SYSTEM_SPEC §4).
    recompute_incident_lifecycle(session, changed_at, mark_fresh=False)
    session.flush()
    for incident_id in old_ids - used_ids:
        incident = session.get(Incident, incident_id)
        if incident is None:
            continue
        has_member = session.exec(
            select(Alert.id).where(Alert.incident_id == incident_id)
        ).first()
        if has_member is not None:
            continue
        has_audit = session.exec(
            select(IncidentAudit.id).where(IncidentAudit.incident_id == incident_id)
        ).first()
        terminate_routes_for_regroup(session, incident, observed_at=changed_at)
        has_route = session.exec(
            select(NotificationRoute.id).where(
                NotificationRoute.incident_id == incident_id
            )
        ).first()
        # A superseded incident points at its canonical one. Deleting the target
        # violates that foreign key, and the failure surfaces on a later
        # autoflush -- far from here -- which took the whole startup down.
        has_successor = session.exec(
            select(Incident.id).where(
                Incident.superseded_by_incident_id == incident_id
            )
        ).first()
        if has_audit is None and has_route is None and has_successor is None:
            session.delete(incident)
        else:
            incident.source_state = "recovered"
            incident.updated_at = changed_at
            session.add(incident)

    for incident_id in used_ids:
        incident = session.get(Incident, incident_id)
        if incident is None:
            continue
        severities = [
            alert.severity
            for alert in alerts
            if alert.incident_id == incident_id and alert.source_state != "resolved"
        ]
        incident.severity = max_severity(severities)
        session.add(incident)


def regroup_all_with_aggregation_rules(
    session: Session, changed_at: datetime | None = None
) -> None:
    changed_at = changed_at or datetime.now(timezone.utc)
    try:
        _regroup_all(session, changed_at)
        session.commit()
    except Exception:
        session.rollback()
        raise


def _ensure_unique_name(
    session: Session, name: str, *, excluding_rule_id: int | None = None
) -> None:
    existing = session.exec(
        select(AggregationRule).where(AggregationRule.name == name)
    ).first()
    if existing is not None and existing.id != excluding_rule_id:
        raise FileExistsError(f"aggregation rule name already exists: {name}")


def create_aggregation_rule(
    session: Session,
    *,
    name: str,
    priority: int,
    enabled: bool,
    matchers: list[dict[str, Any]],
    group_by_labels: list[str],
    source_scope: dict | None = None,
    changed_at: datetime | None = None,
) -> AggregationRule:
    name, priority, matchers, group_by_labels = validate_rule_definition(
        name=name,
        priority=priority,
        matchers=matchers,
        group_by_labels=group_by_labels,
    )
    scope_mode, source_ids = validate_source_scope(session, source_scope)
    _ensure_unique_name(session, name)
    changed_at = changed_at or datetime.now(timezone.utc)
    rule = AggregationRule(
        name=name,
        priority=priority,
        enabled=enabled,
        matchers=matchers,
        group_by_labels=group_by_labels,
        scope_mode=scope_mode,
        version=1,
        created_at=changed_at,
        updated_at=changed_at,
    )
    session.add(rule)
    session.flush()
    replace_aggregation_rule_sources(
        session, int(rule.id), scope_mode, source_ids
    )
    _regroup_all(session, changed_at)
    session.flush()
    return rule


def update_aggregation_rule(
    session: Session,
    rule_id: int,
    *,
    name: str,
    priority: int,
    enabled: bool,
    matchers: list[dict[str, Any]],
    group_by_labels: list[str],
    source_scope: dict | None = None,
    changed_at: datetime | None = None,
) -> AggregationRule:
    rule = session.get(AggregationRule, rule_id)
    if rule is None:
        raise LookupError(f"aggregation rule not found: {rule_id}")
    name, priority, matchers, group_by_labels = validate_rule_definition(
        name=name,
        priority=priority,
        matchers=matchers,
        group_by_labels=group_by_labels,
    )
    scope_mode, source_ids = validate_source_scope(session, source_scope)
    _ensure_unique_name(session, name, excluding_rule_id=rule_id)
    changed_at = changed_at or datetime.now(timezone.utc)
    rule.name = name
    rule.priority = priority
    rule.enabled = enabled
    rule.matchers = matchers
    rule.group_by_labels = group_by_labels
    rule.scope_mode = scope_mode
    rule.version += 1
    rule.updated_at = changed_at
    session.add(rule)
    session.flush()
    replace_aggregation_rule_sources(
        session, int(rule.id), scope_mode, source_ids
    )
    _regroup_all(session, changed_at)
    session.flush()
    return rule
