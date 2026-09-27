"""Public use cases for source collection and deterministic apply."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Mapping, Protocol
from uuid import uuid4

from app.domains.sources.models import (
    CollectionOutcome,
    EndpointObservation,
    EndpointSnapshot,
    PollCompleteness,
    SourceSnapshot,
    SourceState,
    merge_endpoint_observations,
)
from app.domains.incidents.models import IncidentLifecycleSnapshot
from app.domains.operations.jobs import JobPool, JobSpec


@dataclass(frozen=True, slots=True)
class EndpointDraft:
    position: int
    canonical_url: str
    enabled: bool = True
    auth_kind: str = "NONE"
    username: str = ""
    secret_envelope: str | None = None
    secret_action: str = "KEEP"
    secret_value: str | None = field(default=None, repr=False)
    timeout_seconds: float = 10.0


@dataclass(frozen=True, slots=True)
class SourceDraft:
    id: str
    name: str
    endpoints: tuple[EndpointDraft, ...]
    poll_interval_seconds: int = 30
    resolution_grace_seconds: int = 60
    max_parallel_endpoints: int = 4
    watchdog_enabled: bool = False
    watchdog_alertname: str = "Watchdog"
    watchdog_identity_label: str = "cluster"
    watchdog_missing_after_seconds: int = 90


@dataclass(frozen=True, slots=True)
class ApplyResult:
    completeness: PollCompleteness
    committed: bool
    alerts_seen: int
    safe_error_code: str | None = None


@dataclass(frozen=True, slots=True)
class EndpointView:
    position: int
    canonical_url: str
    enabled: bool
    auth_kind: str
    username: str
    secret_configured: bool
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class SourceView:
    id: str
    name: str
    state: str
    version: int
    poll_interval_seconds: int
    resolution_grace_seconds: int
    max_parallel_endpoints: int
    watchdog_enabled: bool
    watchdog_alertname: str
    watchdog_identity_label: str
    watchdog_missing_after_seconds: int
    endpoints: tuple[EndpointView, ...]
    created_at: datetime
    updated_at: datetime
    last_poll_at: datetime | None
    last_poll_completeness: str | None
    last_poll_safe_error_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AlertView:
    id: int
    source_id: str
    upstream_fingerprint: str
    alertname: str
    severity: str
    cluster: str
    source_state: str
    incident_id: int
    labels: dict[str, str]
    annotations: dict[str, str]
    starts_at: datetime | None
    missing_since_at: datetime | None
    origin: str
    evidence_completeness: str
    last_seen_at: datetime


@dataclass(frozen=True, slots=True)
class IncidentView:
    id: int
    source_id: str
    group_key: str
    title: str
    severity: str
    source_state: str
    freshness_state: str
    handling_state: str
    handling_version: int
    occurrence_no: int
    occurrence_started_at: datetime
    updated_at: datetime
    member_count: int
    aggregation_rule_id: int | None
    source_name: str
    aggregation_rule_name: str | None
    aggregation_status: Literal["matched", "unmatched", "missing_labels"]


@dataclass(frozen=True, slots=True)
class RuleView:
    id: int
    name: str
    priority: int
    enabled: bool
    version: int
    matchers: tuple[tuple[str, str, str], ...]
    group_by_labels: tuple[str, ...]
    source_ids: tuple[str, ...]
    grouping_window_seconds: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class LabelCatalogItem:
    name: str
    coverage: float
    present_count: int
    total_count: int
    distinct_count: int
    sample_values: tuple[str, ...]
    sources: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RulePreview:
    matcher_alert_count: int
    selected_alert_count: int
    proposed_group_count: int
    groups: tuple[RulePreviewGroup, ...] = ()
    by_source: tuple[RulePreviewSource, ...] = ()


@dataclass(frozen=True, slots=True)
class RulePreviewGroup:
    source_id: str
    group_key: str
    fingerprints: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RulePreviewSource:
    source_id: str
    source_name: str
    matcher_alert_count: int
    selected_alert_count: int
    proposed_group_count: int


@dataclass(frozen=True, slots=True)
class SourceAuditView:
    sequence: int
    source_id: str
    action: str
    source_version: int
    changed_at: datetime


@dataclass(frozen=True, slots=True)
class WatchdogClusterView:
    id: int
    source_id: str
    identity_value: str
    inventory_state: str
    health_state: str
    last_observed_at: datetime | None


@dataclass(frozen=True, slots=True)
class DueSource:
    source_id: str
    source_version: int
    poll_interval_seconds: int


class EndpointReader(Protocol):
    async def fetch(self, endpoint: EndpointSnapshot) -> EndpointObservation: ...


class SourceStore(Protocol):
    def load_snapshot(self, source_id: str, *, expected_version: int) -> SourceSnapshot: ...

    def apply_collection(
        self,
        snapshot: SourceSnapshot,
        outcome: CollectionOutcome,
        *,
        observed_at: datetime,
    ) -> ApplyResult: ...


class SourceNoiseObserver(Protocol):
    def observe_complete_collection_in_session(
        self,
        session: Any,
        *,
        source_id: str,
        previous_alert_states: Mapping[str, str],
        previous_incidents: Mapping[int, IncidentLifecycleSnapshot],
        observed_at: datetime,
    ) -> None: ...


class SourceTestStore(Protocol):
    def load_test_snapshot(
        self, source_id: str, *, expected_version: int
    ) -> SourceSnapshot: ...

    def record_test_result(
        self,
        source_id: str,
        *,
        expected_version: int,
        completeness: PollCompleteness,
        tested_at: datetime,
    ) -> None: ...


class SourceSchedulePort(Protocol):
    def due_sources(self, *, now: datetime) -> tuple[DueSource, ...]: ...


def new_source_id() -> str:
    return f"src_{uuid4().hex}"


class SourceJobPlanner:
    """Read due configuration and return Jobs; APScheduler still only enqueues."""

    def __init__(self, port: SourceSchedulePort) -> None:
        self._port = port

    def plan(self, *, now: datetime) -> tuple[JobSpec, ...]:
        specs: list[JobSpec] = []
        for source in self._port.due_sources(now=now):
            bucket = int(now.timestamp()) // source.poll_interval_seconds
            specs.append(
                JobSpec(
                    kind="source.collect",
                    pool=JobPool.SOURCE,
                    subject_type="source",
                    subject_id=source.source_id,
                    payload={"expected_version": source.source_version},
                    payload_revision=1,
                    idempotency_key=(
                        f"scheduled:{source.source_id}:{source.source_version}:{bucket}"
                    ),
                )
            )
        return tuple(specs)


class SourceSlicePort(Protocol):
    def create_source(self, draft: SourceDraft, *, now: datetime) -> SourceView: ...

    def update_source(
        self,
        source_id: str,
        draft: SourceDraft,
        *,
        expected_version: int,
        now: datetime,
    ) -> SourceView: ...

    def set_source_state(
        self,
        source_id: str,
        *,
        target: SourceState,
        expected_version: int,
        now: datetime,
    ) -> SourceView: ...

    def add_expected_watchdog_cluster(
        self, source_id: str, identity_value: str, *, now: datetime
    ) -> None: ...

    def set_watchdog_inventory_state(
        self,
        source_id: str,
        identity_value: str,
        *,
        inventory_state: str,
    ) -> None: ...

    def list_sources(self, *, include_archived: bool = False) -> tuple[SourceView, ...]: ...

    def get_source(self, source_id: str) -> SourceView | None: ...

    def list_alerts(self, *, source_id: str | None = None) -> tuple[AlertView, ...]: ...

    def list_incidents(
        self, *, source_id: str | None = None
    ) -> tuple[IncidentView, ...]: ...

    def list_rules(self) -> tuple[RuleView, ...]: ...

    def label_catalog(
        self,
        historical_series: tuple[dict[str, str], ...] = (),
    ) -> tuple[LabelCatalogItem, ...]: ...

    def list_source_audit(self, source_id: str) -> tuple[SourceAuditView, ...]: ...

    def list_watchdog_clusters(
        self, source_id: str, *, now: datetime
    ) -> tuple[WatchdogClusterView, ...]: ...

    def preview_rule(
        self,
        *,
        rule_id: int | None = None,
        name: str,
        priority: int,
        enabled: bool,
        matchers: tuple[tuple[str, str, str], ...],
        group_by_labels: tuple[str, ...],
        source_ids: tuple[str, ...],
        grouping_window_seconds: int,
    ) -> RulePreview: ...

    def publish_rule(
        self,
        *,
        name: str,
        priority: int,
        enabled: bool,
        matchers: tuple[tuple[str, str, str], ...],
        group_by_labels: tuple[str, ...],
        source_ids: tuple[str, ...],
        grouping_window_seconds: int,
        now: datetime,
    ) -> RuleView: ...

    def update_rule(
        self,
        rule_id: int,
        *,
        expected_version: int,
        name: str,
        priority: int,
        enabled: bool,
        matchers: tuple[tuple[str, str, str], ...],
        group_by_labels: tuple[str, ...],
        source_ids: tuple[str, ...],
        grouping_window_seconds: int,
        now: datetime,
    ) -> RuleView: ...


class CollectSource:
    """Freeze configuration first, then perform bounded read-only I/O."""

    def __init__(self, store: SourceStore, reader: EndpointReader) -> None:
        self._store = store
        self._reader = reader

    async def execute(self, source_id: str, expected_version: int) -> CollectionOutcome:
        snapshot = self._store.load_snapshot(
            source_id,
            expected_version=expected_version,
        )
        return await self.collect_snapshot(snapshot)

    async def collect_snapshot(self, snapshot: SourceSnapshot) -> CollectionOutcome:
        return await _collect_snapshot(self._reader, snapshot)


async def _collect_snapshot(
    reader: EndpointReader,
    snapshot: SourceSnapshot,
) -> CollectionOutcome:
    budget = asyncio.Semaphore(max(1, snapshot.max_parallel_endpoints))

    async def fetch(endpoint: EndpointSnapshot) -> EndpointObservation:
        async with budget:
            try:
                return await reader.fetch(endpoint)
            except TimeoutError:
                return EndpointObservation(
                    endpoint=endpoint,
                    status="TIMEOUT",
                    alerts=(),
                    duration_ms=0,
                    safe_error_code="ENDPOINT_TIMEOUT",
                )
            except Exception:  # noqa: BLE001 - raw network errors never escape
                return EndpointObservation(
                    endpoint=endpoint,
                    status="NETWORK",
                    alerts=(),
                    duration_ms=0,
                    safe_error_code="ENDPOINT_NETWORK",
                )

    observations = await asyncio.gather(
        *(fetch(endpoint) for endpoint in snapshot.endpoints)
    )
    return merge_endpoint_observations(tuple(observations))


class TestSource:
    """Diagnostic read of current config; it never saves or changes lifecycle."""

    def __init__(self, store: SourceTestStore, reader: EndpointReader) -> None:
        self._store = store
        self._reader = reader

    async def execute(self, source_id: str, expected_version: int) -> CollectionOutcome:
        snapshot = self._store.load_test_snapshot(
            source_id,
            expected_version=expected_version,
        )
        outcome = await _collect_snapshot(self._reader, snapshot)
        self._store.record_test_result(
            source_id,
            expected_version=expected_version,
            completeness=outcome.completeness,
            tested_at=datetime.now(timezone.utc),
        )
        return outcome


class ApplyCollection:
    """Short version-fenced UoW after collection has completed."""

    def __init__(self, store: SourceStore) -> None:
        self._store = store

    def execute(
        self,
        snapshot: SourceSnapshot,
        outcome: CollectionOutcome,
        *,
        observed_at: datetime,
    ) -> ApplyResult:
        return self._store.apply_collection(
            snapshot,
            outcome,
            observed_at=observed_at,
        )
