"""Explicit short transaction boundary for Incident Operations application use cases."""

from __future__ import annotations

from types import TracebackType

from sqlalchemy.orm import Session

from app.platform.persistence.database import SessionFactory


class SqlAlchemyUnitOfWork:
    """A session that rolls back unless the use case explicitly commits."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory
        self._session: Session | None = None
        self._committed = False

    @property
    def session(self) -> Session:
        if self._session is None:
            raise RuntimeError("unit of work is not active")
        return self._session

    def __enter__(self) -> SqlAlchemyUnitOfWork:
        if self._session is not None:
            raise RuntimeError("unit of work cannot be re-entered")
        self._session = self._session_factory()
        self._committed = False
        return self

    def commit(self) -> None:
        self.session.commit()
        self._committed = True

    def rollback(self) -> None:
        self.session.rollback()
        self._committed = False

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        session = self.session
        try:
            if exception_type is not None or not self._committed:
                session.rollback()
        finally:
            session.close()
            self._session = None
            self._committed = False
