"""SQLite command-receipt adapter with no raw key or request retention."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from collections.abc import Mapping
from typing import Any

from sqlalchemy import DateTime, String, Text, UniqueConstraint, delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.application.commands import CommandClaim, CommandConflict
from app.platform.persistence.database import SessionFactory


class Base(DeclarativeBase):
    pass


class CommandReceiptRecord(Base):
    __tablename__ = "command_receipt"
    __table_args__ = (
        UniqueConstraint("scope", "key_hash", name="uq_command_receipt_scope_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(96), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    response_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class SqlAlchemyCommandReceiptStore:
    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    def claim(self, scope: str, key: str, payload: str) -> CommandClaim:
        key_hash = _hash(key)
        request_hash = _hash(payload)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with self._sessions() as session:
            session.add(
                CommandReceiptRecord(
                    scope=scope,
                    key_hash=key_hash,
                    request_hash=request_hash,
                    state="IN_PROGRESS",
                    response_json=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            try:
                session.commit()
                return CommandClaim(scope, key_hash, request_hash)
            except IntegrityError:
                session.rollback()
            record = session.scalar(
                select(CommandReceiptRecord).where(
                    CommandReceiptRecord.scope == scope,
                    CommandReceiptRecord.key_hash == key_hash,
                )
            )
            if record is None:
                raise RuntimeError("command receipt conflict disappeared")
            if record.request_hash != request_hash:
                raise CommandConflict("IDEMPOTENCY_KEY_REUSED")
            if record.state != "COMPLETED" or record.response_json is None:
                raise CommandConflict("COMMAND_OUTCOME_UNKNOWN")
            replay: Any = json.loads(record.response_json)
            if not isinstance(replay, dict):
                raise RuntimeError("command receipt response is invalid")
            return CommandClaim(scope, key_hash, request_hash, replay)

    def complete(self, claim: CommandClaim, response: Mapping[str, Any]) -> None:
        rendered = json.dumps(response, sort_keys=True, separators=(",", ":"))
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with self._sessions() as session:
            changed = session.scalar(
                update(CommandReceiptRecord)
                .where(
                    CommandReceiptRecord.scope == claim.scope,
                    CommandReceiptRecord.key_hash == claim.key_hash,
                    CommandReceiptRecord.request_hash == claim.request_hash,
                    CommandReceiptRecord.state == "IN_PROGRESS",
                )
                .values(state="COMPLETED", response_json=rendered, updated_at=now)
                .returning(CommandReceiptRecord.id)
            )
            if changed is None:
                session.rollback()
                raise RuntimeError("command receipt completion lost its claim")
            session.commit()

    def release(self, claim: CommandClaim) -> None:
        with self._sessions() as session:
            session.execute(
                delete(CommandReceiptRecord).where(
                    CommandReceiptRecord.scope == claim.scope,
                    CommandReceiptRecord.key_hash == claim.key_hash,
                    CommandReceiptRecord.request_hash == claim.request_hash,
                    CommandReceiptRecord.state == "IN_PROGRESS",
                )
            )
            session.commit()
