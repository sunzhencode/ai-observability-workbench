"""Read-model contracts for deterministic operational Analytics."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from app.domains.analytics import AnalyticsWindow, DurationSummary, RatioSummary


@dataclass(frozen=True, slots=True)
class DurationMetricView:
    completed: DurationSummary
    unfinished: DurationSummary


@dataclass(frozen=True, slots=True)
class ResponseAnalyticsView:
    mtta: DurationMetricView
    resolution: DurationMetricView
    task_outcomes: dict[str, int] = field(default_factory=dict)
    resolution_codes: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SignalAnalyticsView:
    new_occurrences: int
    new_alert_instances: int
    compression: RatioSummary
    alerts_per_occurrence: RatioSummary
    creation_unmapped: RatioSummary
    creation_assignment_unknown: int
    current_unmapped: int
    ack_sla_breach: RatioSummary
    flapping_activations: int = 0
    storm_activations: int = 0


@dataclass(frozen=True, slots=True)
class NotificationAnalyticsView:
    succeeded: int
    permanently_failed: int
    pending_backlog: int
    noise: int
    success_rate: RatioSummary
    latency: DurationSummary


@dataclass(frozen=True, slots=True)
class AiAnalyticsView:
    p2_valid: int
    evidence_only: int
    canceled: int
    contract_rejected: int
    dependency_failed: int
    pending: int
    model_started: int
    unknown_cost: int
    p2_success: RatioSummary
    feedback_response: RatioSummary
    feedback_adoption: RatioSummary
    p0_mtti: DurationSummary
    p1_mtti: DurationSummary
    p2_mtti: DurationSummary


@dataclass(frozen=True, slots=True)
class AnalyticsOverview:
    freshness: Literal["READY", "ROLLUP_PENDING"]
    generated_at: datetime | None
    window: AnalyticsWindow
    response: ResponseAnalyticsView
    signal: SignalAnalyticsView
    notifications: dict[str, NotificationAnalyticsView] = field(default_factory=dict)
    ai: dict[str, AiAnalyticsView] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AnalyticsRefreshResult:
    bucket_count: int
    duration_sample_count: int
    refreshed_at: datetime
