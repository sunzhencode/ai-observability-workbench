"""SQLAlchemy 2 persistence and unit-of-work contracts."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import ForeignKey, Integer, String, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.platform.persistence.database import (
    SqliteDatabaseConfig,
    create_session_factory,
    create_sqlite_engine,
)
from app.platform.persistence.unit_of_work import SqlAlchemyUnitOfWork


class Base(DeclarativeBase):
    pass


class Parent(Base):
    __tablename__ = "uow_parent"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)


class Child(Base):
    __tablename__ = "uow_child"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parent_id: Mapped[int] = mapped_column(ForeignKey("uow_parent.id"))


def _engine(path: Path, *, busy_timeout_ms: int = 5_000):
    return create_sqlite_engine(
        SqliteDatabaseConfig(path=path, busy_timeout_ms=busy_timeout_ms)
    )


def test_every_connection_enables_required_sqlite_pragmas(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "incident-operations.db", busy_timeout_ms=321)

    with engine.connect() as connection:
        journal_mode = connection.execute(text("PRAGMA journal_mode")).scalar_one()
        foreign_keys = connection.execute(text("PRAGMA foreign_keys")).scalar_one()
        busy_timeout = connection.execute(text("PRAGMA busy_timeout")).scalar_one()

    assert str(journal_mode).lower() == "wal"
    assert foreign_keys == 1
    assert busy_timeout == 321


def test_foreign_keys_are_enforced_on_new_sessions(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "incident-operations.db")
    Base.metadata.create_all(engine)
    sessions = create_session_factory(engine)

    with pytest.raises(IntegrityError):
        with SqlAlchemyUnitOfWork(sessions) as uow:
            uow.session.add(Child(parent_id=999))
            uow.commit()


def test_unit_of_work_requires_explicit_commit(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "incident-operations.db")
    Base.metadata.create_all(engine)
    sessions = create_session_factory(engine)

    with SqlAlchemyUnitOfWork(sessions) as uow:
        uow.session.add(Parent(name="rolled-back"))

    with sessions() as session:
        assert session.query(Parent).all() == []

    with SqlAlchemyUnitOfWork(sessions) as uow:
        uow.session.add(Parent(name="committed"))
        uow.commit()

    with sessions() as session:
        assert [item.name for item in session.query(Parent).all()] == ["committed"]


def test_unit_of_work_rolls_back_the_whole_transaction_on_error(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path / "incident-operations.db")
    Base.metadata.create_all(engine)
    sessions = create_session_factory(engine)

    with pytest.raises(RuntimeError, match="stop before commit"):
        with SqlAlchemyUnitOfWork(sessions) as uow:
            uow.session.add_all([Parent(name="one"), Parent(name="two")])
            uow.session.flush()
            raise RuntimeError("stop before commit")

    with sessions() as session:
        assert session.query(Parent).all() == []


def test_busy_timeout_bounds_competing_writers(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "incident-operations.db", busy_timeout_ms=25)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE counter (value INTEGER NOT NULL)"))
        connection.execute(text("INSERT INTO counter (value) VALUES (0)"))

    first = engine.connect()
    second = engine.connect()
    first_transaction = first.begin()
    try:
        first.execute(text("UPDATE counter SET value = 1"))
        with pytest.raises(OperationalError, match="locked"):
            second.execute(text("UPDATE counter SET value = 2"))
    finally:
        second.rollback()
        second.close()
        first_transaction.rollback()
        first.close()
