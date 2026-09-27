"""Current Incident lifecycle and append-only occurrence history contracts."""

from app.domains.incidents.models import (
    HandlingState,
    IncidentLifecycleChange,
    IncidentLifecycleSnapshot,
    can_change_handling,
    is_recurrence,
    should_seal_occurrence,
)

__all__ = [
    "HandlingState",
    "IncidentLifecycleChange",
    "IncidentLifecycleSnapshot",
    "can_change_handling",
    "is_recurrence",
    "should_seal_occurrence",
]
