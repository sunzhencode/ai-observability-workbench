"""SQLAlchemy adapter for durable Jobs and their resumable event stream."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import DateTime, Index, Integer, String, Text, UniqueConstraint, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app.domains.operations.jobs import JobPool, JobSpec, JobState, JobView
from app.domains.operations.events import PlatformEvent
from app.platform.persistence.database import SessionFactory
from app.platform.persistence.codecs import aware_utc as _utc, stored_utc as _stored

UTC = timezone.utc


class Base(DeclarativeBase):
    pass


class JobRecord(Base):
    __tablename__ = "platform_job"
    __table_args__ = (
        UniqueConstraint("kind", "idempotency_key", name="uq_platform_job_kind_key"),
        Index("ix_platform_job_claim", "pool", "state", "available_at", "created_at"),
        Index("ix_platform_job_lease", "state", "lease_expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(80), nullable=False)
    pool: Mapped[str] = mapped_column(String(24), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(80), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    safe_error_code: Mapped[str | None] = mapped_column(String(96))
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class EventRecord(Base):
    __tablename__ = "platform_event"
    __table_args__ = (Index("ix_platform_event_type_sequence", "event_type", "sequence"),)

    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(96), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(80), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class JobRepository:
    """Persistence-only operations; application services own transactions."""

    def enqueue(self, session: Session, spec: JobSpec, *, now: datetime) -> JobView:
        job_id = str(uuid4())
        available_at = spec.available_at or now
        values: dict[str, Any] = {
            "id": job_id,
            "kind": spec.kind,
            "pool": spec.pool.value,
            "subject_type": spec.subject_type,
            "subject_id": spec.subject_id,
            "state": JobState.PENDING.value,
            "payload_json": json.dumps(spec.payload, sort_keys=True, separators=(",", ":")),
            "payload_revision": spec.payload_revision,
            "idempotency_key": spec.idempotency_key,
            "attempt": 0,
            "available_at": _stored(available_at),
            "lease_owner": None,
            "lease_expires_at": None,
            "started_at": None,
            "finished_at": None,
            "safe_error_code": None,
            "version": 1,
            "created_at": _stored(now),
            "updated_at": _stored(now),
        }
        inserted_id = session.scalar(
            sqlite_insert(JobRecord)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["kind", "idempotency_key"])
            .returning(JobRecord.id)
        )
        inserted = inserted_id is not None
        if inserted:
            self.append_event(
                session,
                event_type="job.pending",
                subject_type="job",
                subject_id=job_id,
                now=now,
            )
        record = session.scalar(
            select(JobRecord).where(
                JobRecord.kind == spec.kind,
                JobRecord.idempotency_key == spec.idempotency_key,
            )
        )
        if record is None:
            raise RuntimeError("Job enqueue did not produce a durable record")
        return self.to_view(record)

    def get(self, session: Session, job_id: str) -> JobView | None:
        record = session.get(JobRecord, job_id)
        return None if record is None else self.to_view(record)

    def count(self, session: Session, state: JobState | None) -> int:
        statement = select(JobRecord.id)
        if state is not None:
            statement = statement.where(JobRecord.state == state.value)
        return len(session.scalars(statement).all())

    def claim_next(
        self,
        session: Session,
        *,
        pool: JobPool,
        lease_owner: str,
        now: datetime,
        lease_expires_at: datetime,
    ) -> JobView | None:
        candidate = (
            select(JobRecord.id)
            .where(
                JobRecord.pool == pool.value,
                JobRecord.state == JobState.PENDING.value,
                JobRecord.available_at <= _stored(now),
            )
            .order_by(JobRecord.available_at, JobRecord.created_at, JobRecord.id)
            .limit(1)
            .scalar_subquery()
        )
        claimed_id = session.scalar(
            update(JobRecord)
            .where(JobRecord.id == candidate, JobRecord.state == JobState.PENDING.value)
            .values(
                state=JobState.RUNNING.value,
                attempt=JobRecord.attempt + 1,
                lease_owner=lease_owner,
                lease_expires_at=_stored(lease_expires_at),
                started_at=_stored(now),
                updated_at=_stored(now),
                version=JobRecord.version + 1,
            )
            .returning(JobRecord.id)
        )
        if claimed_id is None:
            return None
        self.append_event(session, "job.running", "job", claimed_id, now)
        record = session.get(JobRecord, claimed_id)
        if record is None:
            raise RuntimeError("claimed Job disappeared")
        return self.to_view(record)

    def finish(
        self,
        session: Session,
        *,
        job_id: str,
        lease_owner: str,
        state: JobState,
        now: datetime,
        safe_error_code: str | None,
    ) -> JobView | None:
        updated_id = session.scalar(
            update(JobRecord)
            .where(
                JobRecord.id == job_id,
                JobRecord.state == JobState.RUNNING.value,
                JobRecord.lease_owner == lease_owner,
            )
            .values(
                state=state.value,
                lease_owner=None,
                lease_expires_at=None,
                finished_at=_stored(now),
                safe_error_code=safe_error_code,
                updated_at=_stored(now),
                version=JobRecord.version + 1,
            )
            .returning(JobRecord.id)
        )
        if updated_id is None:
            return None
        self.append_event(session, f"job.{state.value.lower()}", "job", job_id, now)
        record = session.get(JobRecord, job_id)
        if record is None:
            raise RuntimeError("finished Job disappeared")
        return self.to_view(record)

    def expired(self, session: Session, *, now: datetime) -> list[JobView]:
        records = session.scalars(
            select(JobRecord).where(
                JobRecord.state == JobState.RUNNING.value,
                JobRecord.lease_expires_at.is_not(None),
                JobRecord.lease_expires_at < _stored(now),
            )
        ).all()
        return [self.to_view(record) for record in records]

    def reap(
        self,
        session: Session,
        *,
        job: JobView,
        requeue: bool,
        now: datetime,
    ) -> bool:
        values: dict[str, Any]
        if requeue:
            values = {
                "state": JobState.PENDING.value,
                "lease_owner": None,
                "lease_expires_at": None,
                "safe_error_code": None,
                "updated_at": _stored(now),
                "version": JobRecord.version + 1,
            }
            event_type = "job.pending"
        else:
            values = {
                "state": JobState.FAILED.value,
                "lease_owner": None,
                "lease_expires_at": None,
                "finished_at": _stored(now),
                "safe_error_code": "JOB_LEASE_EXPIRED",
                "updated_at": _stored(now),
                "version": JobRecord.version + 1,
            }
            event_type = "job.failed"
        updated_id = session.scalar(
            update(JobRecord)
            .where(
                JobRecord.id == job.id,
                JobRecord.state == JobState.RUNNING.value,
                JobRecord.version == job.version,
                JobRecord.lease_expires_at < _stored(now),
            )
            .values(**values)
            .returning(JobRecord.id)
        )
        if updated_id is None:
            return False
        self.append_event(session, event_type, "job", job.id, now)
        return True

    def append_event(
        self,
        session: Session,
        event_type: str,
        subject_type: str,
        subject_id: str,
        now: datetime,
    ) -> None:
        session.add(
            EventRecord(
                event_type=event_type,
                subject_type=subject_type,
                subject_id=subject_id,
                created_at=_stored(now),
            )
        )

    def list_events(
        self,
        session: Session,
        *,
        after: int,
        event_types: tuple[str, ...],
        limit: int,
    ) -> list[EventRecord]:
        statement = select(EventRecord).where(EventRecord.sequence > after)
        if event_types:
            statement = statement.where(EventRecord.event_type.in_(event_types))
        return list(
            session.scalars(statement.order_by(EventRecord.sequence).limit(limit)).all()
        )

    @staticmethod
    def to_view(record: JobRecord) -> JobView:
        payload = json.loads(record.payload_json)
        if not isinstance(payload, dict):
            raise RuntimeError("Job payload must be a JSON object")
        available_at = _utc(record.available_at)
        created_at = _utc(record.created_at)
        updated_at = _utc(record.updated_at)
        if available_at is None or created_at is None or updated_at is None:
            raise RuntimeError("required Job timestamp is missing")
        return JobView(
            id=record.id,
            kind=record.kind,
            pool=JobPool(record.pool),
            subject_type=record.subject_type,
            subject_id=record.subject_id,
            state=JobState(record.state),
            payload=payload,
            payload_revision=record.payload_revision,
            idempotency_key=record.idempotency_key,
            attempt=record.attempt,
            available_at=available_at,
            lease_owner=record.lease_owner,
            lease_expires_at=_utc(record.lease_expires_at),
            started_at=_utc(record.started_at),
            finished_at=_utc(record.finished_at),
            safe_error_code=record.safe_error_code,
            version=record.version,
            created_at=created_at,
            updated_at=updated_at,
        )


class SqlAlchemyJobStore:
    """Transaction-owning adapter implementing the application Job/Event ports."""

    def __init__(
        self,
        session_factory: SessionFactory,
        repository: JobRepository | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._repository = repository or JobRepository()

    def enqueue(self, spec: JobSpec, *, now: datetime) -> JobView:
        with self._session_factory() as session, session.begin():
            return self._repository.enqueue(session, spec, now=now)

    def get(self, job_id: str) -> JobView | None:
        with self._session_factory() as session:
            return self._repository.get(session, job_id)

    def count(self, state: JobState | None) -> int:
        with self._session_factory() as session:
            return self._repository.count(session, state)

    def claim_next(
        self,
        *,
        pool: JobPool,
        lease_owner: str,
        now: datetime,
        lease_expires_at: datetime,
    ) -> JobView | None:
        with self._session_factory() as session, session.begin():
            return self._repository.claim_next(
                session,
                pool=pool,
                lease_owner=lease_owner,
                now=now,
                lease_expires_at=lease_expires_at,
            )

    def finish(
        self,
        *,
        job_id: str,
        lease_owner: str,
        state: JobState,
        now: datetime,
        safe_error_code: str | None,
    ) -> JobView | None:
        with self._session_factory() as session, session.begin():
            return self._repository.finish(
                session,
                job_id=job_id,
                lease_owner=lease_owner,
                state=state,
                now=now,
                safe_error_code=safe_error_code,
            )

    def expired(self, *, now: datetime) -> tuple[JobView, ...]:
        with self._session_factory() as session:
            return tuple(self._repository.expired(session, now=now))

    def reap(self, *, job: JobView, requeue: bool, now: datetime) -> bool:
        with self._session_factory() as session, session.begin():
            return self._repository.reap(
                session,
                job=job,
                requeue=requeue,
                now=now,
            )

    def list_events(
        self,
        *,
        after: int,
        event_types: tuple[str, ...],
        limit: int,
    ) -> tuple[PlatformEvent, ...]:
        with self._session_factory() as session:
            records = self._repository.list_events(
                session,
                after=after,
                event_types=event_types,
                limit=limit,
            )
            return tuple(
                PlatformEvent(
                    sequence=record.sequence,
                    event_type=record.event_type,
                    subject_type=record.subject_type,
                    subject_id=record.subject_id,
                    created_at=_utc(record.created_at) or record.created_at.replace(tzinfo=UTC),
                )
                for record in records
            )
