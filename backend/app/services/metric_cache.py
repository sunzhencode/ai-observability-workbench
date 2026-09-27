"""In-process TTL cache for the two slow-moving evidence reads.

Deliberately **not** persisted and deliberately **not** covering curve data.

- Rule definitions and metric metadata change on the order of deployments, so
  re-reading them every time an alert is opened is pure waste.
- Curve data is the opposite: opening an alert means "show me how this looks
  *now*", and a cache there would hand back the very thing the reader came to
  check.

The negative entry matters as much as the positive one: a deployment whose Thanos
Query has no Ruler behind it answers "no rules" every single time, and re-asking
on every alert is a guaranteed-useless round trip.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

RULES_TTL_SECONDS = 300.0
RULES_NEGATIVE_TTL_SECONDS = 900.0
METADATA_TTL_SECONDS = 1800.0


@dataclass
class _Entry:
    value: Any
    expires_at: float


@dataclass
class TTLCache:
    """Small, unbounded-by-design: keys are source ids and metric names."""

    _entries: dict[str, _Entry] = field(default_factory=dict)

    def get(self, key: str) -> tuple[bool, Any]:
        entry = self._entries.get(key)
        if entry is None:
            return False, None
        if entry.expires_at <= time.monotonic():
            self._entries.pop(key, None)
            return False, None
        return True, entry.value

    def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        self._entries[key] = _Entry(
            value=value, expires_at=time.monotonic() + ttl_seconds
        )

    def clear(self) -> None:
        self._entries.clear()


RULES_CACHE = TTLCache()
METADATA_CACHE = TTLCache()


def rules_ttl_for(rules: list[Any]) -> float:
    """Short TTL when rules exist, long one when they never will.

    An empty answer is a stable fact about the deployment, not a transient miss.
    """

    return RULES_TTL_SECONDS if rules else RULES_NEGATIVE_TTL_SECONDS


def reset_all() -> None:
    """Test hook. Production never needs this: process restart clears it."""

    RULES_CACHE.clear()
    METADATA_CACHE.clear()
