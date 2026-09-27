"""Watchdog identification and grouping-label validation.

The per-alertname rule preview/publish flow this module used to own was retired
with its unmounted ``/api/alert-type-rules`` router: aggregation is driven by
``AggregationRule`` (see :mod:`app.services.aggregation_rules`).  The
``AlertTypeRule`` table itself is kept for SQLite compatibility only.
"""

from __future__ import annotations

import re

from app.models import Incident

WATCHDOG_ALERTNAME = "Watchdog"
LABEL_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
MAX_GROUP_LABELS = 8
MAX_LABEL_NAME_LENGTH = 128


def is_watchdog_grouping(group_key: str, title: str) -> bool:
    """The predicate itself, over the only two fields it reads.

    Callers that just need a count can select these two columns instead of
    whole Incident rows.
    """
    return (
        "alertname=Watchdog" in group_key
        or "alertname=Watchdog|" in group_key
        or title == WATCHDOG_ALERTNAME
        or title.startswith(f"{WATCHDOG_ALERTNAME} ·")
    )


def is_watchdog_incident(incident: Incident) -> bool:
    return is_watchdog_grouping(incident.group_key, incident.title)


def validate_group_by_labels(group_by_labels: list[str]) -> list[str]:
    values = [str(value).strip() for value in group_by_labels]
    if len(values) > MAX_GROUP_LABELS:
        raise ValueError(f"group_by_labels may contain at most {MAX_GROUP_LABELS} labels")
    if len(values) != len(set(values)):
        raise ValueError("group_by_labels must not contain duplicate labels")
    for value in values:
        if not value or len(value) > MAX_LABEL_NAME_LENGTH or not LABEL_NAME.fullmatch(value):
            raise ValueError(f"invalid Prometheus label name: {value or '<empty>'}")
        if value in {"alertname", "environment"}:
            raise ValueError(f"{value} is already an implicit grouping dimension")
    return values
