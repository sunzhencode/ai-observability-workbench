"""Pure contracts for frozen, staged incident evidence."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
from datetime import datetime
from typing import Any, Mapping, Sequence

from app.domains.investigations.planner import LabelRejectionV1, PlannerStepV1

MAX_DETAILED_ALERTS = 20
MAX_ZERO_HOP_QUERIES = 6
MAX_ZERO_HOP_CONCURRENCY = 2
MAX_ZERO_HOP_WALL_CLOCK_SECONDS = 20
SNAPSHOT_SCHEMA_REVISION = 1
PLAYBOOK_REVISION = 1


class InvestigationStatus(StrEnum):
    PREPARING = "PREPARING"
    EVIDENCE_ONLY = "EVIDENCE_ONLY"
    QUEUED = "QUEUED"
    FAILED = "FAILED"


class InvestigationPhase(StrEnum):
    CLAIMED = "CLAIMED"
    EVIDENCE_BRIEF_READY = "EVIDENCE_BRIEF_READY"
    EVIDENCE_EXPANSION_QUEUED = "EVIDENCE_EXPANSION_QUEUED"
    EVIDENCE_EXPANDED = "EVIDENCE_EXPANDED"
    ANALYST_RESULT_READY = "ANALYST_RESULT_READY"
    CANCELED = "CANCELED"
    TERMINAL_EVIDENCE_ONLY = "TERMINAL_EVIDENCE_ONLY"


@dataclass(frozen=True, slots=True)
class InitialInvestigationSnapshotV1:
    schema_revision: int
    occurrence: Mapping[str, Any]
    alerts: Mapping[str, Any]
    metrics: Mapping[str, Any]
    grafana: Mapping[str, Any]
    history: Mapping[str, Any]
    context: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class InvestigationPhaseTransitionV1:
    sequence: int
    from_phase: InvestigationPhase | None
    to_phase: InvestigationPhase


@dataclass(frozen=True, slots=True)
class AlertScopeRef:
    alert_ref: str
    alert_id: int
    alertname: str
    severity: str
    source_state: str


@dataclass(frozen=True, slots=True)
class InvestigationScopeResolutionV1:
    source_id: str
    occurrence_id: int
    incident_id: int
    service_id: int | None
    member_alert_refs: tuple[str, ...]
    catalog_revision: str


@dataclass(frozen=True, slots=True)
class InvestigationPlaybookV1:
    playbook_id: str
    revision: int
    metric_strategy: str
    stopping_condition: str


def select_playbook(*, service_id: int | None) -> InvestigationPlaybookV1:
    """Select one immutable built-in playbook without reading arbitrary files."""
    return InvestigationPlaybookV1(
        "service-incident" if service_id is not None else "unmapped-incident",
        PLAYBOOK_REVISION,
        "member-main-curves-then-published-templates",
        "zero-hop-budget-or-deadline",
    )


@dataclass(frozen=True, slots=True)
class MetricObservationV1:
    evidence_ref: str
    alert_ref: str
    metric_name: str
    l1_summary: Mapping[str, float | int | str | None]
    l2_sample: tuple[tuple[float, str], ...]
    l3_series: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True, slots=True)
class MetricEmptyObservationV1:
    evidence_ref: str
    alert_ref: str
    metric_name: str
    code: str = "EMPTY_NO_DATA"
    message: str = "查询成功，但所选时间窗口内没有指标数据"


@dataclass(frozen=True, slots=True)
class SimilarHistoryObservationV1:
    rank: int
    occurrence_id: int
    score: int
    match_reasons: tuple[Mapping[str, str | int], ...]
    resolution_code: str
    operator_conclusion: str | None
    task_outcome: str | None
    handling_duration_seconds: int
    resolved_at: datetime


@dataclass(frozen=True, slots=True)
class InvestigationDegradationV1:
    domain: str
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class InvestigationCanceledV1:
    reason: str
    canceled_before: str


@dataclass(frozen=True, slots=True)
class EvidenceBriefV1:
    member_total: int
    member_detailed: int
    query_planned: int
    query_completed: int
    successful_metric_facts: int
    empty_metric_facts: int
    degraded_domains: tuple[str, ...]
    findings: tuple[str, ...]


InvestigationFactV1 = (
    InvestigationPhaseTransitionV1
    | EvidenceBriefV1
    | MetricObservationV1
    | MetricEmptyObservationV1
    | SimilarHistoryObservationV1
    | InvestigationDegradationV1
    | InvestigationCanceledV1
    | PlannerStepV1
    | LabelRejectionV1
)


def stable_alert_ref(source_id: str, fingerprint: str) -> str:
    value = f"{source_id}\n{fingerprint}".encode()
    return "alert-" + hashlib.sha256(value).hexdigest()[:20]


def evidence_ref(alert_ref: str, position: int) -> str:
    return "metric-" + hashlib.sha256(f"{alert_ref}:{position}".encode()).hexdigest()[:20]


def snapshot_hash(snapshot: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def summarize_series(
    series: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, float | int | str | None], tuple[tuple[float, str], ...]]:
    """Derive bounded L1/L2 while leaving the original L3 untouched."""
    points: list[tuple[float, str]] = []
    for item in series:
        raw_values = item.get("values")
        if isinstance(raw_values, list):
            for raw in raw_values:
                if isinstance(raw, list) and len(raw) >= 2:
                    try:
                        points.append((float(raw[0]), str(raw[1])))
                    except (TypeError, ValueError):
                        continue
        raw_value = item.get("value")
        if isinstance(raw_value, list) and len(raw_value) >= 2:
            try:
                points.append((float(raw_value[0]), str(raw_value[1])))
            except (TypeError, ValueError):
                pass
    numeric: list[float] = []
    for _, value in points:
        try:
            numeric.append(float(value))
        except ValueError:
            continue
    summary: dict[str, float | int | str | None] = {
        "series_count": len(series),
        "point_count": len(points),
        "minimum": min(numeric) if numeric else None,
        "maximum": max(numeric) if numeric else None,
        "latest": numeric[-1] if numeric else None,
    }
    if len(points) <= 20:
        sample = tuple(points)
    else:
        indexes = sorted({round(index * (len(points) - 1) / 19) for index in range(20)})
        sample = tuple(points[index] for index in indexes)
    return summary, sample
