"""Pure helpers for bounded Thanos ALERTS history reconstruction."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


@dataclass(frozen=True)
class BackfillWindow:
    start: datetime
    end: datetime
    requested_hours: int
    effective_hours: int
    truncated_reason: str | None


def backfill_window(
    now: datetime,
    requested_hours: int,
    hard_limit_hours: int = 168,
) -> BackfillWindow:
    requested = max(0, requested_hours)
    hard_limit = max(0, hard_limit_hours)
    effective = min(requested, hard_limit)
    truncated_reason = None
    if requested > hard_limit:
        truncated_reason = (
            f"requested {requested}h exceeds hard limit {hard_limit}h"
        )
    return BackfillWindow(
        start=now - timedelta(hours=effective),
        end=now,
        requested_hours=requested,
        effective_hours=effective,
        truncated_reason=truncated_reason,
    )


def _iso_from_timestamp(value: float) -> str:
    return (
        datetime.fromtimestamp(value, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _positive_sample_timestamps(values: Any) -> list[float]:
    timestamps: list[float] = []
    if not isinstance(values, list):
        return timestamps
    for sample in values:
        if not isinstance(sample, list) or len(sample) < 2:
            continue
        try:
            timestamp = float(sample[0])
            numeric_value = float(sample[1])
        except (TypeError, ValueError):
            continue
        if numeric_value > 0:
            timestamps.append(timestamp)
    return sorted(timestamps)


def thanos_matrix_to_alerts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert a Thanos matrix response into stable reconstructed raw alerts."""
    data = payload.get("data") if isinstance(payload, dict) else None
    results = data.get("result") if isinstance(data, dict) else None
    if not isinstance(results, list):
        return []

    reconstructed: list[dict[str, Any]] = []
    for series in results:
        if not isinstance(series, dict):
            continue
        metric = series.get("metric")
        if not isinstance(metric, dict):
            continue
        timestamps = _positive_sample_timestamps(series.get("values"))
        if not timestamps:
            continue

        labels = {
            str(key): str(value)
            for key, value in metric.items()
            if key not in {"__name__", "alertstate"}
        }
        canonical = json.dumps(labels, sort_keys=True, separators=(",", ":"))
        fingerprint = "backfill-" + hashlib.sha256(canonical.encode()).hexdigest()[:20]
        reconstructed.append(
            {
                "fingerprint": fingerprint,
                "labels": labels,
                "annotations": {},
                "startsAt": _iso_from_timestamp(timestamps[0]),
                "endsAt": _iso_from_timestamp(timestamps[-1]),
                "status": {"state": "historical"},
            }
        )

    reconstructed.sort(key=lambda item: item["fingerprint"])
    return reconstructed
