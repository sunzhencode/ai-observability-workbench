"""Central recursive redaction for logs, health and safe error details."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

SENSITIVE_KEY_PARTS = (
    "secret",
    "token",
    "password",
    "webhook",
    "ciphertext",
    "authorization",
    "api_key",
)


def redact_sensitive(value: Any, *, key: str = "") -> Any:
    """Return a redacted deep copy without mutating the caller's value."""
    lowered = key.lower()
    if any(part in lowered for part in SENSITIVE_KEY_PARTS):
        if value in (None, "", {}, []):
            return None
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            str(item_key): redact_sensitive(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive(item) for item in value)
    return deepcopy(value)
