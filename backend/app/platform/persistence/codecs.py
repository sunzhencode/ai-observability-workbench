"""Canonical SQLite timestamp and JSON codecs for platform persistence adapters."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from typing import overload


def stored_utc(value: datetime) -> datetime:
    """Normalize an application timestamp to SQLite's naive-UTC convention."""
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


@overload
def aware_utc(value: datetime) -> datetime: ...


@overload
def aware_utc(value: None) -> None: ...


def aware_utc(value: datetime | None) -> datetime | None:
    """Restore SQLite timestamps as aware UTC while accepting optional columns."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def stored_utc_text(value: datetime) -> str:
    """Render the canonical SQLite timestamp form used by bounded SQL deletes."""
    return stored_utc(value).isoformat(sep=" ")


def canonical_json(value: object) -> str:
    """Encode deterministic ASCII JSON for persisted comparison and hashing."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def unicode_json(value: object) -> str:
    """Encode deterministic readable JSON for persisted operator/model text."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def stringified_json(value: object) -> str:
    """Encode source payloads whose legacy contract stringifies uncommon values."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )


__all__ = [
    "aware_utc",
    "canonical_json",
    "stored_utc",
    "stored_utc_text",
    "stringified_json",
    "unicode_json",
]
