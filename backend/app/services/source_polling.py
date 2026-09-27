"""F20 multi-source Alertmanager polling and HA endpoint merge."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol

from sqlalchemy import Engine, delete, inspect, update
from sqlmodel import Session, select

from app.master_key import master_key
from app.crypto import SecretBox, SecretError
from app.registry_models import (
    AlertEndpointObservation,
    AlertmanagerEndpointRevision,
    EndpointPollResult,
    EventSource,
    EventSourceRevision,
    SourcePollRun,
)
from app.models import Alert
from app.runtime_config import runtime_config_provider
from app.services.ingest import ingest_alerts
from app.services.normalizer import alert_identity
from app.services.watchdog import is_watchdog_alert, record_watchdog_observations
from app.sources.alertmanager import (
    AlertmanagerEndpointClient,
    AlertmanagerEndpointRequest,
)

POLL_AUDIT_RETENTION_PER_SOURCE = 200
DEFAULT_MAX_CONCURRENT_ENDPOINT_POLLS = 8


@dataclass(frozen=True)
class EndpointRuntimeSnapshot:
    source_id: str
    source_name: str
    source_revision_id: int
    endpoint_revision_id: int
    position: int
    canonical_url: str
    auth_kind: str
    username: str
    secret: str = field(default="", repr=False)
    timeout_seconds: float = 10.0

    def to_alertmanager_request(self) -> AlertmanagerEndpointRequest:
        return AlertmanagerEndpointRequest(
            base_url=self.canonical_url,
            auth_kind=self.auth_kind,
            username=self.username,
            secret=self.secret,
            timeout_seconds=self.timeout_seconds,
        )


@dataclass(frozen=True)
class SourceRuntimeSnapshot:
    source_id: str
    source_name: str
    source_version: int
    source_revision_id: int
    lifecycle_state: str
    poll_interval_seconds: int
    resolution_grace_seconds: int
    max_parallel_endpoints: int
    environment: str
    endpoints: tuple[EndpointRuntimeSnapshot, ...]
    watchdog_enabled: bool = False
    watchdog_alertname: str = "Watchdog"
    watchdog_identity_label: str = "cluster"
    watchdog_missing_after_seconds: int = 90
    snapshot_error_code: str | None = None


@dataclass(frozen=True)
class EndpointPollOutcome:
    endpoint: EndpointRuntimeSnapshot
    status: str
    alerts: tuple[dict[str, Any], ...] = ()
    duration_ms: int = 0
    safe_error_code: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "SUCCESS" and self.safe_error_code is None


@dataclass(frozen=True)
class MergedAlert:
    identity: str
    raw: dict[str, Any]
    endpoint_revision_ids: tuple[int, ...]


@dataclass(frozen=True)
class MergedEndpointPollResult:
    completeness: str
    alerts: tuple[MergedAlert, ...]
    endpoint_outcomes: tuple[EndpointPollOutcome, ...]
    safe_error_codes: tuple[str, ...]


@dataclass(frozen=True)
class SourcePollOutcome:
    source_id: str
    completeness: str
    committed: bool
    alerts_seen: int = 0
    error_code: str | None = None


class EndpointReader(Protocol):
    async def fetch(self, endpoint: EndpointRuntimeSnapshot) -> EndpointPollOutcome: ...


class HTTPAlertmanagerEndpointReader:
    def __init__(self, client: AlertmanagerEndpointClient | None = None) -> None:
        self.client = client or AlertmanagerEndpointClient()

    async def fetch(self, endpoint: EndpointRuntimeSnapshot) -> EndpointPollOutcome:
        result = await self.client.fetch_alerts(endpoint.to_alertmanager_request())
        return EndpointPollOutcome(
            endpoint=endpoint,
            status=result.status,
            alerts=result.alerts,
            duration_ms=result.duration_ms,
            safe_error_code=result.safe_error_code,
        )


def endpoint_identity(raw_alert: dict[str, Any]) -> str:
    return alert_identity(raw_alert)


def _parse_alert_time(value: Any) -> float:
    if not isinstance(value, str) or not value.strip():
        return 0.0
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).timestamp()


def _payload_key(raw_alert: dict[str, Any]) -> str:
    return json.dumps(raw_alert, sort_keys=True, separators=(",", ":"), default=str)


def _choice_key(raw_alert: dict[str, Any], endpoint_position: int) -> tuple[float, int]:
    timestamp = _parse_alert_time(raw_alert.get("updatedAt")) or _parse_alert_time(
        raw_alert.get("startsAt")
    )
    return timestamp, -endpoint_position


def merge_endpoint_outcomes(
    outcomes: list[EndpointPollOutcome] | tuple[EndpointPollOutcome, ...],
) -> MergedEndpointPollResult:
    """Merge one source's endpoint results by upstream identity."""

    endpoint_outcomes = tuple(outcomes)
    successful = [item for item in endpoint_outcomes if item.ok]
    if successful and len(successful) == len(endpoint_outcomes):
        completeness = "COMPLETE"
    elif successful:
        completeness = "PARTIAL"
    else:
        completeness = "FAILED"

    records: dict[str, dict[str, Any]] = {}
    safe_codes = {
        item.safe_error_code
        for item in endpoint_outcomes
        if item.safe_error_code is not None
    }
    for outcome in successful:
        for raw in outcome.alerts:
            identity = endpoint_identity(raw)
            payload_key = _payload_key(raw)
            choice_key = _choice_key(raw, outcome.endpoint.position)
            existing = records.get(identity)
            if existing is None:
                records[identity] = {
                    "raw": raw,
                    "payload_key": payload_key,
                    "choice_key": choice_key,
                    "endpoint_ids": {outcome.endpoint.endpoint_revision_id},
                }
                continue
            existing["endpoint_ids"].add(outcome.endpoint.endpoint_revision_id)
            if existing["payload_key"] != payload_key:
                safe_codes.add("PAYLOAD_DIVERGENCE")
                if choice_key > existing["choice_key"]:
                    existing["raw"] = raw
                    existing["payload_key"] = payload_key
                    existing["choice_key"] = choice_key

    if completeness == "PARTIAL":
        safe_codes.add("PARTIAL_POLL")
    elif completeness == "FAILED":
        safe_codes.add("ALL_ENDPOINTS_FAILED")

    merged_alerts = tuple(
        MergedAlert(
            identity=identity,
            raw=record["raw"],
            endpoint_revision_ids=tuple(sorted(record["endpoint_ids"])),
        )
        for identity, record in sorted(records.items())
    )
    return MergedEndpointPollResult(
        completeness=completeness,
        alerts=merged_alerts,
        endpoint_outcomes=endpoint_outcomes,
        safe_error_codes=tuple(sorted(safe_codes)),
    )


def _f20_tables_available(session: Session) -> bool:
    # Inspect the session's own connection, never the engine: `inspect(engine)`
    # checks a connection out of the pool, which under StaticPool is the very
    # one this session is using, and returning it rolls back whatever the
    # caller had flushed.
    return bool(inspect(session.connection()).has_table("eventsource"))


def has_enabled_event_sources(session: Session) -> bool:
    if not _f20_tables_available(session):
        return False
    return (
        session.exec(
            select(EventSource.id).where(EventSource.lifecycle_state == "ENABLED")
        ).first()
        is not None
    )


def load_enabled_source_snapshots(
    session: Session, *, box: SecretBox | None = None
) -> list[SourceRuntimeSnapshot]:
    if not _f20_tables_available(session):
        return []
    environment = runtime_config_provider.snapshot().environment
    snapshots: list[SourceRuntimeSnapshot] = []
    sources = session.exec(
        select(EventSource)
        .where(EventSource.lifecycle_state == "ENABLED")
        .order_by(EventSource.name, EventSource.id)
    ).all()
    for source in sources:
        if source.active_revision_id is None:
            continue
        revision = session.get(EventSourceRevision, source.active_revision_id)
        if revision is None or revision.internal_state != "ACTIVE":
            continue
        endpoint_rows = session.exec(
            select(AlertmanagerEndpointRevision)
            .where(
                AlertmanagerEndpointRevision.source_revision_id == revision.id,
                AlertmanagerEndpointRevision.enabled.is_(True),
            )
            .order_by(AlertmanagerEndpointRevision.position)
        ).all()
        endpoints: list[EndpointRuntimeSnapshot] = []
        snapshot_error: str | None = None
        secret_box: SecretBox | None = box
        try:
            for endpoint in endpoint_rows:
                secret = ""
                if endpoint.auth_kind != "NONE":
                    secret_box = secret_box or SecretBox(master_key())
                    secret = secret_box.decrypt(endpoint.secret_envelope_json)
                endpoints.append(
                    EndpointRuntimeSnapshot(
                        source_id=source.id,
                        source_name=source.name,
                        source_revision_id=int(revision.id),
                        endpoint_revision_id=int(endpoint.id),
                        position=endpoint.position,
                        canonical_url=endpoint.canonical_url,
                        auth_kind=endpoint.auth_kind,
                        username=endpoint.username,
                        secret=secret,
                    )
                )
        except SecretError:
            snapshot_error = "SECRET_UNAVAILABLE"
            endpoints = []
        snapshots.append(
            SourceRuntimeSnapshot(
                source_id=source.id,
                source_name=source.name,
                source_version=source.version,
                source_revision_id=int(revision.id),
                lifecycle_state=source.lifecycle_state,
                poll_interval_seconds=revision.poll_interval_seconds,
                resolution_grace_seconds=revision.resolution_grace_seconds,
                max_parallel_endpoints=revision.max_parallel_endpoints,
                environment=environment,
                endpoints=tuple(endpoints),
                watchdog_enabled=revision.watchdog_enabled,
                watchdog_alertname=revision.watchdog_alertname,
                watchdog_identity_label=revision.watchdog_identity_label,
                watchdog_missing_after_seconds=revision.watchdog_missing_after_seconds,
                snapshot_error_code=snapshot_error,
            )
        )
    return snapshots


class PollCoordinator:
    """Poll enabled EventSources with source-level locks and endpoint budget."""

    def __init__(
        self,
        engine: Engine | Callable[[], Engine],
        *,
        endpoint_reader: EndpointReader | None = None,
        max_concurrent_endpoint_polls: int = DEFAULT_MAX_CONCURRENT_ENDPOINT_POLLS,
        audit_retention_per_source: int = POLL_AUDIT_RETENTION_PER_SOURCE,
    ) -> None:
        self._engine = engine
        self._endpoint_reader = endpoint_reader or HTTPAlertmanagerEndpointReader()
        self._endpoint_budget = asyncio.Semaphore(
            max(1, int(max_concurrent_endpoint_polls))
        )
        self._audit_retention_per_source = max(1, int(audit_retention_per_source))
        self._source_locks: dict[str, asyncio.Lock] = {}

    def _get_engine(self) -> Engine:
        return self._engine() if callable(self._engine) else self._engine

    async def poll_enabled_sources(
        self, *, now: datetime | None = None
    ) -> list[SourcePollOutcome]:
        poll_time = now or datetime.now(timezone.utc)
        with Session(self._get_engine()) as session:
            snapshots = load_enabled_source_snapshots(session)
            snapshots = [
                snapshot
                for snapshot in snapshots
                if self._source_is_due(session, snapshot, poll_time)
            ]
        tasks = [
            self.poll_snapshot(snapshot, now=poll_time) for snapshot in snapshots
        ]
        if not tasks:
            return []
        return list(await asyncio.gather(*tasks))

    @staticmethod
    def _source_is_due(
        session: Session,
        snapshot: SourceRuntimeSnapshot,
        now: datetime,
    ) -> bool:
        last_started_at = session.exec(
            select(SourcePollRun.started_at)
            .where(SourcePollRun.source_id == snapshot.source_id)
            .order_by(SourcePollRun.started_at.desc(), SourcePollRun.id.desc())
        ).first()
        if last_started_at is None:
            return True
        if last_started_at.tzinfo is None:
            last_started_at = last_started_at.replace(tzinfo=timezone.utc)
        else:
            last_started_at = last_started_at.astimezone(timezone.utc)
        now_utc = (
            now.replace(tzinfo=timezone.utc)
            if now.tzinfo is None
            else now.astimezone(timezone.utc)
        )
        return now_utc >= last_started_at + timedelta(
            seconds=max(5, snapshot.poll_interval_seconds)
        )

    async def poll_source_id(
        self, source_id: str, *, now: datetime | None = None
    ) -> SourcePollOutcome:
        with Session(self._get_engine()) as session:
            snapshots = [
                item
                for item in load_enabled_source_snapshots(session)
                if item.source_id == source_id
            ]
        if not snapshots:
            return SourcePollOutcome(
                source_id=source_id,
                completeness="FAILED",
                committed=False,
                error_code="SOURCE_NOT_POLLABLE",
            )
        return await self.poll_snapshot(snapshots[0], now=now)

    async def poll_snapshot(
        self, snapshot: SourceRuntimeSnapshot, *, now: datetime | None = None
    ) -> SourcePollOutcome:
        lock = self._source_locks.setdefault(snapshot.source_id, asyncio.Lock())
        async with lock:
            return await self._poll_snapshot_unlocked(snapshot, now=now)

    async def _poll_snapshot_unlocked(
        self, snapshot: SourceRuntimeSnapshot, *, now: datetime | None
    ) -> SourcePollOutcome:
        poll_time = now or datetime.now(timezone.utc)
        if snapshot.snapshot_error_code is not None:
            result = MergedEndpointPollResult(
                completeness="FAILED",
                alerts=(),
                endpoint_outcomes=(),
                safe_error_codes=(
                    "ALL_ENDPOINTS_FAILED",
                    snapshot.snapshot_error_code,
                ),
            )
            return self._persist_poll_result(snapshot, result, poll_time)
        if not snapshot.endpoints:
            result = MergedEndpointPollResult(
                completeness="FAILED",
                alerts=(),
                endpoint_outcomes=(),
                safe_error_codes=("ALL_ENDPOINTS_FAILED", "NO_ENABLED_ENDPOINTS"),
            )
            return self._persist_poll_result(snapshot, result, poll_time)

        source_budget = asyncio.Semaphore(max(1, snapshot.max_parallel_endpoints))
        endpoint_outcomes = await asyncio.gather(
            *(
                self._fetch_endpoint(endpoint, source_budget)
                for endpoint in snapshot.endpoints
            )
        )
        return self._persist_poll_result(
            snapshot,
            merge_endpoint_outcomes(endpoint_outcomes),
            poll_time,
        )

    async def _fetch_endpoint(
        self,
        endpoint: EndpointRuntimeSnapshot,
        source_budget: asyncio.Semaphore,
    ) -> EndpointPollOutcome:
        async with source_budget:
            async with self._endpoint_budget:
                try:
                    return await self._endpoint_reader.fetch(endpoint)
                except asyncio.TimeoutError:
                    return EndpointPollOutcome(
                        endpoint=endpoint,
                        status="TIMEOUT",
                        safe_error_code="ENDPOINT_TIMEOUT",
                    )
                except Exception:
                    return EndpointPollOutcome(
                        endpoint=endpoint,
                        status="NETWORK",
                        safe_error_code="ENDPOINT_NETWORK",
                    )

    def _persist_poll_result(
        self,
        snapshot: SourceRuntimeSnapshot,
        result: MergedEndpointPollResult,
        poll_time: datetime,
    ) -> SourcePollOutcome:
        finished_at = datetime.now(timezone.utc)
        with Session(self._get_engine()) as session:
            try:
                # Claim the exact source version with a no-op UPDATE. On SQLite
                # this acquires the writer lock: either a save/disable already
                # won (rowcount 0), or it runs after this poll commits and its
                # lifecycle change remains authoritative.
                claim = session.execute(
                    update(EventSource)
                    .where(
                        EventSource.id == snapshot.source_id,
                        EventSource.lifecycle_state == "ENABLED",
                        EventSource.version == snapshot.source_version,
                        EventSource.active_revision_id
                        == snapshot.source_revision_id,
                    )
                    .values(updated_at=EventSource.updated_at)
                    .execution_options(synchronize_session=False)
                )
                if claim.rowcount != 1:
                    session.rollback()
                    return SourcePollOutcome(
                        source_id=snapshot.source_id,
                        completeness=result.completeness,
                        committed=False,
                        alerts_seen=len(result.alerts),
                        error_code="SOURCE_CONFIG_CHANGED_DURING_POLL",
                    )
                run = SourcePollRun(
                    source_id=snapshot.source_id,
                    source_revision_id=snapshot.source_revision_id,
                    started_at=poll_time,
                    finished_at=finished_at,
                    completeness=result.completeness,
                    endpoint_total=len(result.endpoint_outcomes),
                    endpoint_succeeded=sum(
                        1 for item in result.endpoint_outcomes if item.ok
                    ),
                    endpoint_failed=sum(
                        1 for item in result.endpoint_outcomes if not item.ok
                    ),
                    normalized_alert_count=len(result.alerts),
                    safe_error_codes=list(result.safe_error_codes),
                )
                session.add(run)
                session.flush()
                for outcome in result.endpoint_outcomes:
                    session.add(
                        EndpointPollResult(
                            poll_run_id=int(run.id),
                            endpoint_revision_id=outcome.endpoint.endpoint_revision_id,
                            status=outcome.status,
                            alert_count=len(outcome.alerts) if outcome.ok else 0,
                            duration_ms=outcome.duration_ms,
                            safe_error_code=outcome.safe_error_code,
                        )
                    )
                if result.completeness != "FAILED":
                    raw_alerts = [item.raw for item in result.alerts]
                    watchdog_alerts = [
                        raw
                        for raw in raw_alerts
                        if is_watchdog_alert(raw, snapshot.watchdog_alertname)
                    ]
                    incident_alerts = [
                        raw
                        for raw in raw_alerts
                        if not is_watchdog_alert(raw, snapshot.watchdog_alertname)
                    ]
                    record_watchdog_observations(
                        session,
                        source_id=snapshot.source_id,
                        watchdog_enabled=snapshot.watchdog_enabled,
                        watchdog_alertname=snapshot.watchdog_alertname,
                        watchdog_identity_label=snapshot.watchdog_identity_label,
                        raw_alerts=watchdog_alerts,
                        observed_at=poll_time,
                    )
                    ingest_alerts(
                        session,
                        incident_alerts,
                        poll_time=poll_time,
                        resolution_grace_seconds=snapshot.resolution_grace_seconds,
                        reconcile_lifecycle=result.completeness == "COMPLETE",
                        source_id=snapshot.source_id,
                        environment=snapshot.environment,
                    )
                    self._record_endpoint_observations(
                        session, snapshot, result, poll_time
                    )
                self._prune_poll_audit(session, snapshot.source_id)
                session.commit()
                return SourcePollOutcome(
                    source_id=snapshot.source_id,
                    completeness=result.completeness,
                    committed=True,
                    alerts_seen=len(result.alerts),
                )
            except Exception:
                session.rollback()
                return SourcePollOutcome(
                    source_id=snapshot.source_id,
                    completeness=result.completeness,
                    committed=False,
                    alerts_seen=len(result.alerts),
                    error_code="DB_TRANSACTION_FAILED",
                )

    def _record_endpoint_observations(
        self,
        session: Session,
        snapshot: SourceRuntimeSnapshot,
        result: MergedEndpointPollResult,
        observed_at: datetime,
    ) -> None:
        identities = [item.identity for item in result.alerts]
        if not identities:
            return
        stored = session.exec(
            select(Alert).where(
                Alert.source_id == snapshot.source_id,
                Alert.upstream_fingerprint.in_(identities),
            )
        ).all()
        by_identity = {item.upstream_fingerprint: item for item in stored}
        for merged in result.alerts:
            alert = by_identity.get(merged.identity)
            if alert is None or alert.id is None:
                continue
            for endpoint_revision_id in merged.endpoint_revision_ids:
                observation = session.exec(
                    select(AlertEndpointObservation).where(
                        AlertEndpointObservation.alert_id == alert.id,
                        AlertEndpointObservation.endpoint_revision_id
                        == endpoint_revision_id,
                    )
                ).first()
                if observation is None:
                    observation = AlertEndpointObservation(
                        alert_id=int(alert.id),
                        endpoint_revision_id=endpoint_revision_id,
                        last_seen_at=observed_at,
                    )
                else:
                    observation.last_seen_at = observed_at
                session.add(observation)

    def _prune_poll_audit(self, session: Session, source_id: str) -> None:
        runs = session.exec(
            select(SourcePollRun.id)
            .where(SourcePollRun.source_id == source_id)
            .order_by(SourcePollRun.started_at.desc(), SourcePollRun.id.desc())
        ).all()
        stale_ids = [int(item) for item in runs[self._audit_retention_per_source :]]
        if not stale_ids:
            return
        session.exec(
            delete(EndpointPollResult).where(
                EndpointPollResult.poll_run_id.in_(stale_ids)
            )
        )
        session.exec(delete(SourcePollRun).where(SourcePollRun.id.in_(stale_ids)))
