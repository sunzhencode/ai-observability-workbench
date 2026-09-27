"""Persisted event facts exposed to resumable application queries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class PlatformEvent:
    sequence: int
    event_type: str
    subject_type: str
    subject_id: str
    created_at: datetime
