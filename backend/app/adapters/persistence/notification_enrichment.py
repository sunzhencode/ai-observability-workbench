"""Read-only projection of already-persisted evidence into notification facts."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
import json
from typing import cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.persistence.incidents import OperationalOccurrenceRecord
from app.adapters.persistence.observability import (
    GrafanaTemplateOriginRecord,
    MetricTemplateRecord,
    MonitoringConnectionRecord,
)
from app.adapters.persistence.unified_investigations import (
    EvidenceSnapshotV2Record,
    InvestigationRunV2Record,
    InvestigationToolScopeV2Record,
)
from app.domains.investigations.planner import extract_catalog_metric_names
from app.domains.metrics.models import grafana_deep_link
from app.domains.notifications.models import (
    NotificationDeepLink,
    NotificationEnrichment,
    NotificationMetricEvidence,
)
from app.platform.persistence.codecs import aware_utc as _aware


MAX_NOTIFICATION_METRICS = 2
MAX_NOTIFICATION_DEEP_LINKS = 2


def _json_list(value: str) -> list[object]:
    raw = json.loads(value)
    if not isinstance(raw, list):
        raise RuntimeError("NOTIFICATION_EVIDENCE_JSON_INVALID")
    return cast(list[object], raw)


def _scalar(value: object) -> float | int | str | None:
    if value is None or isinstance(value, (float, int, str)):
        return value
    return str(value)[:128]


def _catalog_by_metric(
    session: Session, investigation_id: str
) -> dict[str, tuple[str, str]]:
    scope = session.get(InvestigationToolScopeV2Record, investigation_id)
    if scope is None:
        return {}
    result: dict[str, tuple[str, str]] = {}
    for raw in _json_list(scope.catalog_json):
        if not isinstance(raw, Mapping):
            continue
        metric_id = str(raw.get("metric_id") or "")
        if not metric_id:
            continue
        result[metric_id] = (
            str(raw.get("display_name") or metric_id),
            str(raw.get("unit") or ""),
        )
    return result


def _metric_evidence(
    session: Session, investigation_id: str
) -> tuple[NotificationMetricEvidence, ...]:
    snapshot = session.get(EvidenceSnapshotV2Record, investigation_id)
    if snapshot is None:
        return ()
    catalog = _catalog_by_metric(session, investigation_id)
    result: list[NotificationMetricEvidence] = []
    seen: set[str] = set()
    for raw in _json_list(snapshot.metric_evidence_json):
        if not isinstance(raw, Mapping) or str(raw.get("status") or "") != "DATA":
            continue
        metric_id = str(raw.get("metric_id") or "")
        summary = raw.get("summary")
        if not metric_id or metric_id in seen or not isinstance(summary, Mapping):
            continue
        display_name, unit = catalog.get(metric_id, (metric_id, ""))
        result.append(
            NotificationMetricEvidence(
                metric_id=metric_id,
                display_name=display_name,
                unit=unit,
                latest=_scalar(summary.get("latest")),
                minimum=_scalar(summary.get("minimum")),
                maximum=_scalar(summary.get("maximum")),
            )
        )
        seen.add(metric_id)
        if len(result) >= MAX_NOTIFICATION_METRICS:
            break
    return tuple(result)


def _source_ids(value: str) -> set[str]:
    return {str(item) for item in _json_list(value)}


def _grafana_links(
    session: Session,
    *,
    source_id: str,
    evidence: tuple[NotificationMetricEvidence, ...],
    observed_at: datetime,
) -> tuple[NotificationDeepLink, ...]:
    connection = session.get(MonitoringConnectionRecord, (source_id, "GRAFANA"))
    if connection is None or connection.state != "ACTIVE":
        return ()
    metric_ids = {item.metric_id for item in evidence}
    if not metric_ids:
        return ()
    start_ms = int((_aware(observed_at) - timedelta(hours=1)).timestamp() * 1000)
    end_ms = int(_aware(observed_at).timestamp() * 1000)
    result: list[NotificationDeepLink] = []
    rows = session.execute(
        select(MetricTemplateRecord, GrafanaTemplateOriginRecord)
        .join(
            GrafanaTemplateOriginRecord,
            GrafanaTemplateOriginRecord.template_id == MetricTemplateRecord.id,
        )
        .where(
            MetricTemplateRecord.enabled.is_(True),
            GrafanaTemplateOriginRecord.source_id == source_id,
        )
        .order_by(MetricTemplateRecord.priority, MetricTemplateRecord.id)
    )
    for template, origin in rows:
        source_ids = _source_ids(template.source_ids_json)
        if source_ids and source_id not in source_ids:
            continue
        names = set(extract_catalog_metric_names((template.promql,)))
        if not names.intersection(metric_ids):
            continue
        result.append(
            NotificationDeepLink(
                label=f"{origin.dashboard_title} / {origin.panel_title}",
                url=grafana_deep_link(
                    connection.base_url,
                    dashboard_uid=origin.dashboard_uid,
                    panel_id=origin.panel_id,
                    start_ms=start_ms,
                    end_ms=end_ms,
                ),
            )
        )
        if len(result) >= MAX_NOTIFICATION_DEEP_LINKS:
            break
    return tuple(result)


def read_notification_enrichment_in_session(
    session: Session,
    incident_id: int,
    occurrence_no: int,
    observed_at: datetime,
    include_evidence: bool,
) -> NotificationEnrichment:
    """Project local L1 facts only; this function performs no network I/O."""
    occurrence = session.scalar(
        select(OperationalOccurrenceRecord).where(
            OperationalOccurrenceRecord.incident_id == incident_id,
            OperationalOccurrenceRecord.occurrence_no == occurrence_no,
        )
    )
    if occurrence is None:
        return NotificationEnrichment(
            occurrence_id=None,
            evidence_status="NOT_AVAILABLE",
            safe_code="NOTIFICATION_OCCURRENCE_PROJECTION_MISSING",
        )
    if not include_evidence:
        return NotificationEnrichment(
            occurrence_id=occurrence.id,
            evidence_status="NOT_REQUESTED",
        )
    run = session.scalar(
        select(InvestigationRunV2Record)
        .where(InvestigationRunV2Record.occurrence_id == occurrence.id)
        .order_by(
            InvestigationRunV2Record.updated_at.desc(),
            InvestigationRunV2Record.id.desc(),
        )
        .limit(1)
    )
    if run is None:
        return NotificationEnrichment(
            occurrence_id=occurrence.id,
            evidence_status="NOT_AVAILABLE",
            safe_code="NOTIFICATION_METRIC_EVIDENCE_NOT_AVAILABLE",
        )
    evidence = _metric_evidence(session, run.id)
    return NotificationEnrichment(
        occurrence_id=occurrence.id,
        metric_evidence=evidence,
        deep_links=_grafana_links(
            session,
            source_id=occurrence.source_id,
            evidence=evidence,
            observed_at=observed_at,
        ),
        evidence_status="AVAILABLE" if evidence else "NOT_AVAILABLE",
        safe_code=(
            None if evidence else "NOTIFICATION_METRIC_EVIDENCE_NOT_AVAILABLE"
        ),
    )
