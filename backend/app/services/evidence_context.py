"""Which source a piece of evidence belongs to, captured once and rechecked.

Extracted from `api/metrics.py` when that file grew to a thousand lines holding
two unrelated things. Both routers need the same four answers before they can
read anything — which source, is it visible, is it still the one we captured,
and how do we reach it — and duplicating them across two files is how the two
paths drift apart.

Deterministic by this repository's layering rule: everything here takes a
session and returns data. `build_thanos_client` is the exception that proves it
— it *constructs* a client without calling anything, and lives here so both
routers share one named seam that offline tests can replace.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlmodel import Session

from app.registry_models import EventSource
from app.services.source_scope import has_event_sources, visible_source_ids
from app.services.thanos_history import enabled_thanos_connections
from app.sources.thanos import ThanosClient


@dataclass(frozen=True)
class SourceSnapshot:
    """One immutable view of a source, captured before any read (D38).

    Carries the config `version` so the same checks F20 already makes on the poll
    path apply here: if the source is saved, disabled or archived while requests
    are in flight, the results describe a configuration that no longer exists and
    must be discarded rather than rendered.
    """

    source_id: str
    version: int
    lifecycle_state: str
    connection: Any

    @property
    def usable(self) -> bool:
        return self.connection is not None and self.connection.usable

    @property
    def cache_scope(self) -> str:
        """Cache keys must include the version — an address change invalidates."""

        return f"{self.source_id}@{self.version}"


def build_thanos_client(connection) -> ThanosClient:  # noqa: D401
    """The single place this module constructs a client.

    A named seam rather than an inline constructor so the HTTP layer itself can
    be tested offline. F24's lesson was that a service layer with coverage and an
    endpoint without one lets the request shape change with nothing failing —
    every classification below only exists at this layer.
    """

    return ThanosClient(
        base_url=connection.base_url,
        token=connection.token,
        timeout=connection.timeout_seconds,
    )


def source_snapshot(session: Session, source_id: str) -> SourceSnapshot:
    source = session.get(EventSource, source_id)
    connection = None
    for candidate in enabled_thanos_connections(session):
        if candidate.source_id == source_id:
            connection = candidate
            break
    return SourceSnapshot(
        source_id=source_id,
        version=int(getattr(source, "version", 0) or 0),
        lifecycle_state=str(getattr(source, "lifecycle_state", "") or ""),
        connection=connection,
    )


def source_changed(session: Session, snapshot: SourceSnapshot) -> bool:
    """Recheck after the network round-trips, exactly as the poll path does."""

    source = session.get(EventSource, snapshot.source_id)
    if source is None:
        return True
    return (
        int(source.version or 0) != snapshot.version
        or str(source.lifecycle_state or "") != snapshot.lifecycle_state
    )



def source_visible(session: Session, source_id: str) -> bool:
    if not has_event_sources(session):
        return source_id in visible_source_ids(session)
    return source_id in visible_source_ids(
        session, requested=[source_id], include_archived=True
    )
