"""Resolve the optional Thanos history address that belongs to a source.

F22 retired Historical Data Sources, evidence bindings and scope modes.  A
source either carries a history address or it does not, and history is read
for that source only -- so there is nothing left to bind, scope or de-overlap.

Read side only.  The address is written through the source's own configuration
in :mod:`app.services.event_sources`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import inspect
from sqlmodel import Session, select

from app.crypto import SecretBox, SecretError
from app.registry_models import EventSource, SourceThanosConfig
from app import master_key as master_key_module


@dataclass(frozen=True)
class ThanosConnection:
    """An immutable connection snapshot taken once at a job boundary."""

    source_id: str
    source_name: str
    base_url: str
    token: str = field(default="", repr=False)
    timeout_seconds: float = 15.0
    error_code: str | None = None

    @property
    def usable(self) -> bool:
        return bool(self.base_url.strip()) and self.error_code is None


def _table_available(session: Session, name: str) -> bool:
    """Ask over the session's own connection.

    Inspecting the *engine* checks a connection out of the pool, and against a
    ``StaticPool`` that is the very connection the session is using: the
    inspector's rollback then throws away whatever the caller had flushed.
    """
    return inspect(session.connection()).has_table(name)


def enabled_thanos_connections(
    session: Session, *, box: SecretBox | None = None
) -> list[ThanosConnection]:
    """History connections of every enabled source that has one configured.

    A connection whose secret cannot be opened is returned carrying
    ``error_code`` instead of being dropped: callers must degrade visibly
    rather than quietly read from somewhere else.
    """
    if not _table_available(session, "sourcethanosconfig"):
        return []
    rows = session.exec(
        select(SourceThanosConfig, EventSource)
        .join(EventSource, EventSource.id == SourceThanosConfig.source_id)
        .where(EventSource.lifecycle_state == "ENABLED")
        .order_by(EventSource.name, EventSource.id)
    ).all()
    connections: list[ThanosConnection] = []
    for config, source in rows:
        if not config.canonical_url:
            continue
        token = ""
        error_code: str | None = None
        if config.auth_kind != "NONE":
            try:
                token = (box or SecretBox(master_key_module.master_key())).decrypt(
                    config.secret_envelope_json
                )
            except SecretError:
                error_code = "SECRET_UNAVAILABLE"
        connections.append(
            ThanosConnection(
                source_id=source.id,
                source_name=source.name,
                base_url=config.canonical_url,
                token=token,
                timeout_seconds=float(config.timeout_seconds),
                error_code=error_code,
            )
        )
    return connections
