"""Normalize raw Alertmanager alerts into the workbench Alert shape.

The raw payload is preserved verbatim; missing fields become explicit defaults
(e.g. ``<no-cluster>``) rather than being silently dropped or fabricated.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from app.runtime_config import runtime_config_provider

_VALID_SEVERITIES = {"critical", "warning", "info"}
NO_CLUSTER = "<no-cluster>"


def _parse_time(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def alert_identity(raw: dict[str, Any]) -> str:
    """Return upstream fingerprint, or a stable hash of normalized labels."""

    fingerprint = str(raw.get("fingerprint") or "").strip()
    if fingerprint:
        return fingerprint
    labels = raw.get("labels") if isinstance(raw.get("labels"), dict) else {}
    normalized = {
        str(key): str(value)
        for key, value in sorted(labels.items(), key=lambda item: str(item[0]))
    }
    encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return "labels:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]


def normalize_alert(
    raw: dict[str, Any], *, environment: str | None = None
) -> dict[str, Any]:
    """Return a dict of normalized Alert fields from a raw Alertmanager alert."""
    labels = raw.get("labels") or {}
    annotations = raw.get("annotations") or {}

    alertname = str(labels.get("alertname") or "<unnamed>")
    severity_raw = str(labels.get("severity") or "").lower()
    severity = severity_raw if severity_raw in _VALID_SEVERITIES else "unknown"
    cluster = str(labels.get("cluster") or NO_CLUSTER)

    fingerprint = alert_identity(raw)

    return {
        "fingerprint": fingerprint,
        "environment": environment or runtime_config_provider.snapshot().environment,
        "alertname": alertname,
        "severity": severity,
        "cluster": cluster,
        "labels": dict(labels),
        "annotations": dict(annotations),
        "starts_at": _parse_time(raw.get("startsAt")),
        "ends_at": _parse_time(raw.get("endsAt")),
        "source_state": "firing",
        "origin": "live",
        "evidence_completeness": "complete",
        "raw_payload": raw,
    }
