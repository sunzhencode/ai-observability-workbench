"""Stable, non-reversible identity derived from an Alertmanager address."""

from __future__ import annotations

import hashlib
from urllib.parse import urlsplit, urlunsplit

from app.runtime_config import ActiveRuntimeConfig

UNMANAGED_SOURCE_ID = "am:unconfigured"


def canonical_alertmanager_url(base_url: str) -> str:
    """Normalize harmless URL spelling differences without exposing credentials."""
    raw = str(base_url or "").strip()
    parsed = urlsplit(raw)
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()

    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    port = parsed.port
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        hostname = f"{hostname}:{port}"

    path = parsed.path.rstrip("/")
    return urlunsplit((scheme, hostname, path, "", ""))


def source_id_for_alertmanager(base_url: str) -> str:
    canonical = canonical_alertmanager_url(base_url)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"am:{digest}"


def active_source_id(runtime: ActiveRuntimeConfig | None = None) -> str:
    """Placeholder id for rows that predate the registry.

    F21 removed the `.env` derivation: there is no "active" source outside the
    registry any more. Kept as a stable constant so historical rows written by
    the retired legacy path still resolve to something, and so migration v6 can
    recognise and repoint them. See ADR 0004.
    """
    del runtime
    return UNMANAGED_SOURCE_ID


def scoped_fingerprint(source_id: str, upstream_fingerprint: str) -> str:
    """Reuse the legacy unique fingerprint column as a source-scoped storage key."""
    return f"{source_id}:{upstream_fingerprint}"
