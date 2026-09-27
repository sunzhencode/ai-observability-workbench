"""Ingest orchestration: normalize -> upsert alerts -> group into incidents.

This function is pure with respect to I/O (no network): it takes already
fetched raw Alertmanager alerts and a database session, so it can be tested
deterministically with offline fixtures.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session, select

from app.models import Alert, Incident, IncidentAudit
from app.runtime_config import ActiveRuntimeConfig, runtime_config_provider
from app.services.aggregation_rules import (
    WATCHDOG_ALERTNAME,
    AggregationDecision,
    aggregation_scope_map,
    choose_matching_rule,
    compute_aggregation_group,
    list_aggregation_rules,
)
from app.services.lifecycle import apply_successful_poll
from app.services.lifecycle import recompute_incident_lifecycle
from app.services.normalizer import normalize_alert
from app.services.occurrence_history import seal_ended_occurrences
from app.services.notification_planner import (
    reconcile_incident_changes,
    snapshot_incidents,
)
from app.services.source_identity import (
    active_source_id,
    scoped_fingerprint,
)


@dataclass
class IngestResult:
    poll_time: datetime
    alerts_seen: int
    incidents_touched: int
    watchdog_clusters_seen: dict[str, datetime]


def _get_or_create_incident(
    session: Session,
    decision: AggregationDecision,
    fields: dict[str, Any],
    policy_version: int,
) -> Incident:
    incident = session.exec(
        select(Incident).where(Incident.group_key == decision.group_key)
    ).first()
    if incident is None:
        incident = Incident(
            source_id=fields["source_id"],
            environment=fields.get("environment", "prod"),
            group_key=decision.group_key,
            title=decision.title,
            severity=fields.get("severity", "unknown"),
            policy_version=policy_version,
            grouping_explanation=decision.explanation,
            aggregation_rule_id=decision.rule_id,
            aggregation_rule_version=(
                policy_version if decision.rule_id is not None else None
            ),
            group_labels=decision.group_labels,
            missing_group_labels=decision.missing_labels,
            occurrence_started_at=fields.get("starts_at") or fields["observed_at"],
            change_origin=fields["change_origin"],
        )
        session.add(incident)
        session.flush()
        session.refresh(incident)
    else:
        incident.title = decision.title
        incident.policy_version = policy_version
        incident.grouping_explanation = decision.explanation
        incident.aggregation_rule_id = decision.rule_id
        incident.aggregation_rule_version = (
            policy_version if decision.rule_id is not None else None
        )
        incident.group_labels = decision.group_labels
        incident.missing_group_labels = decision.missing_labels
        incident.change_origin = fields["change_origin"]
        session.add(incident)
        session.flush()
    return incident


def _upsert_alert(session: Session, fields: dict[str, Any], poll_time: datetime) -> Alert:
    existing = session.exec(
        select(Alert).where(Alert.fingerprint == fields["fingerprint"])
    ).first()
    if existing is None:
        persisted_fields = {
            key: value for key, value in fields.items() if key in Alert.model_fields
        }
        alert = Alert(
            **persisted_fields,
            first_seen_at=poll_time,
            last_seen_at=poll_time,
        )
        session.add(alert)
        session.flush()
        session.refresh(alert)
        return alert

    # Idempotent update: refresh volatile fields, keep first_seen_at immutable.
    existing.last_seen_at = poll_time
    if fields["origin"] == "live":
        existing.source_state = "firing"
        existing.missing_since_at = None
        existing.origin = "live"
        existing.evidence_completeness = "complete"
    elif existing.origin != "live":
        existing.source_state = "resolved"
        existing.origin = "backfill"
        existing.evidence_completeness = "reconstructed"
    existing.labels = fields["labels"]
    existing.annotations = fields["annotations"]
    existing.raw_payload = fields["raw_payload"]
    existing.alertname = fields["alertname"]
    existing.source_id = fields["source_id"]
    existing.upstream_fingerprint = fields["upstream_fingerprint"]
    existing.environment = fields["environment"]
    existing.severity = fields["severity"]
    existing.cluster = fields["cluster"]
    existing.starts_at = fields["starts_at"]
    existing.ends_at = fields["ends_at"]
    session.add(existing)
    session.flush()
    session.refresh(existing)
    return existing


def ingest_alerts(
    session: Session,
    raw_alerts: list[dict[str, Any]],
    poll_time: datetime | None = None,
    resolution_grace_seconds: int | None = None,
    origin: str = "live",
    evidence_completeness: str = "complete",
    reconcile_lifecycle: bool = True,
    source_id: str | None = None,
    runtime: ActiveRuntimeConfig | None = None,
    environment: str | None = None,
) -> IngestResult:
    poll_time = poll_time or datetime.now(timezone.utc)
    runtime = runtime or runtime_config_provider.snapshot()
    source_id = source_id or active_source_id(runtime)
    normalize_environment = environment if environment is not None else runtime.environment
    incident_before = snapshot_incidents(session, source_id=source_id)
    touched_incident_ids: set[int] = set()
    active_fingerprints: set[str] = set()
    watchdog_clusters_seen: dict[str, datetime] = {}
    rules = list_aggregation_rules(session, enabled_only=True)
    rule_scopes = aggregation_scope_map(session, [rule.id for rule in rules])

    for raw in raw_alerts:
        fields = normalize_alert(raw, environment=normalize_environment)
        fields["origin"] = origin
        fields["evidence_completeness"] = evidence_completeness
        fields["source_state"] = "firing" if origin == "live" else "resolved"
        fields["observed_at"] = poll_time
        fields["change_origin"] = "LIVE_POLL" if origin == "live" else "BACKFILL"
        if not fields["fingerprint"]:
            continue
        upstream_fingerprint = fields["fingerprint"]
        fields["source_id"] = source_id
        fields["upstream_fingerprint"] = upstream_fingerprint
        storage_fingerprint = scoped_fingerprint(source_id, upstream_fingerprint)
        if origin == "live":
            active_fingerprints.add(storage_fingerprint)
        if fields["alertname"] == WATCHDOG_ALERTNAME:
            fields["fingerprint"] = storage_fingerprint
            alert = _upsert_alert(session, fields, poll_time)
            if origin == "live":
                # One cluster may emit several Watchdog series. The result is
                # a cluster set, not a series count.
                watchdog_clusters_seen[fields["cluster"]] = poll_time
            previous_incident_id = alert.incident_id
            if previous_incident_id is not None:
                alert.incident_id = None
                session.add(alert)
                session.flush()
                previous = session.get(Incident, previous_incident_id)
                has_member = session.exec(
                    select(Alert.id).where(Alert.incident_id == previous_incident_id)
                ).first()
                has_audit = session.exec(
                    select(IncidentAudit.id).where(
                        IncidentAudit.incident_id == previous_incident_id
                    )
                ).first()
                if previous is not None and has_member is None and has_audit is None:
                    session.delete(previous)
                    session.flush()
            continue

        rule = choose_matching_rule(fields, rules, rule_scopes)
        decision = compute_aggregation_group(fields, rule)
        incident = _get_or_create_incident(
            session,
            decision,
            fields,
            policy_version=rule.version if rule is not None else 0,
        )
        fields["fingerprint"] = storage_fingerprint
        alert = _upsert_alert(session, fields, poll_time)
        if alert.incident_id != incident.id:
            alert.incident_id = incident.id
            session.add(alert)
            session.flush()
        if incident.id is not None:
            touched_incident_ids.add(incident.id)

    if reconcile_lifecycle:
        apply_successful_poll(
            session,
            active_fingerprints,
            poll_time,
            runtime.resolution_grace_seconds
            if resolution_grace_seconds is None
            else resolution_grace_seconds,
            source_id=source_id,
        )
    else:
        recompute_incident_lifecycle(
            session,
            poll_time,
            source_id=source_id,
            mark_fresh=False,
        )
        session.flush()
    changes = reconcile_incident_changes(
        session,
        incident_before,
        source_id=source_id,
        observed_at=poll_time,
        origin="LIVE_POLL" if origin == "live" else "BACKFILL",
    )
    # F26: the same change list, not a second diff of our own. `reconcile_...`
    # already decided which Incidents moved and how; this return value used to be
    # discarded. History and notifications must never disagree about whether an
    # occurrence ended, and `reconcile_lifecycle` is exactly the F20 completeness
    # gate that says whether absence may be read as recovery at all.
    seal_ended_occurrences(
        session,
        changes,
        reconcile_lifecycle=reconcile_lifecycle,
        observed_at=poll_time,
    )

    return IngestResult(
        poll_time=poll_time,
        alerts_seen=len(raw_alerts),
        incidents_touched=len(touched_incident_ids),
        watchdog_clusters_seen=watchdog_clusters_seen,
    )
