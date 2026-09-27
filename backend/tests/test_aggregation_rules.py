"""Standalone aggregation rules and deterministic alert assignment."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlmodel import select

from app.models import AggregationRule, Alert, Incident
from app.services.aggregation_rules import (
    choose_matching_rule,
    compute_aggregation_group,
    create_aggregation_rule,
    preview_aggregation_rule,
    rule_matches,
    validate_rule_definition,
)
from app.services.ingest import ingest_alerts


def _alert(fp: str, name: str, **labels: str) -> dict:
    return {
        "fingerprint": fp,
        "labels": {
            "alertname": name,
            "severity": labels.pop("severity", "warning"),
            **labels,
        },
        "annotations": {},
        "startsAt": "2026-07-16T09:00:00Z",
        "endsAt": "0001-01-01T00:00:00Z",
    }


def _rule(
    rule_id: int,
    name: str,
    priority: int,
    matchers: list[dict[str, str]],
    group_by: list[str],
) -> AggregationRule:
    return AggregationRule(
        id=rule_id,
        name=name,
        priority=priority,
        matchers=matchers,
        group_by_labels=group_by,
        enabled=True,
        version=1,
    )


def test_matcher_operators_and_priority_are_deterministic() -> None:
    fields = {
        "environment": "prod",
        "fingerprint": "pod-1",
        "alertname": "KubePodCrashLooping",
        "severity": "critical",
        "cluster": "cluster-a",
        "labels": {
            "alertname": "KubePodCrashLooping",
            "severity": "critical",
            "cluster": "cluster-a",
            "namespace": "payments",
        },
    }
    assert rule_matches(fields, [{"label": "alertname", "operator": "=", "value": "KubePodCrashLooping"}])
    assert rule_matches(fields, [{"label": "severity", "operator": "!=", "value": "info"}])
    assert rule_matches(fields, [{"label": "namespace", "operator": "=~", "value": "pay.*"}])
    assert rule_matches(fields, [{"label": "namespace", "operator": "!~", "value": "infra.*"}])

    later_id = _rule(9, "later id", 10, [], ["cluster"])
    earlier_id = _rule(3, "earlier id", 10, [], ["cluster"])
    higher_priority = _rule(5, "higher priority", 5, [], ["namespace"])
    assert choose_matching_rule(fields, [later_id, earlier_id, higher_priority]).id == 5
    assert choose_matching_rule(fields, [later_id, earlier_id]).id == 3


def test_invalid_regex_and_unbounded_definition_are_rejected() -> None:
    with pytest.raises(ValueError, match="invalid regex"):
        validate_rule_definition(
            name="bad",
            priority=10,
            matchers=[{"label": "pod", "operator": "=~", "value": "["}],
            group_by_labels=["cluster"],
        )
    with pytest.raises(ValueError, match="duplicate"):
        validate_rule_definition(
            name="duplicate",
            priority=10,
            matchers=[],
            group_by_labels=["cluster", "cluster"],
        )
    for retired_field in ("environment", "source_id"):
        with pytest.raises(ValueError, match="not a rule label"):
            validate_rule_definition(
                name="retired identity",
                priority=10,
                matchers=[
                    {"label": retired_field, "operator": "=", "value": "legacy"}
                ],
                group_by_labels=["cluster"],
            )


def test_unmatched_and_missing_labels_are_fingerprint_isolated() -> None:
    fields = {
        "environment": "prod",
        "fingerprint": "pod-1",
        "alertname": "PodFailure",
        "severity": "warning",
        "cluster": "cluster-a",
        "labels": {"alertname": "PodFailure", "cluster": "cluster-a"},
    }
    unmatched = compute_aggregation_group(fields, None)
    assert "unmatched" in unmatched.group_key
    assert "fingerprint" in unmatched.group_key
    assert "unmatched_rule" in unmatched.explanation

    rule = _rule(7, "pods", 10, [], ["cluster", "namespace"])
    missing = compute_aggregation_group(fields, rule)
    assert "rule=7" in missing.group_key
    assert "fingerprint" in missing.group_key
    assert missing.missing_labels == ["namespace"]

    complete = {**fields, "labels": {**fields["labels"], "namespace": "payments"}}
    group = compute_aggregation_group(complete, rule)
    assert "rule=7" in group.group_key
    assert "namespace=payments" in group.group_key
    assert "fingerprint" not in group.group_key
    assert "env=" not in unmatched.group_key
    assert "env=" not in missing.group_key
    assert "env=" not in group.group_key

    changed_environment = {**complete, "environment": "legacy-other"}
    assert compute_aggregation_group(changed_environment, rule).group_key == group.group_key
    assert not rule_matches(
        complete,
        [{"label": "environment", "operator": "=", "value": "prod"}],
    )


def test_preview_is_read_only_and_create_rule_atomically_regroups(session) -> None:
    now = datetime(2026, 7, 16, 10, 0, tzinfo=timezone.utc)
    ingest_alerts(
        session,
        [
            _alert("p-1", "PodFailure", cluster="a", namespace="payments", pod="one"),
            _alert("p-2", "PodFailure", cluster="a", namespace="payments", pod="two"),
            _alert("n-1", "NodeDown", cluster="a", instance="node-1"),
        ],
        poll_time=now,
    )
    before_ids = [row.id for row in session.exec(select(Incident)).all()]

    preview = preview_aggregation_rule(
        session,
        rule_id=None,
        name="Pod failures",
        priority=10,
        enabled=True,
        matchers=[{"label": "alertname", "operator": "=", "value": "PodFailure"}],
        group_by_labels=["cluster", "namespace"],
    )
    assert preview.matcher_alert_count == 2
    assert preview.selected_alert_count == 2
    assert preview.proposed_group_count == 1
    assert preview.groups[0].fingerprints == ["p-1", "p-2"]
    assert session.exec(select(AggregationRule)).all() == []
    assert [row.id for row in session.exec(select(Incident)).all()] == before_ids

    rule = create_aggregation_rule(
        session,
        name="Pod failures",
        priority=10,
        enabled=True,
        matchers=[{"label": "alertname", "operator": "=", "value": "PodFailure"}],
        group_by_labels=["cluster", "namespace"],
        changed_at=now,
    )
    assert rule.version == 1
    incidents = session.exec(select(Incident).order_by(Incident.group_key)).all()
    assert len(incidents) == 2
    pod_alerts = session.exec(
        select(Alert).where(Alert.alertname == "PodFailure").order_by(Alert.fingerprint)
    ).all()
    assert pod_alerts[0].incident_id == pod_alerts[1].incident_id
    pod_incident = session.get(Incident, pod_alerts[0].incident_id)
    assert pod_incident is not None
    assert "rule_name=Pod failures" in pod_incident.grouping_explanation
