"""UTC clock and serialization helpers for Incident Operations platform contracts."""

from __future__ import annotations

from datetime import datetime, timezone


def utc_now() -> datetime:
    """Return an aware UTC timestamp."""
    return datetime.now(timezone.utc)


def to_utc_iso(value: datetime) -> str:
    """Serialize any datetime as RFC3339 UTC with a trailing ``Z``.

    Historical SQLite rows can be naive.  The existing product consistently
    interprets those values as UTC, so the platform seam preserves that rule.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
