"""Public use cases and ports for metrics, Grafana and model channels."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from uuid import uuid4

from app.domains.investigations.prompt_profiles import (
    PromptGuidanceV1,
    PromptProfileRevisionView,
    PromptProfileView,
)
from app.domains.investigations.runtime import ProviderProfile
from app.domains.metrics.models import (
    BackfillWindow,
    GrafanaCandidate,
    MAX_PROBE_CONCURRENCY,
    MAX_PROBES_PER_IMPORT,
    MAX_SERIES_PER_QUERY,
    MetricReadResult,
    MetricReadStatus,
    QueryBudgetExceeded,
    QueryWindow,
    backfill_window,
    classify_read,
    expr_from_generator_url,
    extract_grafana_candidates,
    grafana_deep_link,
    render_metric_template,
    reconstruct_alerts,
    strip_numeric_comparison,
)
from app.domains.operations.jobs import JobPool, JobSpec


@dataclass(frozen=True, slots=True)
class MonitoringConnectionDraft:
    source_id: str
    kind: str
    base_url: str
    secret_action: str = "KEEP"
    secret_value: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class MonitoringConnectionView:
    source_id: str
    kind: str
    base_url: str
    state: str
    secret_configured: bool
    tested_at: datetime | None
    last_test_code: str | None
    version: int


@dataclass(frozen=True, slots=True)
class MonitoringConnectionSecret:
    view: MonitoringConnectionView
    secret: str = field(repr=False, default="")


@dataclass(frozen=True, slots=True)
class MetricTemplateDraft:
    name: str
    promql: str
    enabled: bool
    priority: int
    source_ids: tuple[str, ...]
    description: str = ""
    required_labels: tuple[str, ...] = ()
    legend_format: str = ""
    unit: str = ""


@dataclass(frozen=True, slots=True)
class MetricTemplateView:
    id: int
    name: str
    promql: str
    enabled: bool
    priority: int
    source_ids: tuple[str, ...]
    origin_kind: str
    version: int
    description: str
    required_labels: tuple[str, ...]
    legend_format: str
    unit: str
    builtin_key: str | None = None
    user_modified: bool = False
    grafana_origin: GrafanaOriginView | None = None


@dataclass(frozen=True, slots=True)
class GrafanaOriginView:
    source_id: str
    dashboard_uid: str
    dashboard_title: str
    panel_id: int
    panel_title: str
    base_url: str


@dataclass(frozen=True, slots=True)
class GrafanaImportSelection:
    candidate: GrafanaCandidate
    name: str
    final_promql: str
    priority: int


@dataclass(frozen=True, slots=True)
class GrafanaImportedState:
    template_id: int
    dashboard_uid: str
    dashboard_title: str
    panel_id: int
    panel_title: str
    ref_id: str
    imported_promql: str
    confirmed_promql: str
    current_promql: str


@dataclass(frozen=True, slots=True)
class ModelChannelInput:
    name: str
    kind: str
    base_url: str
    model: str
    api_key_action: str = "KEEP"
    api_key_value: str | None = field(default=None, repr=False)
    provider_id: str = "CUSTOM"


@dataclass(frozen=True, slots=True)
class ModelChannelView:
    id: str
    name: str
    kind: str
    enabled: bool
    revision_no: int
    state: str
    base_url: str
    model: str
    api_key_configured: bool
    tested_at: datetime | None
    last_test_code: str | None
    provider_profile_id: str | None = None
    provider_id: str = "CUSTOM"
    protocol_profile: str = "CHAT_COMPLETIONS"
    support_level: str = "BEST_EFFORT"


@dataclass(frozen=True, slots=True)
class ModelChannelSecret:
    view: ModelChannelView
    api_key: str = field(repr=False, default="")
    provider_profile: ProviderProfile | None = None


@dataclass(frozen=True, slots=True)
class AlertMetricContext:
    alert_id: int
    source_id: str
    alertname: str
    starts_at: datetime | None
    last_seen_at: datetime
    labels: dict[str, str]
    generator_url: str


@dataclass(frozen=True, slots=True)
class EvidenceCurve:
    kind: str
    name: str
    promql: str
    result: MetricReadResult
    legend_format: str = ""
    unit: str = ""
    deep_link: str | None = None


@dataclass(frozen=True, slots=True)
class MetricEvidence:
    alert_id: int
    alert_starts_at: datetime | None
    window_start: datetime
    window_end: datetime
    step_seconds: int
    curves: tuple[EvidenceCurve, ...]
    failures: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BackfillResult:
    source_id: str
    window: BackfillWindow
    reconstructed: int


class ObservabilityPort(Protocol):
    def save_monitoring_connection(self, draft: MonitoringConnectionDraft, *, expected_version: int | None, now: datetime) -> MonitoringConnectionView: ...
    def get_monitoring_connection(self, source_id: str, kind: str) -> MonitoringConnectionView | None: ...
    def list_monitoring_connections(self, source_id: str) -> tuple[MonitoringConnectionView, ...]: ...
    def list_active_monitoring_connections(self, kind: str) -> tuple[MonitoringConnectionView, ...]: ...
    def load_monitoring_secret(self, source_id: str, kind: str, *, require_active: bool = False) -> MonitoringConnectionSecret: ...
    def record_monitoring_test(self, source_id: str, kind: str, *, expected_version: int, ok: bool, safe_error_code: str, now: datetime) -> MonitoringConnectionView: ...
    def list_metric_templates(self) -> tuple[MetricTemplateView, ...]: ...
    def create_metric_template(self, draft: MetricTemplateDraft, *, origin_kind: str, now: datetime) -> MetricTemplateView: ...
    def update_metric_template(self, template_id: int, draft: MetricTemplateDraft, *, expected_version: int, now: datetime) -> MetricTemplateView: ...
    def delete_metric_template(self, template_id: int, *, expected_version: int) -> None: ...
    def import_grafana_templates(self, source_id: str, selections: tuple[GrafanaImportSelection, ...], *, now: datetime) -> tuple[MetricTemplateView, ...]: ...
    def grafana_imported_promql(self, source_id: str, dashboard_uid: str) -> dict[tuple[int, str], str]: ...
    def grafana_import_states(self, source_id: str, dashboard_uid: str) -> dict[tuple[int, str], GrafanaImportedState]: ...
    def get_alert_metric_context(self, alert_id: int) -> AlertMetricContext: ...
    def apply_metric_backfill(self, source_id: str, raw_alerts: tuple[dict[str, Any], ...], *, expected_connection_version: int, observed_at: datetime, window: BackfillWindow) -> int: ...
    def create_model_channel(self, draft: ModelChannelInput, *, now: datetime) -> ModelChannelView: ...
    def update_model_channel(self, channel_id: str, draft: ModelChannelInput, *, expected_revision: int, now: datetime) -> ModelChannelView: ...
    def load_model_secret(self, channel_id: str, *, require_draft: bool = False) -> ModelChannelSecret: ...
    def load_model_secret_revision(self, channel_id: str, revision_no: int) -> ModelChannelSecret: ...
    def record_model_test(self, channel_id: str, *, expected_revision: int, ok: bool, safe_error_code: str, now: datetime) -> ModelChannelView: ...
    def activate_model_channel(self, channel_id: str, *, expected_revision: int, now: datetime) -> ModelChannelView: ...
    def set_model_channel_enabled(self, channel_id: str, *, enabled: bool, now: datetime) -> ModelChannelView: ...
    def list_model_channels(self) -> tuple[ModelChannelView, ...]: ...
    def active_model_channel(self) -> ModelChannelView | None: ...
    def list_prompt_profiles(self) -> tuple[PromptProfileView, ...]: ...
    def list_prompt_profile_revisions(self, profile_id: str) -> tuple[PromptProfileRevisionView, ...]: ...
    def copy_prompt_profile(self, name: str, *, now: datetime) -> PromptProfileView: ...
    def update_prompt_profile(self, profile_id: str, guidance: PromptGuidanceV1, *, expected_revision: int, now: datetime) -> PromptProfileView: ...
    def test_prompt_profile(self, profile_id: str, *, expected_revision: int, now: datetime) -> PromptProfileView: ...
    def activate_prompt_profile(self, profile_id: str, *, expected_revision: int, global_default: bool, service_ids: tuple[int, ...], now: datetime) -> PromptProfileView: ...
    def resolve_prompt_profile(self, *, profile_id: str | None, service_id: int | None) -> PromptProfileView: ...
    def planner_label_keys(self, source_id: str) -> tuple[str, ...]: ...


class ThanosReadPort(Protocol):
    async def probe(self) -> tuple[bool, str]: ...
    async def query_range(self, query: str, start: datetime, end: datetime, step_seconds: int) -> dict[str, Any]: ...
    async def query_instant(self, query: str, at: datetime, *, limit: int | None = None) -> dict[str, Any]: ...
    async def alert_rules(self, alertname: str) -> tuple[dict[str, Any], ...]: ...
    async def query_alerts(self, start: datetime, end: datetime, step_seconds: int) -> dict[str, Any]: ...
    async def metric_names(self, start: datetime, end: datetime) -> tuple[str, ...]: ...
    async def metric_metadata(self, metric: str) -> dict[str, str] | None: ...
    async def metric_label_names(self, metric: str, start: datetime, end: datetime) -> tuple[str, ...]: ...
    async def metric_label_values(self, metric: str, label: str, start: datetime, end: datetime) -> tuple[str, ...]: ...


class GrafanaReadPort(Protocol):
    async def search_dashboards(self, query: str, *, limit: int = 50) -> tuple[dict[str, Any], ...]: ...
    async def get_dashboard(self, uid: str) -> dict[str, Any]: ...


class ModelProbePort(Protocol):
    async def test(self, *, base_url: str, model: str, api_key: str) -> Any: ...


class BackfillJobPlanner:
    def __init__(self, port: ObservabilityPort) -> None:
        self._port = port

    def plan(self, *, now: datetime) -> tuple[JobSpec, ...]:
        day = now.astimezone(timezone.utc).strftime("%Y%m%d")
        return tuple(
            JobSpec(
                kind="metrics.backfill",
                pool=JobPool.SOURCE,
                subject_type="event_source",
                subject_id=item.source_id,
                payload={"expected_connection_version": item.version},
                payload_revision=1,
                idempotency_key=f"metrics-backfill:{item.source_id}:{item.version}:{day}",
            )
            for item in self._port.list_active_monitoring_connections("THANOS")
        )


class BackfillSourceMetrics:
    def __init__(self, port: ObservabilityPort) -> None:
        self._port = port

    async def execute(
        self,
        source_id: str,
        reader: ThanosReadPort,
        *,
        expected_connection_version: int,
        now: datetime,
        requested_hours: int = 24,
        hard_limit_hours: int = 168,
        step_seconds: int = 60,
    ) -> BackfillResult:
        window = backfill_window(
            now,
            requested_hours=requested_hours,
            hard_limit_hours=hard_limit_hours,
        )
        data = await reader.query_alerts(window.start, window.end, step_seconds)
        alerts = reconstruct_alerts(data)
        count = self._port.apply_metric_backfill(
            source_id,
            alerts,
            expected_connection_version=expected_connection_version,
            observed_at=now,
            window=window,
        )
        return BackfillResult(source_id, window, count)


class TestMonitoringConnection:
    def __init__(self, port: ObservabilityPort) -> None:
        self._port = port

    async def execute(self, source_id: str, kind: str, *, expected_version: int, reader: ThanosReadPort | GrafanaReadPort, now: datetime) -> MonitoringConnectionView:
        if kind == "THANOS":
            ok, code = await reader.probe()  # type: ignore[union-attr]
        else:
            try:
                await reader.search_dashboards("", limit=1)  # type: ignore[union-attr]
                ok, code = True, "OK"
            except Exception as exc:
                ok, code = False, getattr(exc, "code", "GRAFANA_UNAVAILABLE")
        return self._port.record_monitoring_test(source_id, kind, expected_version=expected_version, ok=ok, safe_error_code=code, now=now)


class PreviewGrafanaImport:
    async def execute(self, reader: GrafanaReadPort, *, dashboard_uid: str) -> tuple[GrafanaCandidate, ...]:
        dashboard = await reader.get_dashboard(dashboard_uid)
        return extract_grafana_candidates(dashboard, dashboard_uid=dashboard_uid)


class ProbeGrafanaCandidates:
    async def execute(self, reader: ThanosReadPort, candidates: tuple[GrafanaCandidate, ...], *, at: datetime) -> tuple[str, ...]:
        results = ["UNVERIFIED"] * len(candidates)
        semaphore = asyncio.Semaphore(MAX_PROBE_CONCURRENCY)

        async def run(index: int, candidate: GrafanaCandidate) -> None:
            if candidate.status == "UNSUPPORTED":
                results[index] = "UNSUPPORTED"
                return
            if candidate.required_variables:
                results[index] = "NEEDS_BINDING"
                return
            async with semaphore:
                try:
                    data = await reader.query_instant(candidate.imported_promql, at, limit=MAX_SERIES_PER_QUERY + 1)
                    results[index] = classify_read(data).status.value
                except QueryBudgetExceeded:
                    results[index] = "REJECTED"
                except Exception:
                    results[index] = MetricReadStatus.SOURCE_UNAVAILABLE.value

        await asyncio.gather(
            *(run(index, item) for index, item in enumerate(candidates[:MAX_PROBES_PER_IMPORT]))
        )
        for index in range(MAX_PROBES_PER_IMPORT, len(candidates)):
            results[index] = "PROBE_BUDGET_EXCEEDED"
        return tuple(results)


class ReadMetricEvidence:
    def __init__(self, port: ObservabilityPort) -> None:
        self._port = port

    async def execute(self, alert_id: int, reader: ThanosReadPort) -> MetricEvidence:
        context = self._port.get_alert_metric_context(alert_id)
        end = context.last_seen_at
        start = context.starts_at or end - timedelta(hours=2)
        start = max(start, end - timedelta(seconds=24 * 3600))
        if (end - start).total_seconds() <= 0:
            start = end - timedelta(minutes=5)
        window = QueryWindow(start, end, 60)
        templates = [
            item
            for item in self._port.list_metric_templates()
            if item.enabled
            and (not item.source_ids or context.source_id in item.source_ids)
            and all(context.labels.get(label, "").strip() for label in item.required_labels)
        ]
        templates.sort(key=lambda item: (item.priority, item.id))
        templates = templates[:5]
        window.validate(query_count=1 + len(templates))
        curves: list[EvidenceCurve] = []
        failures: list[str] = []
        rule_expression = ""
        try:
            rules = tuple(
                item for item in await reader.alert_rules(context.alertname)
                if str(item.get("name") or item.get("alert") or "") == context.alertname
            )
            if len(rules) == 1:
                rule_expression = str(
                    rules[0].get("query") or rules[0].get("expr") or ""
                ).strip()
            elif len(rules) > 1:
                failures.append("MAIN_EXPRESSION_AMBIGUOUS")
        except Exception as exc:
            failures.append(getattr(exc, "code", "ALERT_RULES_UNAVAILABLE"))

        source_expression = rule_expression or (
            expr_from_generator_url(context.generator_url) or ""
        )
        if source_expression:
            promql = strip_numeric_comparison(source_expression) or source_expression
            try:
                result = classify_read(
                    await reader.query_range(
                        promql, window.start, window.end, window.step_seconds
                    )
                )
            except Exception as exc:
                result = MetricReadResult(
                    MetricReadStatus.SOURCE_UNAVAILABLE,
                    safe_error_code=getattr(exc, "code", "METRIC_SOURCE_UNAVAILABLE"),
                )
            curves.append(EvidenceCurve("MAIN", context.alertname, promql, result))
        else:
            failures.append("MAIN_EXPRESSION_UNAVAILABLE")

        for template in templates:
            rendered_promql = render_metric_template(
                template.promql,
                context.labels,
                required_labels=template.required_labels,
            )
            if rendered_promql is None:
                continue
            try:
                result = classify_read(
                    await reader.query_range(
                        rendered_promql,
                        window.start,
                        window.end,
                        window.step_seconds,
                    )
                )
            except Exception as exc:
                result = MetricReadResult(
                    MetricReadStatus.SOURCE_UNAVAILABLE,
                    safe_error_code=getattr(exc, "code", "METRIC_SOURCE_UNAVAILABLE"),
                )
            deep_link = None
            if template.grafana_origin is not None:
                origin = template.grafana_origin
                deep_link = grafana_deep_link(
                    origin.base_url,
                    dashboard_uid=origin.dashboard_uid,
                    panel_id=origin.panel_id,
                    start_ms=int(window.start.timestamp() * 1000),
                    end_ms=int(window.end.timestamp() * 1000),
                )
            curves.append(
                EvidenceCurve(
                    "TEMPLATE",
                    template.name,
                    rendered_promql,
                    result,
                    template.legend_format,
                    template.unit,
                    deep_link,
                )
            )
        return MetricEvidence(
            alert_id,
            context.starts_at,
            window.start,
            window.end,
            window.step_seconds,
            tuple(curves),
            tuple(failures),
        )


def new_model_channel_id() -> str:
    return f"model_{uuid4().hex}"


def new_prompt_profile_id() -> str:
    return f"prompt_{uuid4().hex}"
