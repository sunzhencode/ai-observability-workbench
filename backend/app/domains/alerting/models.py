"""Pure Alert normalization and deterministic aggregation decisions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import json
import re
from typing import Mapping

from app.domains.sources.models import RawAlert, alert_identity

NO_CLUSTER = "__unknown_cluster__"
SEVERITIES = {"critical", "warning", "info"}


class MatcherOperator(StrEnum):
    EQUALS = "="
    NOT_EQUALS = "!="
    REGEX = "=~"
    NOT_REGEX = "!~"


@dataclass(frozen=True, slots=True)
class Matcher:
    label: str
    operator: MatcherOperator
    value: str

    def matches(self, labels: Mapping[str, str]) -> bool:
        actual = labels.get(self.label, "")
        if self.operator is MatcherOperator.EQUALS:
            return actual == self.value
        if self.operator is MatcherOperator.NOT_EQUALS:
            return actual != self.value
        matched = re.fullmatch(self.value, actual) is not None
        return matched if self.operator is MatcherOperator.REGEX else not matched


@dataclass(frozen=True, slots=True)
class AggregationRule:
    id: int
    name: str
    priority: int
    enabled: bool
    matchers: tuple[Matcher, ...]
    group_by_labels: tuple[str, ...]
    source_ids: tuple[str, ...]
    version: int
    grouping_window_seconds: int = 30

    def applies_to(self, alert: NormalizedAlert) -> bool:
        return (
            self.enabled
            and (not self.source_ids or alert.source_id in self.source_ids)
            and all(matcher.matches(alert.labels) for matcher in self.matchers)
        )


@dataclass(frozen=True, slots=True)
class NormalizedAlert:
    source_id: str
    upstream_fingerprint: str
    labels: Mapping[str, str]
    annotations: Mapping[str, str]
    alertname: str
    severity: str
    cluster: str
    starts_at: datetime | None
    observed_at: datetime
    raw: RawAlert

    @property
    def route_fields(self) -> Mapping[str, str]:
        return {
            "source_id": self.source_id,
            "alertname": self.alertname,
            "severity": self.severity,
            "cluster": self.cluster,
        }


@dataclass(frozen=True, slots=True)
class AggregationDecision:
    group_key: str
    title: str
    rule_id: int | None
    rule_version: int | None
    group_labels: Mapping[str, str]
    missing_labels: tuple[str, ...]


def _strings(raw: object) -> dict[str, str]:
    if not isinstance(raw, Mapping):
        return {}
    return {str(key): str(value) for key, value in raw.items()}


def _datetime(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        value = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def normalize_alert(raw: RawAlert, *, source_id: str) -> NormalizedAlert:
    labels = _strings(raw.get("labels"))
    annotations = _strings(raw.get("annotations"))
    alertname = labels.get("alertname", "UnknownAlert")
    severity = labels.get("severity", "unknown").lower()
    if severity not in SEVERITIES:
        severity = "unknown"
    observed_at = _datetime(raw.get("updatedAt")) or _datetime(raw.get("startsAt"))
    return NormalizedAlert(
        source_id=source_id,
        upstream_fingerprint=alert_identity(raw),
        labels=labels,
        annotations=annotations,
        alertname=alertname,
        severity=severity,
        cluster=labels.get("cluster") or NO_CLUSTER,
        starts_at=_datetime(raw.get("startsAt")),
        observed_at=observed_at or datetime.now(timezone.utc),
        raw=raw,
    )


def _group_key(
    alert: NormalizedAlert,
    *,
    rule: AggregationRule | None,
    values: Mapping[str, str],
    missing: tuple[str, ...],
) -> str:
    parts = [f"source={alert.source_id}"]
    if rule is None:
        parts.extend(("rule=unmatched", f"fallback={alert.upstream_fingerprint}"))
        return "|".join(parts)
    parts.append(f"rule={rule.id}")
    if missing:
        parts.append(f"fallback={alert.upstream_fingerprint}")
        return "|".join(parts)
    encoded = json.dumps(values, sort_keys=True, separators=(",", ":"))
    parts.append(f"values={hashlib.sha256(encoded.encode('utf-8')).hexdigest()}")
    return "|".join(parts)


def choose_aggregation(
    alert: NormalizedAlert,
    rules: tuple[AggregationRule, ...],
) -> AggregationDecision:
    ordered = sorted(rules, key=lambda item: (item.priority, item.id))
    rule = next((item for item in ordered if item.applies_to(alert)), None)
    if rule is None:
        values: dict[str, str] = {}
        missing: tuple[str, ...] = ()
    else:
        values = {
            label: alert.labels[label]
            for label in rule.group_by_labels
            if label in alert.labels and alert.labels[label] != ""
        }
        missing = tuple(label for label in rule.group_by_labels if label not in values)
    title = alert.alertname
    if values:
        title = f"{alert.alertname} · " + " / ".join(values.values())
    return AggregationDecision(
        group_key=_group_key(alert, rule=rule, values=values, missing=missing),
        title=title,
        rule_id=None if rule is None else rule.id,
        rule_version=None if rule is None else rule.version,
        group_labels=values,
        missing_labels=missing,
    )
