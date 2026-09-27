"""Read-only query service for the durable platform event stream."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, cast

from app.domains.operations.events import PlatformEvent


class DecodedCursor(Protocol):
    position: int


class CursorCodec(Protocol):
    def encode(self, *, position: int, filters: dict[str, Any]) -> str: ...

    def decode(self, token: str, *, filters: dict[str, Any]) -> Any: ...


class EventStore(Protocol):
    def list_events(
        self,
        *,
        after: int,
        event_types: tuple[str, ...],
        limit: int,
    ) -> tuple[PlatformEvent, ...]: ...


@dataclass(frozen=True, slots=True)
class EventPage:
    items: tuple[PlatformEvent, ...]
    next_cursor: str


class EventQueryService:
    def __init__(
        self,
        store: EventStore,
        codec: CursorCodec,
    ) -> None:
        self._store = store
        self._codec = codec

    def cursor_for(self, *, position: int, event_types: tuple[str, ...]) -> str:
        normalized = tuple(sorted(set(event_types)))
        return self._codec.encode(
            position=position,
            filters={"types": list(normalized)},
        )

    def poll(
        self,
        *,
        cursor: str | None,
        event_types: tuple[str, ...],
        limit: int,
    ) -> EventPage:
        normalized = tuple(sorted(set(event_types)))
        filters = {"types": list(normalized)}
        decoded = None if cursor is None else cast(
            DecodedCursor,
            self._codec.decode(cursor, filters=filters),
        )
        position = 0 if decoded is None else decoded.position
        items = self._store.list_events(
            after=position,
            event_types=normalized,
            limit=limit,
        )
        next_position = items[-1].sequence if items else position
        return EventPage(
            items=items,
            next_cursor=self._codec.encode(position=next_position, filters=filters),
        )
