"""Deterministic alert and incident lifecycle transitions (no network I/O)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from app.models import Alert, Incident
from app.services.grouping import max_severity
from app.services.source_identity import active_source_id


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def derive_incident_source_state(member_states: list[str]) -> str:
    """Derive Incident state with explicit, deterministic precedence."""
    states = set(member_states)
    if "firing" in states:
        return "firing"
    if "unknown" in states:
        return "unknown"
    if "pending_resolution" in states:
        return "pending_resolution"
    return "recovered"


def recompute_incident_lifecycle(
    session: Session,
    poll_time: datetime,
    source_id: str | None = None,
    *,
    mark_fresh: bool = True,
) -> None:
    """Recompute derived Incident state, touching `updated_at` only on change.

    This runs for every Incident of a source on every successful poll. Stamping
    `updated_at` unconditionally therefore gave every Incident of an actively
    polled source the same timestamp -- the last poll -- which silently emptied
    the alert list's "most recently updated" ordering (CAP-08): a three-week-old
    Incident sorted exactly like one that had just fired.
    """
    statement = select(Incident)
    statement = statement.where(Incident.superseded_by_incident_id.is_(None))
    if source_id is not None:
        statement = statement.where(Incident.source_id == source_id)
    for incident in session.exec(statement).all():
        members = session.exec(
            select(Alert).where(Alert.incident_id == incident.id)
        ).all()
        if not members:
            severity = incident.severity
            state = "recovered"
        else:
            active = [alert for alert in members if alert.source_state != "resolved"]
            severity_basis = active if active else members
            severity = max_severity([alert.severity for alert in severity_basis])
            state = derive_incident_source_state(
                [alert.source_state for alert in members]
            )
        freshness = "FRESH" if mark_fresh else incident.freshness_state
        if (
            incident.severity == severity
            and incident.source_state == state
            and incident.freshness_state == freshness
        ):
            continue
        incident.severity = severity
        incident.source_state = state
        incident.freshness_state = freshness
        incident.updated_at = poll_time
        session.add(incident)


def apply_successful_poll(
    session: Session,
    active_fingerprints: set[str],
    poll_time: datetime,
    resolution_grace_seconds: int,
    source_id: str | None = None,
) -> None:
    """Apply trustworthy presence/absence evidence from one successful poll.

    The `unknown` branch below is a read path, not a write path. No current
    runtime produces an unknown Alert: a FAILED poll never reaches ingest, which
    is what CAP-02.7 requires. It survives for M1-era rows, which re-enter the
    resolution grace here rather than resolving straight from a stale state.
    """
    grace = timedelta(seconds=max(0, resolution_grace_seconds))
    poll_utc = _utc(poll_time)

    source_id = source_id or active_source_id()
    alerts = session.exec(select(Alert).where(Alert.source_id == source_id)).all()
    for alert in alerts:
        if alert.fingerprint in active_fingerprints:
            alert.source_state = "firing"
            alert.missing_since_at = None
        elif alert.source_state == "resolved":
            continue
        elif alert.source_state == "unknown" or alert.missing_since_at is None:
            alert.source_state = "pending_resolution"
            alert.missing_since_at = poll_time
        elif poll_utc - _utc(alert.missing_since_at) >= grace:
            alert.source_state = "resolved"
        else:
            alert.source_state = "pending_resolution"
        session.add(alert)

    recompute_incident_lifecycle(session, poll_time, source_id=source_id)
    session.flush()
