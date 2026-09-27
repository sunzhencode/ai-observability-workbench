"""Severity ranking plus the legacy ``GroupingPolicy`` group-key helpers.

``compute_group_key`` here serves the legacy :mod:`app.services.policies` path
and still emits an ``env=`` dimension for old rows.  F20 grouping does **not**
use it: source-bound incident keys are built by
:func:`app.services.group_keys.build_group_key_v2`, which is keyed on the stable
``source_id`` and carries no environment at all.
"""

from __future__ import annotations

DEFAULT_GROUP_BY = ["alertname", "cluster", "severity"]

# Higher number = more severe. Used to sort and to pick incident severity.
SEVERITY_RANK = {"critical": 3, "warning": 2, "info": 1, "unknown": 0}


def severity_rank(severity: str) -> int:
    return SEVERITY_RANK.get(severity, 0)


def max_severity(severities: list[str]) -> str:
    if not severities:
        return "unknown"
    return max(severities, key=severity_rank)


def compute_group_key(fields: dict[str, str], group_by: list[str] | None = None) -> str:
    """Build a stable group key from normalized alert fields.

    ``source_id`` and ``environment`` are mandatory isolation dimensions.
    """
    keys = group_by or DEFAULT_GROUP_BY
    parts = [
        f"source={fields.get('source_id', 'legacy')}",
        f"env={fields.get('environment', 'prod')}",
    ]
    for key in keys:
        value = fields.get(key)
        parts.append(f"{key}={value if value not in (None, '') else f'<no-{key}>'}")
    return "|".join(parts)


def build_title(fields: dict[str, str]) -> str:
    """Human-readable incident title derived from the grouping fields."""
    alertname = fields.get("alertname", "<unnamed>")
    cluster = fields.get("cluster", "<no-cluster>")
    return f"{alertname} · {cluster}"


def build_explanation(fields: dict[str, str], group_by: list[str] | None = None) -> str:
    keys = group_by or DEFAULT_GROUP_BY
    matched = ", ".join(f"{k}={fields.get(k)}" for k in keys)
    return (
        f"Grouped by {matched} "
        f"(source={fields.get('source_id', 'legacy')}; "
        f"environment={fields.get('environment', 'prod')})"
    )
