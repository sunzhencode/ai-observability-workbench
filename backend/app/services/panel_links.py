"""URL validation shared by Web-managed link configuration.

The incident dashboard/runbook link resolvers that used to live here were
retired together with the unmounted Grafana evidence router; only the URL
normalizer is still used (by :mod:`app.schemas` validators).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit


def normalize_web_url(value: Any) -> str | None:
    """Return a trimmed absolute HTTP(S) URL, otherwise ``None``."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or any(char.isspace() or ord(char) < 32 for char in value):
        return None
    parsed = urlsplit(value)
    try:
        hostname = parsed.hostname
        parsed.port
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc or not hostname:
        return None
    return value
