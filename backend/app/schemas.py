"""API response schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, Optional

from pydantic import BaseModel, Field, PlainSerializer, field_validator

from app.platform.utc import to_utc_iso
from app.services.panel_links import normalize_web_url


# Datetime that always serializes as UTC ISO-8601 with a trailing Z, so browsers
# parse it as UTC rather than local time.
UTCDateTime = Annotated[datetime, PlainSerializer(to_utc_iso, return_type=str)]


class AlertOut(BaseModel):
    id: int
    fingerprint: str
    source_id: str = "legacy"
    alertname: str
    severity: str
    cluster: str
    labels: dict[str, Any]
    annotations: dict[str, Any]
    starts_at: Optional[UTCDateTime]
    ends_at: Optional[UTCDateTime]
    source_state: str
    missing_since_at: Optional[UTCDateTime]
    origin: str
    evidence_completeness: str
    first_seen_at: UTCDateTime
    last_seen_at: UTCDateTime


class IncidentSummary(BaseModel):
    id: int
    source_id: str = "legacy"
    source_name: Optional[str] = None
    title: str
    severity: str
    source_state: str
    freshness_state: str = "FRESH"
    handling_state: str
    member_count: int
    updated_at: UTCDateTime
    aggregation_rule_id: Optional[int]
    aggregation_rule_name: Optional[str]
    aggregation_status: Literal["matched", "unmatched", "missing_labels"]


HandlingStateValue = Literal["NEW", "IN_PROGRESS", "CLOSED", "FALSE_POSITIVE"]


class HandlingAuditOut(BaseModel):
    id: int
    incident_id: int
    actor: str
    from_state: HandlingStateValue
    to_state: HandlingStateValue
    reason: str
    created_at: UTCDateTime


class HandlingUpdate(BaseModel):
    state: HandlingStateValue
    reason: str
    actor: str = "local-user"

    @field_validator("reason", "actor")
    @classmethod
    def non_empty_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value


class IncidentDetail(IncidentSummary):
    group_key: str
    grouping_explanation: str
    policy_version: int
    created_at: UTCDateTime
    members: list[AlertOut]
    handling_history: list[HandlingAuditOut]


class PanelMappingPut(BaseModel):
    alertname: str
    links: list[str]

    @field_validator("alertname")
    @classmethod
    def validate_alertname(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("alertname must not be empty")
        return value

    @field_validator("links")
    @classmethod
    def validate_links(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            url = normalize_web_url(value)
            if url is None:
                raise ValueError("links must contain only absolute HTTP(S) URLs")
            if url not in seen:
                normalized.append(url)
                seen.add(url)
        return normalized


class PanelMappingOut(BaseModel):
    alertname: str
    links: list[str]


class PanelMappingsOut(BaseModel):
    mappings: list[PanelMappingOut]


class GroupingPolicyOut(BaseModel):
    version: int
    group_by: list[str]
    enabled: bool
    created_at: UTCDateTime


class GroupingPolicyPut(BaseModel):
    group_by: list[str]

    @field_validator("group_by")
    @classmethod
    def validate_group_by_fields(cls, value: list[str]) -> list[str]:
        from app.services.policies import validate_group_by

        try:
            return validate_group_by(value)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc


class AggregationMatcherIn(BaseModel):
    label: str
    operator: Literal["=", "!=", "=~", "!~"]
    value: str


class SourceScope(BaseModel):
    mode: Literal["ALL", "SELECTED"] = "ALL"
    source_ids: list[str] = Field(default_factory=list)


class AggregationRulePut(BaseModel):
    name: str
    priority: int = 100
    enabled: bool = True
    matchers: list[AggregationMatcherIn] = Field(default_factory=list)
    group_by_labels: list[str] = Field(default_factory=list)
    source_scope: SourceScope = Field(default_factory=SourceScope)


class AggregationRulePreviewPut(AggregationRulePut):
    rule_id: Optional[int] = None


class AggregationRuleOut(AggregationRulePut):
    id: int
    version: int
    created_at: UTCDateTime
    updated_at: UTCDateTime


class AggregationPreviewGroupOut(BaseModel):
    group_key: str
    title: str
    fingerprints: list[str]
    missing_labels: list[str]


class AggregationPreviewSourceOut(BaseModel):
    source_id: str
    source_name: Optional[str] = None
    matcher_alert_count: int
    selected_alert_count: int
    proposed_group_count: int


class AggregationRulePreviewOut(BaseModel):
    matcher_alert_count: int
    selected_alert_count: int
    proposed_group_count: int
    groups: list[AggregationPreviewGroupOut]
    by_source: list[AggregationPreviewSourceOut] = Field(default_factory=list)


class AggregationLabelOut(BaseModel):
    name: str
    coverage: float
    present_count: int
    total_count: int
    distinct_count: int
    sample_values: list[str]
    sources: list[Literal["current", "history"]]


class AggregationLabelCatalogOut(BaseModel):
    lookback_hours: Literal[24, 168, 720]
    history_status: Literal["ok", "unconfigured", "error"]
    history_error: Optional[str]
    history_series_count: int
    labels: list[AggregationLabelOut]


class GrafanaDashboardSearchOut(BaseModel):
    uid: str
    title: str
    folder: str


class GrafanaPanelDefinitionOut(BaseModel):
    id: str
    title: str
    type: str


class GrafanaDashboardDefinitionOut(BaseModel):
    uid: str
    title: str
    panels: list[GrafanaPanelDefinitionOut]
    variables: list[str]


class WatchdogSummary(BaseModel):
    total: int
    healthy: int
    missing: int
    unknown: int


class WatchdogClusterHealth(BaseModel):
    cluster: str
    status: Literal["healthy", "missing", "unknown"]
    last_seen_at: Optional[UTCDateTime]
    freshness_seconds: Optional[int]


class WatchdogSourceClusterHealth(BaseModel):
    id: Optional[int] = None
    source_id: str
    identity_value: str
    cluster: str
    inventory_state: Optional[Literal["DISCOVERED", "EXPECTED", "IGNORED"]] = None
    health_state: Optional[Literal["HEALTHY", "MISSING", "UNKNOWN"]] = None
    status: Literal["healthy", "missing", "unknown"]
    first_discovered_at: Optional[UTCDateTime] = None
    monitoring_started_at: Optional[UTCDateTime] = None
    last_seen_at: Optional[UTCDateTime]
    ignored_at: Optional[UTCDateTime] = None
    ignored_reason: Optional[str] = None
    still_emitting: bool = False
    freshness_seconds: Optional[int]
    version: Optional[int] = None


class WatchdogSourceHealth(BaseModel):
    source_id: str
    source_name: str
    source_health: Literal[
        "HEALTHY", "DEGRADED", "UNAVAILABLE", "DISABLED", "ARCHIVED"
    ]
    monitor_state: Literal["ENABLED", "DISABLED", "SOURCE_DISABLED", "SOURCE_ARCHIVED"]
    summary: WatchdogSummary
    clusters: list[WatchdogSourceClusterHealth]


class WatchdogHealth(BaseModel):
    overall_status: Literal["healthy", "missing", "unknown", "no_data"]
    summary: WatchdogSummary
    clusters: list[WatchdogClusterHealth]
    monitored_source_count: int = 0
    unmonitored_source_count: int = 0
    sources: list[WatchdogSourceHealth] = Field(default_factory=list)


class NotificationHealth(BaseModel):
    status: Literal["disabled", "healthy", "degraded"]
    worker_last_run_at: Optional[UTCDateTime]
    pending: int
    retrying: int
    permanent_failed_24h: int
    oldest_pending_seconds: Optional[int]
    active_channels: int
    active_policies: int
    last_error_code: Optional[str]


class SourcePollHealth(BaseModel):
    source_id: str
    source_name: str
    lifecycle_state: str
    health: Literal["HEALTHY", "DEGRADED", "UNAVAILABLE", "DISABLED", "ARCHIVED"]
    endpoint_total: int
    endpoint_succeeded: int
    endpoint_failed: int
    last_poll_at: Optional[UTCDateTime]
    last_complete_success_at: Optional[UTCDateTime]
    last_any_success_at: Optional[UTCDateTime]
    safe_error_codes: list[str]


class Health(BaseModel):
    status: str
    # F21: the registry is the only source of truth, so an empty registry means
    # nothing is configured -- there is no `.env` path left to fall back to.
    source_mode: Literal["REGISTRY", "UNCONFIGURED"]
    configuration_status: Literal["configured", "unconfigured", "degraded"]
    configuration_errors: list[str]
    last_poll_at: Optional[UTCDateTime]
    last_poll_ok: Optional[bool]
    last_poll_error: Optional[str]
    incident_count: int
    last_backfill_at: Optional[UTCDateTime]
    last_backfill_ok: Optional[bool]
    last_backfill_error: Optional[str]
    backfill_skipped: bool
    backfill_effective_hours: int
    backfill_truncated_reason: Optional[str]
    alerts_reconstructed: int
    sources: list[SourcePollHealth] = Field(default_factory=list)
    watchdog: WatchdogHealth
    notifications: NotificationHealth


class IncidentOccurrenceOut(BaseModel):
    """One ended occurrence (F26 / CAP-11).

    Everything a reader needs is on the record itself: it outlives the Incident,
    the source and the rule it names, so nothing here is joined at read time.
    `incident_id` is a jump hint that may already point at nothing.
    """

    id: int
    incident_id: int
    occurrence_no: int
    source_id: str
    source_name: str
    group_key: str
    title: str
    aggregation_rule_id: Optional[int] = None
    aggregation_rule_name: Optional[str] = None
    started_at: UTCDateTime
    recovered_at: UTCDateTime
    member_count: int
    # The highest severity among members at seal time, not a running peak.
    member_max_severity: str
    handling_conclusion: HandlingStateValue


class IncidentOccurrencePage(BaseModel):
    """A keyset page. The cursor is an id, and that is only safe because the
    table is append-only in recovery order -- see SYSTEM_SPEC §11."""

    items: list[IncidentOccurrenceOut]
    # Pass back as `before_id` to get the next page; null means this is the last.
    next_before_id: Optional[int] = None


# ---------------------------------------------------------------------------
# F27 metric evidence (CAP-12)
# ---------------------------------------------------------------------------


class CurveSeriesOut(BaseModel):
    """One line on a chart. Several appear when a query resolves to several series."""

    labels: dict[str, str] = {}
    # [unix_seconds, value]. Samples that were NaN/Inf/unparseable upstream are
    # absent rather than zeroed -- a fabricated zero cannot be told from a real one.
    points: list[list[float]] = []


class CurveOut(BaseModel):
    """One chart plus everything the reader needs to judge how far to trust it."""

    curve_id: str
    kind: str  # PRIMARY | AUXILIARY
    title: str
    query: str
    # RULES_API (authoritative rule definition) | GENERATOR_URL (reconstructed
    # from the alert's link) | TEMPLATE. Shown, not hidden: it changes how much
    # the curve is worth (CAP-12.1).
    expr_origin: str
    # THRESHOLD | METRIC | EXPRESSION_RESULT, or null for template curves.
    # Never "condition true/false": a plain comparison filters series rather than
    # returning 0/1, so that label would describe a chart nobody is looking at.
    tier: Optional[str] = None
    # COUNTER | GAUGE | UNKNOWN_SUFFIX_GUESS | UNKNOWN. The guess is labelled as
    # a guess rather than silently drawn as a gauge (CAP-12.5).
    metric_type: str
    threshold: Optional[float] = None
    # A threshold line without a direction cannot be read, and the derived facts
    # (when it entered the alerting range, how long it stayed) need it too.
    threshold_operator: Optional[str] = None
    display_unit: str = ""
    window_mode: str = "RECENT"
    queried_at: Optional[UTCDateTime] = None
    window_start: UTCDateTime
    window_end: UTCDateTime
    step_seconds: int
    series: list[CurveSeriesOut] = []


class EvidenceNoteOut(BaseModel):
    """One entry in `warnings` or `failures`.

    Same shape, different meaning: a warning means the curve is there but part of
    it was inferred; a failure means the curve is not there. Merging them would
    pair a working chart with an alarming message about the store being down.
    """

    kind: str
    subject: str = ""
    detail: str = ""


# The name this had when failures were the only outlet.
EvidenceFailureOut = EvidenceNoteOut


class MetricEvidenceOut(BaseModel):
    alert_starts_at: Optional[UTCDateTime] = None
    curves: list[CurveOut] = []
    warnings: list[EvidenceNoteOut] = []
    failures: list[EvidenceNoteOut] = []


class MetricTemplateOriginOut(BaseModel):
    """Where an imported template came from. Absent means hand-written."""

    source_id: str
    dashboard_uid: str
    dashboard_title: str = ""
    panel_id: int = 0
    panel_title: str = ""


class MetricTemplateOut(BaseModel):
    id: int
    name: str
    promql: str
    required_labels: list[str] = []
    description: str = ""
    builtin_key: Optional[str] = None
    user_modified: bool = False
    enabled: bool = False
    # Ordering and display live in a side table: the template model itself is
    # frozen by an applied migration's checksum.
    priority: int = 100
    display_unit: str = ""
    # Also a side table, for the same reason.
    origin: Optional[MetricTemplateOriginOut] = None
    source_scope: SourceScope = Field(default_factory=SourceScope)


class MetricTemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    promql: str = Field(min_length=1, max_length=8192)
    required_labels: list[str] = []
    description: str = Field(default="", max_length=2000)
    priority: Optional[int] = Field(default=None, ge=0, le=10_000)
    display_unit: str = Field(default="", max_length=40)
    source_scope: SourceScope = Field(default_factory=SourceScope)


class MetricTemplateUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    promql: Optional[str] = Field(default=None, min_length=1, max_length=8192)
    required_labels: Optional[list[str]] = None
    description: Optional[str] = Field(default=None, max_length=2000)
    enabled: Optional[bool] = None
    priority: Optional[int] = Field(default=None, ge=0, le=10_000)
    display_unit: Optional[str] = Field(default=None, max_length=40)
    source_scope: Optional[SourceScope] = None
