"""Use cases for the single V2 Incident Investigator deep module."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import re
from typing import Awaitable, Callable, Protocol

from app.application.incidents import IncidentStore
from app.application.investigator_runtime import (
    InvestigatorRequest,
    InvestigatorRuntime,
    InvestigatorUsageDelta,
    ReadOnlyMetricTools,
)
from app.application.observability import AlertMetricContext, ObservabilityPort, ThanosReadPort
from app.domains.incidents.actors import InteractiveOperatorActor, require_interactive_operator
from app.domains.investigations.planner import extract_catalog_metric_names
from app.domains.investigations.runtime import (
    EvidenceSnapshotV2,
    InvestigationActivityV2,
    InvestigationReportV2,
    InvestigationRunState,
    MetricDescriptorV2,
    MetricObservationV2,
)
from app.domains.metrics.models import (
    MetricReadStatus,
    QueryWindow,
    classify_read,
    expr_from_generator_url,
    render_metric_template,
    strip_numeric_comparison,
)
from app.domains.investigations.models import stable_alert_ref, summarize_series


UTC = timezone.utc
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9_.:/-]{1,128}$")
_MAX_ALERT_DETAILS = 20
_MAX_INITIAL_QUERIES = 6
_MAX_INITIAL_CONCURRENCY = 2
_INITIAL_READ_TIMEOUT_SECONDS = 20.0


@dataclass(frozen=True, slots=True)
class MetricToolScopeV2:
    metric_id: str
    display_name: str
    description: str
    unit: str
    expression: str = field(repr=False)
    scope_id: str = ""
    alert_ref: str | None = None

    @property
    def tool_id(self) -> str:
        return self.scope_id or self.metric_id


@dataclass(frozen=True, slots=True)
class InvestigatorFeedbackV2:
    sequence: int
    rating: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class UnifiedInvestigationView:
    id: str
    occurrence_id: int
    status: InvestigationRunState
    job_id: str | None
    provider_profile_id: str | None
    model_channel_id: str | None
    model_revision: int | None
    snapshot: EvidenceSnapshotV2
    tool_scope_source_id: str
    tool_scope_connection_version: int | None
    tool_scope: tuple[MetricToolScopeV2, ...]
    observations: tuple[MetricObservationV2, ...]
    activities: tuple[InvestigationActivityV2, ...]
    report: InvestigationReportV2 | None
    request_count: int
    tool_call_count: int
    input_tokens: int
    output_tokens: int
    safe_error_code: str | None
    feedback: InvestigatorFeedbackV2 | None
    created_at: datetime
    updated_at: datetime


class UnifiedInvestigationStore(Protocol):
    def find_by_request(
        self, occurrence_id: int, request_key: str
    ) -> UnifiedInvestigationView | None: ...

    def create(
        self,
        *,
        occurrence_id: int,
        request_key: str,
        provider_profile_id: str | None,
        model_channel_id: str | None,
        model_revision: int | None,
        request_id: str,
        source_ip: str,
        snapshot: EvidenceSnapshotV2,
        source_id: str,
        connection_version: int | None,
        tool_scope: tuple[MetricToolScopeV2, ...],
        queue_model: bool,
        model_execution_mode: str,
        now: datetime,
    ) -> UnifiedInvestigationView: ...

    def get(self, investigation_id: str) -> UnifiedInvestigationView: ...

    def is_cancel_requested(self, investigation_id: str) -> bool: ...

    def list_for_occurrence(
        self, occurrence_id: int
    ) -> tuple[UnifiedInvestigationView, ...]: ...

    def mark_running(self, investigation_id: str, *, now: datetime) -> None: ...

    def record_observation(
        self,
        investigation_id: str,
        observation: MetricObservationV2,
        *,
        now: datetime,
    ) -> None: ...

    def record_usage(
        self,
        investigation_id: str,
        delta: InvestigatorUsageDelta,
        *,
        now: datetime,
    ) -> None: ...

    def complete(
        self,
        investigation_id: str,
        *,
        observations: tuple[MetricObservationV2, ...],
        activities: tuple[InvestigationActivityV2, ...],
        report: InvestigationReportV2,
        request_count: int,
        tool_call_count: int,
        input_tokens: int,
        output_tokens: int,
        now: datetime,
    ) -> None: ...

    def fail(
        self,
        investigation_id: str,
        *,
        safe_error_code: str,
        now: datetime,
    ) -> None: ...

    def cancel(
        self, investigation_id: str, *, now: datetime
    ) -> UnifiedInvestigationView: ...

    def record_feedback(
        self,
        investigation_id: str,
        *,
        rating: str,
        actor: InteractiveOperatorActor,
        now: datetime,
    ) -> InvestigatorFeedbackV2: ...


def _evidence_id(prefix: str, *parts: object) -> str:
    digest = hashlib.sha256(":".join(str(item) for item in parts).encode()).hexdigest()
    return f"{prefix}-{digest[:20]}"


def _safe_labels(labels: dict[str, str], allowed_keys: set[str]) -> dict[str, str]:
    return {
        key: value
        for key, value in sorted(labels.items())
        if key in allowed_keys and _SAFE_VALUE.fullmatch(value) is not None
    }


async def _collect_initial_observations(
    metric_ids: tuple[str, ...],
    *,
    read: Callable[[str], Awaitable[MetricObservationV2]],
    concurrency: int = _MAX_INITIAL_CONCURRENCY,
    timeout_seconds: float = _INITIAL_READ_TIMEOUT_SECONDS,
) -> tuple[tuple[MetricObservationV2, ...], bool]:
    """Read the zero-hop set without discarding work completed before deadline."""

    if not metric_ids:
        return (), False
    semaphore = asyncio.Semaphore(concurrency)

    async def bounded(metric_id: str) -> MetricObservationV2:
        async with semaphore:
            return await read(metric_id)

    tasks = [asyncio.create_task(bounded(metric_id)) for metric_id in metric_ids]
    done: set[asyncio.Task[MetricObservationV2]] = set()
    pending: set[asyncio.Task[MetricObservationV2]] = set(tasks)
    try:
        done, pending = await asyncio.wait(tasks, timeout=timeout_seconds)
        observations: list[MetricObservationV2] = []
        incomplete = bool(pending)
        for task in tasks:
            if task not in done:
                continue
            try:
                observations.append(task.result())
            except Exception:  # noqa: BLE001 - one failed read must not erase siblings
                incomplete = True
        return tuple(observations), incomplete
    finally:
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


class InvestigationCanceled(RuntimeError):
    def __init__(self) -> None:
        super().__init__("INVESTIGATION_CANCELED")
        self.code = "INVESTIGATION_CANCELED"


class ScopedMetricTools(ReadOnlyMetricTools):
    """Closed metric catalog; the model never supplies PromQL or a URL."""

    def __init__(
        self,
        *,
        investigation_id: str,
        reader: ThanosReadPort,
        scope: tuple[MetricToolScopeV2, ...],
        initial_observations: tuple[MetricObservationV2, ...] = (),
        observation_sink: Callable[[MetricObservationV2, datetime], None] | None = None,
        cancel_requested: Callable[[], bool] = lambda: False,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._investigation_id = investigation_id
        self._reader = reader
        self._scope = {item.tool_id: item for item in scope}
        self._now = now
        self._observation_sink = observation_sink
        self._cancel_requested = cancel_requested
        self._observations = {
            item.evidence_id: item for item in initial_observations
        }

    @property
    def observations(self) -> tuple[MetricObservationV2, ...]:
        return tuple(self._observations[key] for key in sorted(self._observations))

    def raise_if_canceled(self) -> None:
        if self._cancel_requested():
            raise InvestigationCanceled

    async def list_metrics(self) -> tuple[MetricDescriptorV2, ...]:
        self.raise_if_canceled()
        return tuple(
            MetricDescriptorV2(
                item.tool_id,
                item.display_name,
                item.description,
                item.unit,
            )
            for item in sorted(self._scope.values(), key=lambda value: value.tool_id)
        )

    async def describe_metric(self, metric_id: str) -> MetricDescriptorV2:
        self.raise_if_canceled()
        item = self._scope.get(metric_id)
        if item is None:
            raise LookupError("METRIC_OUT_OF_SCOPE")
        return MetricDescriptorV2(
            item.tool_id, item.display_name, item.description, item.unit
        )

    async def query_metric(
        self, metric_id: str, window_minutes: int
    ) -> MetricObservationV2:
        self.raise_if_canceled()
        item = self._scope.get(metric_id)
        if item is None:
            raise LookupError("METRIC_OUT_OF_SCOPE")
        end = self._now()
        window = QueryWindow(
            end - timedelta(minutes=window_minutes),
            end,
            max(15, min(300, window_minutes * 60 // 60)),
        )
        window.validate(query_count=1)
        evidence_id = _evidence_id(
            "metric-v2", self._investigation_id, metric_id, window_minutes
        )
        try:
            raw = await self._reader.query_range(
                item.expression, window.start, window.end, window.step_seconds
            )
            self.raise_if_canceled()
            result = classify_read(raw)
            if result.status is MetricReadStatus.SUCCESS:
                summary, sample = summarize_series(result.series)
                observation = MetricObservationV2(
                    evidence_id, item.metric_id, "DATA", summary, sample
                )
            else:
                observation = MetricObservationV2(
                    evidence_id,
                    item.metric_id,
                    "EMPTY_NO_DATA",
                    {"safe_code": "EMPTY_NO_DATA"},
                    (),
                )
        except InvestigationCanceled:
            # Cancellation is a control-flow boundary, not evidence that the
            # metric source failed. Do not checkpoint a late network result
            # after the operator has stopped the run.
            raise
        except Exception as exc:  # noqa: BLE001 - typed safe observation boundary
            observation = MetricObservationV2(
                evidence_id,
                item.metric_id,
                "SOURCE_UNAVAILABLE",
                {
                    "safe_code": str(
                        getattr(exc, "code", "METRIC_SOURCE_UNAVAILABLE")
                    )[:96]
                },
                (),
            )
        self._observations[evidence_id] = observation
        if self._observation_sink is not None:
            self._observation_sink(observation, end)
        return observation


class StartUnifiedInvestigation:
    def __init__(
        self,
        *,
        store: UnifiedInvestigationStore,
        incidents: IncidentStore,
        observability: ObservabilityPort,
        thanos_factory: Callable[[str, str], ThanosReadPort],
        model_execution_mode: str,
    ) -> None:
        self._store = store
        self._incidents = incidents
        self._observability = observability
        self._thanos_factory = thanos_factory
        if model_execution_mode not in {"FAKE", "EXTERNAL"}:
            raise ValueError("MODEL_EXECUTION_MODE_INVALID")
        self._model_execution_mode = model_execution_mode

    async def execute(
        self,
        occurrence_id: int,
        *,
        request_key: str,
        actor: InteractiveOperatorActor,
        request_id: str,
        source_ip: str,
        now: datetime | None = None,
    ) -> UnifiedInvestigationView:
        require_interactive_operator(actor)
        replay = self._store.find_by_request(occurrence_id, request_key)
        if replay is not None:
            return replay
        current = now or datetime.now(UTC)
        occurrence = self._incidents.get_operational_occurrence(
            occurrence_id, now=current
        )
        incident = self._incidents.get_incident(occurrence.incident_id)
        if incident.occurrence_no != occurrence.occurrence_no:
            raise RuntimeError("INVESTIGATION_OCCURRENCE_SCOPE_STALE")

        allowed_keys = set(self._observability.planner_label_keys(occurrence.source_id))
        severity_rank = {"critical": 0, "warning": 1, "info": 2}
        members = tuple(
            sorted(
                incident.members,
                key=lambda item: (
                    0 if item.source_state == "FIRING" else 1,
                    severity_rank.get(item.severity.lower(), 3),
                    -item.last_seen_at.timestamp(),
                    item.id,
                ),
            )
        )
        member_alert_refs = tuple(
            stable_alert_ref(item.source_id, item.upstream_fingerprint)
            for item in members
        )
        detailed_members = tuple(zip(members[:_MAX_ALERT_DETAILS], member_alert_refs))
        alert_evidence = tuple(
            {
                "alert_ref": alert_ref,
                "evidence_id": _evidence_id("alert-v2", alert_ref),
                "alertname": item.alertname,
                "severity": item.severity,
                "source_state": item.source_state,
                "labels": _safe_labels(item.labels, allowed_keys),
                "summary": str(item.annotations.get("summary") or "")[:2_000],
                "description": str(item.annotations.get("description") or "")[:2_000],
            }
            for item, alert_ref in detailed_members
        )

        coverage: dict[tuple[str, str, str], int] = {}
        for item in members[_MAX_ALERT_DETAILS:]:
            key = (item.alertname, item.severity, item.source_state)
            coverage[key] = coverage.get(key, 0) + 1
        alert_coverage = tuple(
            {
                "alertname": alertname,
                "severity": severity,
                "source_state": source_state,
                "count": count,
            }
            for (alertname, severity, source_state), count in sorted(coverage.items())
        )

        candidates: list[tuple[str, str, str, str, str]] = []
        contexts: dict[str, AlertMetricContext] = {}
        for member, alert_ref in detailed_members:
            context = self._observability.get_alert_metric_context(member.id)
            contexts[alert_ref] = context
            expression = expr_from_generator_url(context.generator_url)
            if expression is not None:
                candidates.append(
                    (
                        alert_ref,
                        f"{context.alertname} 主曲线",
                        strip_numeric_comparison(expression) or expression,
                        "告警生成表达式派生的主曲线",
                        "",
                    )
                )
            if len(candidates) >= _MAX_INITIAL_QUERIES:
                break
        if len(candidates) < _MAX_INITIAL_QUERIES:
            for template in sorted(
                self._observability.list_metric_templates(),
                key=lambda item: (item.priority, item.id),
            ):
                if not template.enabled or (
                    template.source_ids and occurrence.source_id not in template.source_ids
                ):
                    continue
                for alert_ref, context in contexts.items():
                    rendered = render_metric_template(
                        template.promql,
                        context.labels,
                        required_labels=template.required_labels,
                    )
                    if rendered is not None:
                        candidates.append(
                            (
                                alert_ref,
                                template.name,
                                rendered,
                                template.description,
                                template.unit,
                            )
                        )
                    if len(candidates) >= _MAX_INITIAL_QUERIES:
                        break
                if len(candidates) >= _MAX_INITIAL_QUERIES:
                    break

        degraded: set[str] = set()
        if occurrence.service_id is None:
            degraded.add("service")
        connection = None
        reader = None
        discovered: tuple[str, ...] = ()
        try:
            connection = self._observability.load_monitoring_secret(
                occurrence.source_id, "THANOS", require_active=True
            )
            reader = self._thanos_factory(connection.view.base_url, connection.secret)
            catalog_window = QueryWindow(current - timedelta(hours=1), current, 60)
            discovered = await reader.metric_names(catalog_window.start, catalog_window.end)
        except Exception:  # noqa: BLE001 - explicit evidence degradation
            degraded.add("metrics")

        scope_by_id: dict[str, MetricToolScopeV2] = {}
        initial_scope_ids: list[str] = []
        for alert_ref, display_name, expression, description, unit in candidates:
            for metric_id in extract_catalog_metric_names((expression,)):
                scope_id = _evidence_id(
                    "metric-scope-v2", alert_ref, metric_id, expression
                )
                scope_by_id.setdefault(
                    scope_id,
                    MetricToolScopeV2(
                        metric_id,
                        display_name,
                        description,
                        unit,
                        expression,
                        scope_id,
                        alert_ref,
                    ),
                )
                if scope_id not in initial_scope_ids:
                    initial_scope_ids.append(scope_id)
                break
        for metric_id in discovered:
            scope_id = _evidence_id("metric-catalog-v2", occurrence.source_id, metric_id)
            scope_by_id.setdefault(
                scope_id,
                MetricToolScopeV2(
                    metric_id,
                    metric_id,
                    "指标源目录中的只读指标",
                    "",
                    metric_id,
                    scope_id,
                    None,
                ),
            )
        tool_scope = tuple(
            sorted(
                scope_by_id.values(),
                key=lambda item: (item.alert_ref is None, item.tool_id),
            )[:200]
        )

        observations: list[MetricObservationV2] = []
        if reader is not None:
            initial_tools = ScopedMetricTools(
                investigation_id="initial",
                reader=reader,
                scope=tool_scope,
                now=lambda: current,
            )
            allowed_tool_ids = {item.tool_id for item in tool_scope}
            initial_ids = tuple(
                item for item in initial_scope_ids if item in allowed_tool_ids
            )[:_MAX_INITIAL_QUERIES]

            async def read(metric_id: str) -> MetricObservationV2:
                return await initial_tools.query_metric(metric_id, 60)

            initial, incomplete = await _collect_initial_observations(
                initial_ids,
                read=read,
            )
            observations.extend(initial)
            if incomplete:
                degraded.add("metrics")
        if not observations:
            degraded.add("metrics")

        snapshot = EvidenceSnapshotV2(
            investigation_id="pending",
            occurrence_id=occurrence_id,
            alert_evidence=alert_evidence,
            metric_evidence=tuple(observations),
            degraded_domains=tuple(sorted(degraded)),
            member_alert_refs=member_alert_refs,
            alert_coverage=alert_coverage,
        )
        model = self._observability.active_model_channel()
        queue_model = bool(
            model is not None
            and model.enabled
            and model.api_key_configured
            and model.provider_profile_id
            and any(item.status == "DATA" for item in observations)
        )
        return self._store.create(
            occurrence_id=occurrence_id,
            request_key=request_key,
            provider_profile_id=None if model is None else model.provider_profile_id,
            model_channel_id=None if model is None else model.id,
            model_revision=None if model is None else model.revision_no,
            request_id=request_id,
            source_ip=source_ip,
            snapshot=snapshot,
            source_id=occurrence.source_id,
            connection_version=None if connection is None else connection.view.version,
            tool_scope=tool_scope,
            queue_model=queue_model,
            model_execution_mode=self._model_execution_mode,
            now=datetime.now(UTC),
        )


class RunUnifiedInvestigation:
    def __init__(
        self,
        *,
        store: UnifiedInvestigationStore,
        observability: ObservabilityPort,
        thanos_factory: Callable[[str, str], ThanosReadPort],
        runtime: InvestigatorRuntime,
    ) -> None:
        self._store = store
        self._observability = observability
        self._thanos_factory = thanos_factory
        self._runtime = runtime

    async def execute(self, investigation_id: str) -> None:
        now = datetime.now(UTC)
        current = self._store.get(investigation_id)
        if current.status is InvestigationRunState.COMPLETED:
            return
        if current.status is InvestigationRunState.CANCELED:
            return
        if current.status not in {
            InvestigationRunState.QUEUED,
            InvestigationRunState.RUNNING,
        }:
            raise RuntimeError("INVESTIGATION_V2_NOT_QUEUED")
        if (
            current.model_channel_id is None
            or current.model_revision is None
            or current.provider_profile_id is None
        ):
            self._store.fail(
                investigation_id,
                safe_error_code="MODEL_CONFIGURATION_INCOMPLETE",
                now=now,
            )
            return
        try:
            model = self._observability.load_model_secret_revision(
                current.model_channel_id, current.model_revision
            )
            if (
                model.provider_profile is None
                or model.view.provider_profile_id != current.provider_profile_id
            ):
                raise RuntimeError("MODEL_PROVIDER_PROFILE_CHANGED")
            connection = self._observability.load_monitoring_secret(
                current.tool_scope_source_id, "THANOS", require_active=True
            )
            if connection.view.version != current.tool_scope_connection_version:
                raise RuntimeError("METRIC_CONNECTION_CHANGED")
            tools = ScopedMetricTools(
                investigation_id=investigation_id,
                reader=self._thanos_factory(
                    connection.view.base_url, connection.secret
                ),
                scope=current.tool_scope,
                initial_observations=current.observations,
                observation_sink=lambda observation, observed_at: self._store.record_observation(
                    investigation_id,
                    observation,
                    now=observed_at,
                ),
                cancel_requested=lambda: self._store.is_cancel_requested(
                    investigation_id
                ),
            )
            tools.raise_if_canceled()
            self._store.mark_running(investigation_id, now=now)
            tools.raise_if_canceled()
            result = await self._runtime.run(
                InvestigatorRequest(
                    snapshot=current.snapshot,
                    provider_profile=model.provider_profile,
                    api_key=model.api_key,
                    usage_sink=lambda delta: self._store.record_usage(
                        investigation_id,
                        delta,
                        now=datetime.now(UTC),
                    ),
                ),
                tools=tools,
            )
            self._store.complete(
                investigation_id,
                observations=tools.observations,
                activities=result.activities,
                report=result.report,
                request_count=result.request_count,
                tool_call_count=result.tool_call_count,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                now=datetime.now(UTC),
            )
        except Exception as exc:  # noqa: BLE001 - durable safe failure boundary
            safe_code = safe_investigation_error_code(exc)
            self._store.fail(
                investigation_id,
                safe_error_code=safe_code,
                now=datetime.now(UTC),
            )


_SAFE_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,95}$")


def safe_investigation_error_code(exc: Exception) -> str:
    """Return a stable code without persisting provider exception text."""

    candidate = getattr(exc, "code", None)
    if isinstance(candidate, str) and _SAFE_ERROR_CODE.fullmatch(candidate):
        return candidate
    message = str(exc)
    if _SAFE_ERROR_CODE.fullmatch(message):
        return message
    return "MODEL_SERVICE_UNAVAILABLE"


__all__ = [
    "MetricToolScopeV2",
    "InvestigationCanceled",
    "RunUnifiedInvestigation",
    "ScopedMetricTools",
    "StartUnifiedInvestigation",
    "UnifiedInvestigationStore",
    "UnifiedInvestigationView",
]
