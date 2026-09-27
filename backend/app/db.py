"""SQLite database engine and session helpers."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import Engine, event
from sqlmodel import Session, create_engine

from app.config import settings
from app.migrations import run_migrations

_engine = None


def create_sqlite_engine(path: str | Path) -> Engine:
    """Create one SQLite engine with the required per-connection PRAGMAs."""
    path = Path(path)
    if path.parent and str(path.parent) not in ("", "."):
        path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _configure_sqlite(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
        finally:
            cursor.close()

    return engine


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_sqlite_engine(settings.database_path)
    return _engine


def init_db() -> None:
    """Create tables. Imports models so they register on SQLModel.metadata."""
    from app import models  # noqa: F401

    engine = get_engine()
    report = run_migrations(engine, database_path=Path(settings.database_path))
    _seed_metric_templates(engine)
    if 5 in report.applied_versions:
        # Migration 5 already assigns v2 keys and resolves collisions without
        # entering regroup/Planner paths.  Preserve that non-live migration
        # boundary for the upgrade startup.
        return
    from app.services.aggregation_rules import regroup_all_with_aggregation_rules

    with Session(engine) as session:
        regroup_all_with_aggregation_rules(session)


def _seed_metric_templates(engine: Engine) -> None:
    """Refresh the shipped F27 template catalogue (idempotent).

    Runs here rather than inside migration 11 on purpose: these queries have to
    stay correctable as they meet real clusters, and an applied migration's
    checksum is frozen the moment it lands anywhere. Rows the user has edited and
    the enabled/disabled switch are never touched.

    Deliberately *before* the migration-5 early return: seeding is independent of
    the regroup boundary, and an upgrading database should still get the
    catalogue.
    """
    from app.services.metric_templates import seed_builtin_templates

    with Session(engine) as session:
        seed_builtin_templates(session)
        session.commit()


def ensure_compatible_schema(engine: Engine) -> None:
    """Backward-compatible entrypoint now backed by the versioned runner."""
    run_migrations(engine)


def get_session() -> Iterator[Session]:
    with Session(get_engine()) as session:
        yield session
