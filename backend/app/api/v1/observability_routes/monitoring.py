"""Monitoring connection and Grafana import routes."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Query

from app.api.v1.observability_routes.common import (
    ObservabilityApiDependencies,
    connection_response,
    error_responses,
    metric_template_response,
    monitoring_secret,
    safe_error,
)
from app.api.v1.schemas import (
    DashboardSearchResponse,
    GrafanaCandidateResponse,
    GrafanaConfirmInput,
    GrafanaPreviewInput,
    MetricTemplateResponse,
    MonitoringConnectionInput,
    MonitoringConnectionResponse,
    MonitoringTestInput,
)
from app.application.observability import (
    GrafanaImportSelection,
    MonitoringConnectionDraft,
    PreviewGrafanaImport,
    ProbeGrafanaCandidates,
    TestMonitoringConnection,
)
from app.domains.metrics.models import GrafanaCandidate
from app.platform.errors import SafeApiError


def register_monitoring_routes(
    router: APIRouter,
    dependencies: ObservabilityApiDependencies,
) -> None:
    port = dependencies.port
    errors = error_responses()

    @router.get(
        "/sources/{source_id}/monitoring",
        response_model=list[MonitoringConnectionResponse],
        responses=errors,
    )
    async def list_monitoring(source_id: str) -> list[MonitoringConnectionResponse]:
        return [connection_response(value) for value in port.list_monitoring_connections(source_id)]

    @router.get(
        "/sources/{source_id}/monitoring/{kind}",
        response_model=MonitoringConnectionResponse,
        responses=errors,
    )
    async def get_monitoring(source_id: str, kind: str) -> MonitoringConnectionResponse:
        value = port.get_monitoring_connection(source_id, kind.upper())
        if value is None:
            raise SafeApiError(
                status_code=404,
                code="MONITORING_CONNECTION_NOT_FOUND",
                message="该来源尚未配置此监控连接",
            )
        return connection_response(value)

    @router.put(
        "/sources/{source_id}/monitoring/{kind}",
        response_model=MonitoringConnectionResponse,
        responses=errors,
    )
    async def save_monitoring(
        source_id: str,
        kind: str,
        payload: MonitoringConnectionInput,
    ) -> MonitoringConnectionResponse:
        try:
            value = port.save_monitoring_connection(
                MonitoringConnectionDraft(
                    source_id,
                    kind.upper(),
                    payload.base_url,
                    payload.secret.action,
                    payload.secret.value,
                ),
                expected_version=payload.expected_version,
                now=datetime.now(timezone.utc),
            )
        except Exception as exc:
            raise safe_error(exc) from exc
        return connection_response(value)

    @router.post(
        "/sources/{source_id}/monitoring/{kind}/test",
        response_model=MonitoringConnectionResponse,
        responses=errors,
    )
    async def test_monitoring(
        source_id: str,
        kind: str,
        payload: MonitoringTestInput,
    ) -> MonitoringConnectionResponse:
        upper = kind.upper()
        configured = monitoring_secret(port, source_id, upper)
        if upper == "THANOS":
            reader = dependencies.thanos_factory(configured.view.base_url, configured.secret)
        elif upper == "GRAFANA":
            reader = dependencies.grafana_factory(configured.view.base_url, configured.secret)
        else:
            raise SafeApiError(
                status_code=422,
                code="MONITORING_KIND_INVALID",
                message="监控连接类型不受支持",
            )
        try:
            value = await TestMonitoringConnection(port).execute(
                source_id,
                upper,
                expected_version=payload.expected_version,
                reader=reader,
                now=datetime.now(timezone.utc),
            )
        except Exception as exc:
            raise safe_error(exc) from exc
        return connection_response(value)

    @router.get(
        "/sources/{source_id}/grafana/dashboards",
        response_model=list[DashboardSearchResponse],
        responses=errors,
    )
    async def search_dashboards(
        source_id: str,
        query: str = Query(default="", max_length=256),
    ) -> list[DashboardSearchResponse]:
        configured = monitoring_secret(port, source_id, "GRAFANA", active=True)
        reader = dependencies.grafana_factory(configured.view.base_url, configured.secret)
        try:
            rows = await reader.search_dashboards(query, limit=50)
        except Exception as exc:
            code = getattr(exc, "code", "GRAFANA_UNAVAILABLE")
            raise SafeApiError(
                status_code=409,
                code=code,
                message="Grafana 读取未完成，请检查连接测试状态与网络",
            ) from exc
        return [
            DashboardSearchResponse(
                uid=str(item.get("uid") or ""),
                title=str(item.get("title") or ""),
                url=str(item.get("url") or ""),
            )
            for item in rows
            if item.get("uid")
        ]

    @router.post(
        "/sources/{source_id}/grafana/import/preview",
        response_model=list[GrafanaCandidateResponse],
        responses=errors,
    )
    async def preview_grafana(
        source_id: str,
        payload: GrafanaPreviewInput,
    ) -> list[GrafanaCandidateResponse]:
        configured = monitoring_secret(port, source_id, "GRAFANA", active=True)
        reader = dependencies.grafana_factory(configured.view.base_url, configured.secret)
        try:
            candidates = await PreviewGrafanaImport().execute(
                reader,
                dashboard_uid=payload.dashboard_uid,
            )
        except Exception as exc:
            code = getattr(exc, "code", "GRAFANA_UNAVAILABLE")
            raise SafeApiError(
                status_code=409,
                code=code,
                message="Grafana dashboard 定义读取未完成",
            ) from exc
        previous = port.grafana_import_states(source_id, payload.dashboard_uid)
        try:
            thanos = port.load_monitoring_secret(source_id, "THANOS", require_active=True)
            probe_statuses = await ProbeGrafanaCandidates().execute(
                dependencies.thanos_factory(thanos.view.base_url, thanos.secret),
                candidates,
                at=datetime.now(timezone.utc),
            )
        except Exception:
            probe_statuses = tuple("UNVERIFIED" for _item in candidates)
        response: list[GrafanaCandidateResponse] = []
        current_keys: set[tuple[int, str]] = set()
        for index, item in enumerate(candidates):
            key = (item.panel_id, item.ref_id)
            current_keys.add(key)
            prior = previous.get(key)
            change_kind: Literal[
                "NEW", "UNCHANGED", "UPSTREAM_CHANGED", "CONFLICT", "GONE"
            ]
            if prior is None:
                change_kind = "NEW"
            elif prior.imported_promql == item.imported_promql:
                change_kind = "UNCHANGED"
            elif prior.current_promql != prior.confirmed_promql:
                change_kind = "CONFLICT"
            else:
                change_kind = "UPSTREAM_CHANGED"
            response.append(
                GrafanaCandidateResponse(
                    **asdict(item),
                    change_kind=change_kind,
                    probe_status=probe_statuses[index],
                    template_id=None if prior is None else prior.template_id,
                    current_promql="" if prior is None else prior.current_promql,
                )
            )
        for key, prior in previous.items():
            if key in current_keys:
                continue
            response.append(
                GrafanaCandidateResponse(
                    dashboard_uid=prior.dashboard_uid,
                    dashboard_title=prior.dashboard_title,
                    panel_id=prior.panel_id,
                    panel_title=prior.panel_title,
                    ref_id=prior.ref_id,
                    imported_promql=prior.imported_promql,
                    required_variables=[],
                    legend_format="",
                    unit="",
                    status="READY",
                    reason="",
                    change_kind="GONE",
                    probe_status="NOT_PROBED",
                    template_id=prior.template_id,
                    current_promql=prior.current_promql,
                )
            )
        return response

    @router.post(
        "/sources/{source_id}/grafana/import/confirm",
        response_model=list[MetricTemplateResponse],
        responses=errors,
    )
    async def confirm_grafana(
        source_id: str,
        payload: GrafanaConfirmInput,
    ) -> list[MetricTemplateResponse]:
        selections = tuple(
            GrafanaImportSelection(
                GrafanaCandidate(
                    item.dashboard_uid,
                    item.dashboard_title,
                    item.panel_id,
                    item.panel_title,
                    item.ref_id,
                    item.imported_promql,
                    tuple(item.required_variables),
                    item.legend_format,
                    item.unit,
                ),
                item.name,
                item.final_promql,
                item.priority,
            )
            for item in payload.selections
        )
        try:
            values = port.import_grafana_templates(
                source_id,
                selections,
                now=datetime.now(timezone.utc),
            )
        except Exception as exc:
            raise safe_error(exc) from exc
        return [metric_template_response(item) for item in values]


__all__ = ["register_monitoring_routes"]
