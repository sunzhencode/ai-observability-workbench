"""SQLAlchemy 2 engine and session construction for the separate Incident Operations store."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, URL, create_engine, event
from sqlalchemy.orm import Session, sessionmaker


@dataclass(frozen=True, slots=True)
class SqliteDatabaseConfig:
    path: Path
    busy_timeout_ms: int = 5_000

    def __post_init__(self) -> None:
        normalized = Path(self.path).expanduser().resolve()
        if normalized.exists() and not normalized.is_file():
            raise ValueError("SQLite database path must identify a file")
        if not 1 <= self.busy_timeout_ms <= 60_000:
            raise ValueError("busy_timeout_ms must be between 1 and 60000")
        object.__setattr__(self, "path", normalized)

    @property
    def url(self) -> URL:
        return URL.create("sqlite+pysqlite", database=str(self.path))


SessionFactory = sessionmaker[Session]


def create_sqlite_engine(config: SqliteDatabaseConfig) -> Engine:
    """Create an isolated Incident Operations engine with invariants on every connection."""
    config.path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        config.url,
        connect_args={
            "check_same_thread": False,
            "timeout": config.busy_timeout_ms / 1_000,
        },
        pool_pre_ping=True,
    )

    @event.listens_for(engine, "connect")
    def configure_connection(
        dbapi_connection: Any,
        _connection_record: Any,
    ) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute(f"PRAGMA busy_timeout={config.busy_timeout_ms}")
        finally:
            cursor.close()

    return engine


def create_session_factory(engine: Engine) -> SessionFactory:
    return sessionmaker(
        bind=engine,
        class_=Session,
        autoflush=False,
        expire_on_commit=False,
    )
