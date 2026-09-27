"""Typed transport schemas used as the frontend contract source."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorEnvelope(BaseModel):
    error: ErrorBody


class AnalyticsRatioResponse(BaseModel):
    numerator: int
    denominator: int
    ratio: float | None


class AnalyticsDurationSummaryResponse(BaseModel):
    count: int
    median_ms: int | None
    p90_ms: int | None


class AnalyticsDurationMetricResponse(BaseModel):
    completed: AnalyticsDurationSummaryResponse
    unfinished: AnalyticsDurationSummaryResponse


class AnalyticsResponseSection(BaseModel):
    mtta: AnalyticsDurationMetricResponse
    resolution: AnalyticsDurationMetricResponse
    task_outcomes: dict[str, int]
    resolution_codes: dict[str, int]


class AnalyticsSignalSection(BaseModel):
    new_occurrences: int
    new_alert_instances: int
    compression: AnalyticsRatioResponse
    alerts_per_occurrence: AnalyticsRatioResponse
    creation_unmapped: AnalyticsRatioResponse
    creation_assignment_unknown: int
    current_unmapped: int
    ack_sla_breach: AnalyticsRatioResponse
    flapping_activations: int
    storm_activations: int


class AnalyticsNotificationModeResponse(BaseModel):
    succeeded: int
    permanently_failed: int
    pending_backlog: int
    noise: int
    success_rate: AnalyticsRatioResponse
    latency: AnalyticsDurationSummaryResponse


class AnalyticsAiModeResponse(BaseModel):
    p2_valid: int
    evidence_only: int
    canceled: int
    contract_rejected: int
    dependency_failed: int
    pending: int
    model_started: int
    unknown_cost: int
    p2_success: AnalyticsRatioResponse
    feedback_response: AnalyticsRatioResponse
    feedback_adoption: AnalyticsRatioResponse
    p0_mtti: AnalyticsDurationSummaryResponse
    p1_mtti: AnalyticsDurationSummaryResponse
    p2_mtti: AnalyticsDurationSummaryResponse


class AnalyticsOverviewResponse(BaseModel):
    freshness: Literal["READY", "ROLLUP_PENDING"]
    generated_at: str | None
    range: Literal["24h", "7d", "30d"]
    from_utc: str
    to_utc: str
    source_id: str | None
    service_id: int | None
    signal_severity: Literal["critical", "warning", "info", "unknown"] | None
    response: AnalyticsResponseSection
    signal: AnalyticsSignalSection
    notifications: dict[str, AnalyticsNotificationModeResponse]
    ai: dict[str, AnalyticsAiModeResponse]


class JobResponse(BaseModel):
    id: str
    kind: str
    pool: str
    subject_type: str
    subject_id: str
    state: str
    payload_revision: int
    attempt: int
    available_at: str
    started_at: str | None
    finished_at: str | None
    safe_error_code: str | None
    version: int
    created_at: str
    updated_at: str


class PlatformHealthCheckResponse(BaseModel):
    name: str
    status: Literal["ready", "not_ready"]
    code: str


class PlatformReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    checked_at: str
    checks: list[PlatformHealthCheckResponse]


class PlatformJobRuntimeResponse(BaseModel):
    scheduler_running: bool
    scheduler_heartbeat_at: str | None
    runner_running: bool
    runner_heartbeat_at: str | None
    pending: int
    running: int
    expired_leases: int
    oldest_pending_seconds: int | None


class PlatformSourcePollResponse(BaseModel):
    source_id: str
    source_name: str
    lifecycle_state: str
    health: Literal["HEALTHY", "DEGRADED", "UNAVAILABLE", "DISABLED", "ARCHIVED"]
    endpoint_total: int
    endpoint_succeeded: int
    endpoint_failed: int
    last_poll_at: str | None
    last_complete_success_at: str | None
    last_any_success_at: str | None
    safe_error_codes: list[str]


class PlatformWatchdogCountsResponse(BaseModel):
    total: int
    healthy: int
    missing: int
    unknown: int


class PlatformWatchdogClusterResponse(BaseModel):
    cluster: str
    status: Literal["healthy", "missing", "unknown"]
    last_seen_at: str | None
    freshness_seconds: int | None


class PlatformWatchdogSourceClusterResponse(BaseModel):
    id: int | None
    source_id: str
    identity_value: str
    cluster: str
    inventory_state: str | None
    health_state: str | None
    status: Literal["healthy", "missing", "unknown"]
    first_discovered_at: str | None
    monitoring_started_at: str | None
    last_seen_at: str | None
    ignored_at: str | None
    ignored_reason: str | None
    still_emitting: bool
    freshness_seconds: int | None
    version: int | None


class PlatformWatchdogSourceResponse(BaseModel):
    source_id: str
    source_name: str
    source_health: Literal[
        "HEALTHY", "DEGRADED", "UNAVAILABLE", "DISABLED", "ARCHIVED"
    ]
    monitor_state: Literal[
        "ENABLED", "DISABLED", "SOURCE_DISABLED", "SOURCE_ARCHIVED"
    ]
    summary: PlatformWatchdogCountsResponse
    clusters: list[PlatformWatchdogSourceClusterResponse]


class PlatformWatchdogResponse(BaseModel):
    overall_status: Literal["healthy", "missing", "unknown", "no_data"]
    summary: PlatformWatchdogCountsResponse
    clusters: list[PlatformWatchdogClusterResponse]
    monitored_source_count: int
    unmonitored_source_count: int
    sources: list[PlatformWatchdogSourceResponse]


class PlatformNotificationHealthResponse(BaseModel):
    status: Literal["disabled", "healthy", "degraded"]
    worker_last_run_at: str | None
    pending: int
    retrying: int
    permanent_failed_24h: int
    oldest_pending_seconds: int | None
    active_channels: int
    active_policies: int
    last_error_code: str | None


class PlatformHealthResponse(BaseModel):
    status: str
    source_mode: Literal["REGISTRY", "UNCONFIGURED"]
    configuration_status: Literal["configured", "unconfigured", "degraded"]
    configuration_errors: list[str]
    last_poll_at: str | None
    last_poll_ok: bool | None
    last_poll_error: str | None
    incident_count: int
    last_backfill_at: str | None
    last_backfill_ok: bool | None
    last_backfill_error: str | None
    backfill_skipped: bool
    backfill_effective_hours: int
    backfill_truncated_reason: str | None
    alerts_reconstructed: int
    sources: list[PlatformSourcePollResponse]
    watchdog: PlatformWatchdogResponse
    notifications: PlatformNotificationHealthResponse
    readiness: PlatformReadinessResponse
    jobs: PlatformJobRuntimeResponse


class EventResponse(BaseModel):
    sequence: int
    event_type: str
    subject_type: str
    subject_id: str
    created_at: str


class EventPageResponse(BaseModel):
    items: list[EventResponse]
    next_cursor: str


class InvestigationDegradationResponse(BaseModel):
    domain: str
    code: str
    message: str
    impact: str
    preserved: str
    next_step: str


class PlannerStepResponse(BaseModel):
    sequence: int
    action: str
    outcome: str
    metric_name: str | None
    safe_code: str | None
    result_metric_names: list[str]


class MetricSamplePointResponse(BaseModel):
    timestamp: float
    value: str


class MetricObservationResponse(BaseModel):
    evidence_ref: str
    alert_ref: str
    metric_name: str
    series_count: int
    point_count: int
    minimum: float | None
    maximum: float | None
    latest: float | None
    sample: list[MetricSamplePointResponse]


class AnalystHypothesisResponse(BaseModel):
    title: str
    explanation: str
    verdict: Literal["SUPPORTED", "SYMPTOM", "DISPROVEN", "BLOCKED"]
    supporting_evidence_ids: list[str]
    contradicting_evidence_ids: list[str]
    missing_evidence: list[str]


class AnalystRecommendedActionResponse(BaseModel):
    kind: Literal["NEXT_CHECK", "MANUAL_MITIGATION", "RUNBOOK"]
    description: str
    risk: str
    evidence_ids: list[str]


class AnalystResultResponse(BaseModel):
    summary: str
    hypotheses: list[AnalystHypothesisResponse]
    missing_evidence: list[str]
    recommended_actions: list[AnalystRecommendedActionResponse]


class AlertEvidenceResponse(BaseModel):
    evidence_ref: str
    alertname: str


class InvestigationToolActionResponse(BaseModel):
    sequence: int
    action: str
    outcome: str
    metric_name: str | None
    window: str | None
    aggregation: str | None
    label_names: list[str]
    group_by: list[str]
    safe_code: str | None


class InvestigationUsageResponse(BaseModel):
    initiator_kind: Literal["INTERACTIVE_OPERATOR"]
    request_id: str
    source_ip: str
    planner_calls: int
    analyst_calls: int
    metric_queries: int
    accounted_tokens: int
    model_execution_mode: Literal["FAKE", "EXTERNAL", "UNKNOWN_LEGACY"]
    model_channel_id: str | None
    model_channel_revision: int | None
    prompt_profile_revision: int | None
    playbook_revision: int | None
    egress_categories: list[str]
    pricing_revision: int | None
    cost_status: Literal["NOT_INCURRED", "UNKNOWN"]
    p0_mtti_ms: int | None
    p1_mtti_ms: int | None
    p2_mtti_ms: int | None
    query_yield: float | None
    evidence_gain: int
    degradation_count: int
    canceled: bool
    tool_actions: list[InvestigationToolActionResponse]


class InvestigationDailyUsageResponse(BaseModel):
    day_utc: date
    schema_revision: int
    run_count: int
    terminal_run_count: int
    p2_valid_count: int
    evidence_only_count: int
    canceled_count: int
    contract_rejected_count: int
    dependency_failed_count: int
    model_started_run_count: int
    fake_model_started_run_count: int
    external_model_started_run_count: int
    unknown_model_started_run_count: int
    planner_calls: int
    analyst_calls: int
    metric_queries: int
    accounted_tokens: int
    unknown_cost_run_count: int
    feedback_response_count: int
    feedback_adopted_count: int
    degraded_run_count: int
    p0_mtti_count: int
    p0_mtti_sum_ms: int
    p1_mtti_count: int
    p1_mtti_sum_ms: int
    p2_mtti_count: int
    p2_mtti_sum_ms: int
    updated_at: str


class InvestigationFeedbackInput(BaseModel):
    rating: Literal["USEFUL", "NOT_USEFUL", "ADOPTED"]


class InvestigationFeedbackResponse(BaseModel):
    sequence: int
    rating: Literal["USEFUL", "NOT_USEFUL", "ADOPTED"]
    created_at: str


class InvestigationResponse(BaseModel):
    id: str
    occurrence_id: int
    incident_id: int
    status: Literal["PREPARING", "EVIDENCE_ONLY", "QUEUED", "FAILED"]
    phase: str
    member_total: int
    member_detailed: int
    query_planned: int
    query_completed: int
    successful_metric_facts: int
    empty_metric_facts: int
    foundation_successful_metric_facts: int
    foundation_empty_metric_facts: int
    degraded_domains: list[str]
    degradations: list[InvestigationDegradationResponse]
    findings: list[str]
    job_id: str | None
    model_cost_status: Literal["NOT_INCURRED", "UNKNOWN"]
    snapshot_occurrence_version: int | None
    planner_rounds: int
    metric_queries_total: int
    accounted_tokens: int
    termination_reason: str | None
    cancel_requested_at: str | None
    planner_steps: list[PlannerStepResponse]
    alert_evidence: list[AlertEvidenceResponse]
    metric_observations: list[MetricObservationResponse]
    analyst_result: AnalystResultResponse | None
    analyst_calls: int
    analyst_prompt_tokens: int
    analyst_completion_tokens: int
    prompt_profile_id: str | None
    prompt_profile_name: str | None
    prompt_profile_revision: int | None
    playbook_id: str | None
    playbook_revision: int | None
    usage: InvestigationUsageResponse
    feedback: InvestigationFeedbackResponse | None
    created_at: str
    updated_at: str


class StartInvestigationInput(BaseModel):
    prompt_profile_id: str | None = Field(default=None, max_length=128)


class InvestigatorMetricObservationResponse(BaseModel):
    evidence_id: str
    metric_id: str
    status: Literal["DATA", "EMPTY_NO_DATA", "SOURCE_UNAVAILABLE"]
    summary: dict[str, float | int | str | None]
    sample: list[MetricSamplePointResponse]


class InvestigatorActivityResponse(BaseModel):
    sequence: int
    kind: str
    status: str
    safe_code: str
    evidence_ids: list[str]


class InvestigatorFindingResponse(BaseModel):
    title_zh: str
    analysis_zh: str
    evidence_ids: list[str]


class InvestigatorActionResponse(BaseModel):
    title_zh: str
    rationale_zh: str
    evidence_ids: list[str]


class InvestigatorReportResponse(BaseModel):
    summary_zh: str
    verdict: Literal[
        "INCIDENT_CONFIRMED",
        "LIKELY_INCIDENT",
        "INCONCLUSIVE",
        "NO_INCIDENT_EVIDENCE",
    ]
    confidence: float
    findings: list[InvestigatorFindingResponse]
    recommended_actions: list[InvestigatorActionResponse]
    missing_evidence_zh: list[str]
    degraded_domains: list[str]
    evidence_gain: int


class InvestigatorRunResponse(BaseModel):
    schema_version: Literal["V2"] = "V2"
    id: str
    occurrence_id: int
    status: Literal[
        "QUEUED", "RUNNING", "COMPLETED", "DEGRADED", "FAILED", "CANCELED"
    ]
    job_id: str | None
    provider_profile_id: str | None
    model_channel_id: str | None
    model_revision: int | None
    alert_evidence: list[dict[str, object]]
    metric_evidence: list[InvestigatorMetricObservationResponse]
    degraded_domains: list[str]
    available_metric_count: int
    activities: list[InvestigatorActivityResponse]
    report: InvestigatorReportResponse | None
    request_count: int
    tool_call_count: int
    input_tokens: int
    output_tokens: int
    safe_error_code: str | None
    feedback: InvestigationFeedbackResponse | None
    created_at: str
    updated_at: str


class EndpointResponse(BaseModel):
    position: int
    canonical_url: str
    enabled: bool
    auth_kind: str
    username: str
    secret_configured: bool
    timeout_seconds: float


class SourceResponse(BaseModel):
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
    endpoints: list[EndpointResponse]
    created_at: str
    updated_at: str
    last_poll_at: str | None
    last_poll_completeness: str | None
    last_poll_safe_error_codes: list[str]


class SourceAuditResponse(BaseModel):
    sequence: int
    source_id: str
    action: str
    source_version: int
    changed_at: str


class WatchdogClusterResponse(BaseModel):
    id: int
    source_id: str
    identity_value: str
    inventory_state: str
    health_state: str
    last_observed_at: str | None


class AlertResponse(BaseModel):
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
    starts_at: str | None
    missing_since_at: str | None
    origin: str
    evidence_completeness: str
    last_seen_at: str


class IncidentResponse(BaseModel):
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
    occurrence_started_at: str
    updated_at: str
    member_count: int
    aggregation_rule_id: int | None
    source_name: str
    aggregation_rule_name: str | None
    aggregation_status: Literal["matched", "unmatched", "missing_labels"]


class IncidentHandlingInput(BaseModel):
    state: Literal["IN_PROGRESS", "CLOSED", "FALSE_POSITIVE"]
    reason: str = Field(min_length=1, max_length=2000)
    actor: str = Field(default="local-user", min_length=1, max_length=128)
    expected_version: int = Field(ge=1)


class IncidentHandlingAuditResponse(BaseModel):
    id: int
    incident_id: int
    actor: str
    from_state: str
    to_state: str
    reason: str
    created_at: str


class IncidentDetailResponse(BaseModel):
    id: int
    source_id: str
    source_name: str
    group_key: str
    title: str
    severity: str
    source_state: str
    freshness_state: str
    handling_state: str
    handling_version: int
    occurrence_no: int
    occurrence_started_at: str
    updated_at: str
    member_count: int
    aggregation_rule_id: int | None
    aggregation_rule_name: str | None
    aggregation_status: Literal["matched", "unmatched", "missing_labels"]
    grouping_explanation: str
    aggregation_rule_version: int | None
    members: list[AlertResponse]
    handling_history: list[IncidentHandlingAuditResponse]


class IncidentOccurrenceResponse(BaseModel):
    id: int
    incident_id: int
    occurrence_no: int
    source_id: str
    source_name: str
    group_key: str
    title: str
    aggregation_rule_id: int | None
    aggregation_rule_name: str | None
    started_at: str
    recovered_at: str
    member_count: int
    member_max_severity: str
    handling_conclusion: str


class IncidentOccurrencePageResponse(BaseModel):
    items: list[IncidentOccurrenceResponse]
    next_cursor: str | None


class OperationalOccurrenceResponse(BaseModel):
    id: int
    incident_id: int
    occurrence_no: int
    source_id: str
    source_name: str
    group_key: str
    title: str
    signal_state: Literal["FIRING", "RECOVERED", "UNKNOWN", "STALE"]
    signal_severity: str
    response_state: Literal[
        "UNACKNOWLEDGED",
        "IN_PROGRESS",
        "RESOLVED",
    ]
    resolution_code: str | None
    duplicate_of_occurrence_id: int | None
    service_id: int | None
    service_name: str | None
    assignment_origin: Literal["UNMAPPED", "MAPPING", "MANUAL"]
    service_assignment_state: Literal[
        "UNMAPPED", "MAPPED", "SERVICE_AMBIGUOUS", "SERVICE_ARCHIVED"
    ]
    member_count: int
    evidence_completeness: Literal["COMPLETE", "PARTIAL", "FAILED", "UNKNOWN"]
    detected_at: str | None
    source_started_at: str | None
    ack_sla_seconds: int
    ack_sla_due_at: str | None
    acknowledged_at: str | None
    resolved_at: str | None
    ack_sla_state: Literal[
        "NOT_STARTED", "ON_TRACK", "AT_RISK", "BREACHED", "STOPPED"
    ]
    ack_sla_remaining_seconds: int | None
    noise_state: Literal[
        "NONE", "GROUPING", "FLAPPING", "STORM", "MAINTENANCE", "SUPPRESSED"
    ]
    noise_reason: str | None
    noise_scope: str | None
    noise_starts_at: str | None
    noise_ends_at: str | None
    noise_remaining_seconds: int | None
    latest_activity_at: str
    version: int


class OperationalOccurrencePageResponse(BaseModel):
    items: list[OperationalOccurrenceResponse]
    next_cursor: str | None
    evaluated_at: str


class SimilarHistoryMatchReasonResponse(BaseModel):
    kind: Literal["SERVICE", "PRIMARY_ALERTNAME", "AGGREGATION_RULE", "GROUP_LABEL"]
    field: str
    value: str
    points: int


class SimilarHistoryResponse(BaseModel):
    occurrence_id: int
    occurrence_no: int
    title: str
    score: int
    match_reasons: list[SimilarHistoryMatchReasonResponse]
    resolution_code: str
    operator_conclusion: str | None
    task_outcome: str | None
    handling_duration_seconds: int
    resolved_at: str


class TimelineEntryResponse(BaseModel):
    id: int
    occurrence_id: int
    sequence: int
    actor_type: Literal["INTERACTIVE_OPERATOR", "SYSTEM"]
    event_type: Literal[
        "RESPONSE_ACKNOWLEDGED",
        "ACKNOWLEDGED_IMPLICIT",
        "RESPONSE_STATE_CHANGED",
        "RESPONSE_PRIORITY_CHANGED",
        "RESPONSE_HANDLING_STARTED",
        "OCCURRENCE_RESOLVED",
        "LEGACY_HANDLING_IMPORTED",
        "TASK_CREATED",
        "TASK_STATE_CHANGED",
        "MANUAL_NOTE",
        "NOTE_REDACTED",
        "SERVICE_MAPPING_UPDATED",
        "SERVICE_ASSIGNMENT_CHANGED",
        "SUPPRESSION_STARTED",
        "SUPPRESSION_ENDED",
    ]
    summary: str
    detail: dict[str, Any]
    request_id: str
    source_ip: str
    created_at: str


class IncidentTaskResponse(BaseModel):
    id: int
    occurrence_id: int
    title: str
    description: str | None
    due_at: str | None
    runbook_link: str | None
    status: Literal["TODO", "IN_PROGRESS", "DONE", "CANCELED"]
    result: str | None
    version: int
    created_at: str
    updated_at: str


class ServiceInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    slug: str = Field(min_length=1, max_length=63)
    criticality: Literal["TIER_0", "TIER_1", "TIER_2", "TIER_3"]
    links: list[str] = Field(default_factory=list, max_length=10)


class ServiceUpdateInput(ServiceInput):
    expected_version: int = Field(ge=1)


class ServiceArchiveInput(BaseModel):
    expected_version: int = Field(ge=1)


class ServiceResponse(BaseModel):
    id: int
    name: str
    slug: str
    criticality: Literal["TIER_0", "TIER_1", "TIER_2", "TIER_3"]
    ack_sla_seconds: int
    status: Literal["ACTIVE", "ARCHIVED"]
    links: list[str]
    version: int
    created_at: str
    updated_at: str


class ServiceAuditResponse(BaseModel):
    id: int
    service_id: int
    action: Literal["CREATED", "UPDATED", "ARCHIVED"]
    service_version: int
    detail: dict[str, Any]
    changed_at: str


class ServiceMappingRuleInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    priority: int = Field(default=100, ge=0, le=1_000_000)
    service_id: int = Field(ge=1)
    enabled: bool = True
    source_ids: list[str] = Field(default_factory=list, max_length=20)
    matchers: list["MatcherInput"] = Field(default_factory=list, max_length=20)


class ServiceMappingRuleUpdateInput(ServiceMappingRuleInput):
    expected_version: int = Field(ge=1)


class ServiceMappingPublishInput(BaseModel):
    expected_version: int = Field(ge=1)


class ServiceMappingRuleResponse(BaseModel):
    id: int
    name: str
    priority: int
    service_id: int
    service_name: str
    enabled: bool
    source_ids: list[str]
    matchers: list["MatcherInput"]
    version: int
    published_version: int
    published_at: str | None
    has_unpublished_changes: bool
    created_at: str
    updated_at: str


class ServiceMappingPreviewSampleResponse(BaseModel):
    occurrence_id: int
    title: str
    assignment_state: Literal["UNMAPPED", "MAPPED", "SERVICE_AMBIGUOUS"]
    service_ids: list[int]


class ServiceMappingPreviewResponse(BaseModel):
    rule_id: int
    matched_alert_count: int
    mapped_occurrence_count: int
    ambiguous_occurrence_count: int
    unmapped_occurrence_count: int
    samples: list[ServiceMappingPreviewSampleResponse]


class ServiceMappingPublishResponse(BaseModel):
    rule: ServiceMappingRuleResponse
    reprojection_job: JobResponse


class SourceNoiseControlsResponse(BaseModel):
    source_id: str
    flapping_enabled: bool
    storm_enabled: bool
    storm_alert_threshold: int
    storm_occurrence_threshold: int
    storm_active: bool
    storm_started_at: str | None
    version: int


class SourceNoiseControlsInput(BaseModel):
    flapping_enabled: bool = True
    storm_enabled: bool = True
    storm_alert_threshold: int = Field(default=100, ge=10, le=10_000)
    storm_occurrence_threshold: int = Field(default=20, ge=5, le=1_000)
    expected_version: int = Field(ge=1)


class MaintenanceInput(BaseModel):
    scope_kind: Literal["SOURCE", "SERVICE", "RULE"]
    source_id: str | None = Field(default=None, max_length=128)
    service_id: int | None = Field(default=None, ge=1)
    aggregation_rule_id: int | None = Field(default=None, ge=1)
    starts_at: datetime
    ends_at: datetime
    reason: str = Field(min_length=1, max_length=1000)


class MaintenanceEndInput(BaseModel):
    expected_version: int = Field(ge=1)


class MaintenanceResponse(BaseModel):
    id: int
    scope_kind: Literal["SOURCE", "SERVICE", "RULE"]
    source_id: str | None
    service_id: int | None
    aggregation_rule_id: int | None
    starts_at: str
    ends_at: str
    reason: str
    status: Literal["ACTIVE", "ENDED"]
    ended_at: str | None
    version: int
    created_at: str


class SuppressionInput(BaseModel):
    duration_seconds: Literal[900, 3600, 14400, 86400]
    reason: str = Field(min_length=1, max_length=1000)


class SuppressionEndInput(BaseModel):
    expected_version: int = Field(ge=1)


class SuppressionResponse(BaseModel):
    id: int
    occurrence_id: int
    starts_at: str
    ends_at: str
    reason: str
    status: Literal["ACTIVE", "ENDED"]
    ended_at: str | None
    version: int
    created_at: str


class OccurrenceNoiseResponse(BaseModel):
    state: Literal["NONE", "GROUPING", "FLAPPING", "STORM", "MAINTENANCE", "SUPPRESSED"]
    reason: str | None
    scope: str | None
    starts_at: str | None
    ends_at: str | None
    suppression_id: int | None
    suppression_version: int | None


class ServiceAssignmentInput(BaseModel):
    service_id: int = Field(ge=1)
    expected_version: int = Field(ge=1)


class ServiceAssignmentResponse(BaseModel):
    occurrence_id: int
    service_id: int
    service_name: str
    assignment_origin: Literal["MANUAL"]
    service_assignment_state: Literal["MAPPED"]
    version: int
    timeline: TimelineEntryResponse
    replayed: bool


class IncidentTaskCreateInput(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    due_at: datetime | None = None
    runbook_link: str | None = Field(default=None, max_length=2048)


class IncidentTaskTransitionInput(BaseModel):
    expected_version: int = Field(ge=1)
    target: Literal["IN_PROGRESS", "DONE", "CANCELED"]
    result: str | None = Field(default=None, max_length=4000)
    reason: str | None = Field(default=None, max_length=2000)


class IncidentNoteCreateInput(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class IncidentNoteRedactInput(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class CollaborationCommandResponse(BaseModel):
    task: IncidentTaskResponse | None
    timeline: TimelineEntryResponse
    replayed: bool


class ResponseCommandResponse(BaseModel):
    occurrence_id: int
    previous_state: str
    current_state: str
    resolution_code: str | None
    version: int
    timeline: list[TimelineEntryResponse]
    replayed: bool


class OccurrenceStartHandlingInput(BaseModel):
    expected_version: int = Field(ge=1)
    reason: str | None = Field(default=None, max_length=2000)


class OccurrenceResolveInput(BaseModel):
    expected_version: int = Field(ge=1)
    resolution_code: Literal[
        "FIXED", "SELF_RECOVERED", "FALSE_POSITIVE", "DUPLICATE", "NO_ACTION"
    ]
    duplicate_of_occurrence_id: int | None = Field(default=None, ge=1)
    reason: str | None = Field(default=None, max_length=2000)


class BatchStartHandlingItemInput(BaseModel):
    occurrence_id: int = Field(ge=1)
    expected_version: int = Field(ge=1)


class BatchStartHandlingInput(BaseModel):
    items: list[BatchStartHandlingItemInput] = Field(min_length=1, max_length=50)
    reason: str | None = Field(default=None, max_length=2000)


class BatchStartHandlingItemResponse(BaseModel):
    occurrence_id: int
    ok: bool
    command: ResponseCommandResponse | None = None
    error_code: str | None = None
    message: str | None = None


class BatchStartHandlingResponse(BaseModel):
    items: list[BatchStartHandlingItemResponse]
    succeeded: int
    failed: int


class RuleResponse(BaseModel):
    id: int
    name: str
    priority: int
    enabled: bool
    version: int
    matchers: list[MatcherInput]
    group_by_labels: list[str]
    source_ids: list[str]
    grouping_window_seconds: Literal[0, 30, 60, 120, 300]
    created_at: str
    updated_at: str


class AggregationLabelResponse(BaseModel):
    name: str
    coverage: float
    present_count: int
    total_count: int
    distinct_count: int
    sample_values: list[str]
    sources: list[str]


class AggregationLabelCatalogResponse(BaseModel):
    lookback_hours: Literal[24, 168, 720]
    history_status: Literal["ok", "unconfigured", "error"]
    history_error: str | None
    history_series_count: int
    labels: list[AggregationLabelResponse]


class MatcherInput(BaseModel):
    label: str = Field(min_length=1, max_length=128)
    operator: str
    value: str = Field(max_length=512)


class RuleInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    priority: int = Field(ge=0, le=1_000_000)
    enabled: bool = True
    matchers: list[MatcherInput] = Field(max_length=20)
    group_by_labels: list[str] = Field(min_length=1, max_length=20)
    source_ids: list[str] = Field(default_factory=list, max_length=20)
    grouping_window_seconds: Literal[0, 30, 60, 120, 300] = 30


class RulePreviewInput(RuleInput):
    rule_id: int | None = Field(default=None, ge=1)


class RuleUpdateInput(RuleInput):
    expected_version: int = Field(ge=1)


class RulePreviewGroupResponse(BaseModel):
    source_id: str
    group_key: str
    fingerprints: list[str]


class RulePreviewSourceResponse(BaseModel):
    source_id: str
    source_name: str
    matcher_alert_count: int
    selected_alert_count: int
    proposed_group_count: int


class RulePreviewResponse(BaseModel):
    matcher_alert_count: int
    selected_alert_count: int
    proposed_group_count: int
    groups: list[RulePreviewGroupResponse]
    by_source: list[RulePreviewSourceResponse]


class CollectSourceInput(BaseModel):
    expected_version: int = Field(ge=1)


class JobAcceptedResponse(BaseModel):
    job_id: str
    state: str


class SecretUpdateInput(BaseModel):
    action: Literal["KEEP", "REPLACE", "CLEAR"] = "KEEP"
    value: str | None = Field(default=None, max_length=8192)


class EndpointConfigInput(BaseModel):
    position: int = Field(ge=0, le=7)
    url: str = Field(min_length=1, max_length=2048)
    enabled: bool = True
    auth_kind: Literal["NONE", "BEARER", "BASIC"] = "NONE"
    username: str = Field(default="", max_length=256)
    secret: SecretUpdateInput = Field(default_factory=SecretUpdateInput)
    timeout_seconds: float = Field(default=10.0, ge=1, le=120)


class SourceConfigInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    endpoints: list[EndpointConfigInput] = Field(min_length=1, max_length=8)
    poll_interval_seconds: int = Field(default=30, ge=5, le=3600)
    resolution_grace_seconds: int = Field(default=60, ge=0, le=86400)
    max_parallel_endpoints: int = Field(default=4, ge=1, le=8)
    watchdog_enabled: bool = False
    watchdog_alertname: str = Field(default="Watchdog", min_length=1, max_length=128)
    watchdog_identity_label: str = Field(default="cluster", min_length=1, max_length=128)
    watchdog_missing_after_seconds: int = Field(default=90, ge=60, le=86400)


class SourceUpdateInput(SourceConfigInput):
    expected_version: int = Field(ge=1)


class SourceLifecycleInput(BaseModel):
    expected_version: int = Field(ge=1)


class ExpectedWatchdogClusterInput(BaseModel):
    identity_value: str = Field(min_length=1, max_length=256)


class WatchdogInventoryInput(BaseModel):
    inventory_state: Literal["DISCOVERED", "EXPECTED", "IGNORED"]


class EndpointTestResponse(BaseModel):
    position: int
    ok: bool
    status: str
    safe_error_code: str | None
    duration_ms: int


class SourceTestResponse(BaseModel):
    ok: bool
    completeness: str
    safe_error_codes: list[str]
    endpoints: list[EndpointTestResponse]


class MonitoringConnectionInput(BaseModel):
    base_url: str = Field(min_length=1, max_length=2048)
    secret: SecretUpdateInput = Field(default_factory=SecretUpdateInput)
    expected_version: int | None = Field(default=None, ge=1)


class MonitoringConnectionResponse(BaseModel):
    source_id: str
    kind: str
    base_url: str
    state: str
    secret_configured: bool
    tested_at: str | None
    last_test_code: str | None
    version: int


class MonitoringTestInput(BaseModel):
    expected_version: int = Field(ge=1)


class DashboardSearchResponse(BaseModel):
    uid: str
    title: str
    url: str


class GrafanaPreviewInput(BaseModel):
    dashboard_uid: str = Field(min_length=1, max_length=64)


class GrafanaCandidateResponse(BaseModel):
    dashboard_uid: str
    dashboard_title: str
    panel_id: int
    panel_title: str
    ref_id: str
    imported_promql: str
    required_variables: list[str]
    legend_format: str
    unit: str
    status: Literal["READY", "NEEDS_DECISION", "UNSUPPORTED"]
    reason: str
    change_kind: Literal[
        "NEW", "UNCHANGED", "UPSTREAM_CHANGED", "CONFLICT", "GONE"
    ]
    probe_status: str
    template_id: int | None = None
    current_promql: str = ""


class GrafanaImportSelectionInput(BaseModel):
    dashboard_uid: str = Field(min_length=1, max_length=64)
    dashboard_title: str = Field(min_length=1, max_length=256)
    panel_id: int
    panel_title: str = Field(min_length=1, max_length=256)
    ref_id: str = Field(min_length=1, max_length=32)
    imported_promql: str = Field(min_length=1, max_length=4096)
    required_variables: list[str] = Field(default_factory=list, max_length=30)
    legend_format: str = Field(default="", max_length=256)
    unit: str = Field(default="", max_length=64)
    name: str = Field(min_length=1, max_length=160)
    final_promql: str = Field(min_length=1, max_length=4096)
    priority: int = Field(ge=0, le=1_000_000)


class GrafanaConfirmInput(BaseModel):
    selections: list[GrafanaImportSelectionInput] = Field(min_length=1, max_length=40)


class MetricTemplateInput(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    promql: str = Field(min_length=1, max_length=4096)
    enabled: bool = True
    priority: int = Field(default=100, ge=0, le=1_000_000)
    source_ids: list[str] = Field(default_factory=list, max_length=20)
    description: str = Field(default="", max_length=2000)
    required_labels: list[str] = Field(default_factory=list, max_length=16)
    legend_format: str = Field(default="", max_length=256)
    unit: str = Field(default="", max_length=64)


class MetricTemplateUpdateInput(MetricTemplateInput):
    expected_version: int = Field(ge=1)


class VersionInput(BaseModel):
    expected_version: int = Field(ge=1)


class GrafanaOriginResponse(BaseModel):
    source_id: str
    dashboard_uid: str
    dashboard_title: str
    panel_id: int
    panel_title: str
    base_url: str


class MetricTemplateResponse(BaseModel):
    id: int
    name: str
    promql: str
    enabled: bool
    priority: int
    source_ids: list[str]
    origin_kind: str
    version: int
    description: str
    required_labels: list[str]
    legend_format: str
    unit: str
    builtin_key: str | None
    user_modified: bool
    grafana_origin: GrafanaOriginResponse | None


class MetricReadResultResponse(BaseModel):
    status: str
    series: list[dict[str, Any]]
    safe_error_code: str | None


class EvidenceCurveResponse(BaseModel):
    kind: str
    name: str
    promql: str
    result: MetricReadResultResponse
    legend_format: str
    unit: str
    deep_link: str | None


class MetricEvidenceResponse(BaseModel):
    alert_id: int
    alert_starts_at: str | None
    window_start: str
    window_end: str
    step_seconds: int
    curves: list[EvidenceCurveResponse]
    failures: list[str]


class ModelChannelInputSchema(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: Literal["OPENAI_COMPATIBLE"] = "OPENAI_COMPATIBLE"
    provider_id: Literal["OPENAI", "DEEPSEEK", "MOONSHOT", "ZHIPU", "DASHSCOPE", "CUSTOM"] = "CUSTOM"
    base_url: str = Field(min_length=1, max_length=2048)
    model: str = Field(min_length=1, max_length=256)
    api_key: SecretUpdateInput = Field(default_factory=SecretUpdateInput)


class ModelChannelUpdateInput(ModelChannelInputSchema):
    expected_revision: int = Field(ge=1)


class ModelChannelActionInput(BaseModel):
    expected_revision: int = Field(ge=1)


class ModelChannelResponse(BaseModel):
    id: str
    name: str
    kind: str
    enabled: bool
    revision_no: int
    state: str
    base_url: str
    model: str
    api_key_configured: bool
    tested_at: str | None
    last_test_code: str | None
    provider_profile_id: str | None
    provider_id: str
    protocol_profile: str
    support_level: str


class ModelVendorResponse(BaseModel):
    id: str
    label: str
    base_url: str
    key_hint: str
    recommended_models: list[str]
    protocol_profile: str
    support_level: str


class ModelListResponse(BaseModel):
    models: list[str]


class ModelRuntimeResponse(BaseModel):
    configured: bool
    channel_id: str | None
    model: str | None
    fake_mode: bool


class PromptGuidanceInput(BaseModel):
    organization_context: str = Field(default="", max_length=2_000)
    investigation_focus: str = Field(default="", max_length=2_000)
    terminology: str = Field(default="", max_length=2_000)
    response_style: str = Field(default="", max_length=2_000)


class PromptProfileCopyInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class PromptProfileUpdateInput(PromptGuidanceInput):
    expected_revision: int = Field(ge=1)


class PromptProfileActionInput(BaseModel):
    expected_revision: int = Field(ge=1)


class PromptProfileActivateInput(PromptProfileActionInput):
    global_default: bool = False
    service_ids: list[int] = Field(default_factory=list, max_length=100)


class PromptProfileResponse(BaseModel):
    id: str
    name: str
    builtin: bool
    status: Literal["DRAFT", "ACTIVE", "RETIRED"]
    revision: int
    active_revision: int | None
    guidance: PromptGuidanceInput
    is_global_default: bool
    service_ids: list[int]
    tested_at: str | None
    last_test_code: str | None


class PromptProfileRevisionResponse(BaseModel):
    revision: int
    status: Literal["DRAFT", "ACTIVE", "RETIRED"]
    guidance: PromptGuidanceInput
    tested_at: str | None
    last_test_code: str | None
    created_at: str | None
    changed_fields: list[
        Literal[
            "organization_context",
            "investigation_focus",
            "terminology",
            "response_style",
        ]
    ]


class PromptProfilePreviewResponse(BaseModel):
    profile_id: str
    revision: int
    safety_kernel: list[str]
    operator_guidance: dict[str, str]
    egress_categories: list[str]
    estimated_tokens: int
    maximum_cost: Literal["UNKNOWN"]


class NotificationChannelInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    provider: Literal["FEISHU_CUSTOM_BOT", "SMTP", "GENERIC_WEBHOOK"]
    config: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, SecretUpdateInput] = Field(default_factory=dict)


class NotificationChannelUpdateInput(NotificationChannelInput):
    expected_revision: int = Field(ge=1)


class NotificationChannelActionInput(BaseModel):
    expected_revision: int = Field(ge=1)


class NotificationChannelResponse(BaseModel):
    id: str
    name: str
    provider: str
    enabled: bool
    revision_no: int
    revision_state: str
    config: dict[str, Any]
    secret_configured: dict[str, bool]
    tested_at: str | None
    last_test_code: str | None


class NotificationMatcherInput(BaseModel):
    field: str = Field(min_length=1, max_length=128)
    operator: Literal["=", "!=", "=~", "!~"]
    value: str = Field(max_length=512)


class NotificationPolicyInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    priority: int = Field(default=100, ge=-1_000_000, le=1_000_000)
    matchers: list[NotificationMatcherInput] = Field(default_factory=list, max_length=20)
    repeat_interval_seconds: int = Field(default=14_400, ge=0, le=2_592_000)
    channel_ids: list[str] = Field(min_length=1, max_length=20)
    scope_mode: Literal["ALL", "SELECTED"] = "ALL"
    source_ids: list[str] = Field(default_factory=list, max_length=20)


class NotificationPolicyUpdateInput(NotificationPolicyInput):
    expected_version: int = Field(ge=1)


class NotificationPolicyResponse(BaseModel):
    revision_id: int
    logical_id: str
    version: int
    name: str
    state: str
    priority: int
    matchers: list[NotificationMatcherInput]
    repeat_interval_seconds: int
    channel_ids: list[str]
    scope_mode: str
    source_ids: list[str]


class NotificationPolicyPreviewSampleResponse(BaseModel):
    incident_id: int
    winner_policy_id: int | None
    winner_policy_name: str | None


class NotificationPolicyPreviewResponse(BaseModel):
    direct_match_count: int
    shadowed_count: int
    final_match_count: int
    firing_match_count: int
    unrouted_count: int
    disabled_channel_count: int
    samples: list[NotificationPolicyPreviewSampleResponse]


class NotificationPolicyActivationPrepareInput(BaseModel):
    expected_version: int = Field(ge=1)
    notify_existing: bool = False


class NotificationPolicyActionInput(BaseModel):
    expected_version: int = Field(ge=1)


class NotificationPolicyActivationPrepareResponse(BaseModel):
    confirm_token: str
    eligible_incident_count: int
    expires_at_epoch: int


class NotificationPolicyActivationInput(NotificationPolicyActivationPrepareInput):
    confirm_token: str = Field(min_length=16, max_length=4096)


class NotificationAttemptResponse(BaseModel):
    id: int
    attempt_no: int
    trigger: str
    started_at: str
    finished_at: str | None
    outcome: str | None
    http_status: int | None
    provider_request_id: str | None
    error_code: str | None


class NotificationDeliveryResponse(BaseModel):
    id: int
    event_key: str
    incident_id: int
    occurrence_no: int
    route_id: int
    route_target_id: int
    channel_id: str
    channel_name: str
    provider: str
    event_type: str
    state: str
    attempt_count: int
    scheduled_at: str
    next_attempt_at: str
    suppression_reason: str | None
    payload_snapshot: dict[str, Any]
    succeeded_at: str | None
    attempts: list[NotificationAttemptResponse] = Field(default_factory=list)


class NotificationRetryResponse(BaseModel):
    delivery: NotificationDeliveryResponse
    warning: str


class NotificationRouteResponse(BaseModel):
    id: int
    status: str
    policy_name: str
    policy_version: int
    repeat_interval_seconds: int
    next_reminder_at: str | None
    termination_reason: str | None


class NotificationRouteTargetResponse(BaseModel):
    id: int
    channel_id: str
    channel_name: str
    provider: str
    channel_revision_no: int
    opened_success_at: str | None
    last_success_at: str | None


class IncidentNotificationResponse(BaseModel):
    incident_id: int
    occurrence_no: int
    route: NotificationRouteResponse | None
    targets: list[NotificationRouteTargetResponse]
    deliveries: list[NotificationDeliveryResponse]


class WorkbenchUrlInput(BaseModel):
    url: str = Field(max_length=2048)


class WorkbenchUrlResponse(BaseModel):
    url: str
