"""Local Incident handling-state transitions with immutable audit records."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlmodel import Session

from app.models import Incident, IncidentAudit

ALLOWED_TRANSITIONS = {
    "NEW": {"IN_PROGRESS", "CLOSED", "FALSE_POSITIVE"},
    "IN_PROGRESS": {"CLOSED", "FALSE_POSITIVE"},
    "CLOSED": set(),
    "FALSE_POSITIVE": set(),
}


class InvalidHandlingTransition(ValueError):
    pass


def change_handling_state(
    session: Session,
    incident: Incident,
    to_state: str,
    reason: str,
    actor: str = "local-user",
    changed_at: datetime | None = None,
) -> IncidentAudit:
    from_state = incident.handling_state
    if to_state not in ALLOWED_TRANSITIONS.get(from_state, set()):
        raise InvalidHandlingTransition(f"Cannot transition {from_state} to {to_state}")

    changed_at = changed_at or datetime.now(timezone.utc)
    audit = IncidentAudit(
        incident_id=incident.id,
        actor=actor,
        from_state=from_state,
        to_state=to_state,
        reason=reason,
        created_at=changed_at,
    )
    incident.handling_state = to_state
    incident.updated_at = changed_at
    session.add(incident)
    session.add(audit)
    session.flush()
    session.refresh(audit)
    return audit
