export type Severity = "critical" | "warning" | "info" | "unknown";
export type SourceState =
  | "firing"
  | "resolved"
  | "pending_resolution"
  | "recovered"
  | "unknown";
export type HandlingState = "NEW" | "IN_PROGRESS" | "CLOSED" | "FALSE_POSITIVE";

export interface IncidentSummary {
  id: number;
  source_id: string;
  source_name: string | null;
  title: string;
  severity: Severity;
  source_state: SourceState;
  freshness_state: "FRESH" | "STALE";
  handling_state: HandlingState;
  handling_version: number;
  member_count: number;
  updated_at: string;
  aggregation_rule_id: number | null;
  aggregation_rule_name: string | null;
  aggregation_status: "matched" | "unmatched" | "missing_labels";
}

export interface AlertOut {
  id: number;
  fingerprint: string;
  source_id: string;
  alertname: string;
  severity: Severity;
  cluster: string;
  labels: Record<string, string>;
  annotations: Record<string, string>;
  starts_at: string | null;
  ends_at: string | null;
  source_state: SourceState;
  missing_since_at: string | null;
  origin: string;
  evidence_completeness: string;
  first_seen_at: string;
  last_seen_at: string;
}

export interface HandlingAudit {
  id: number;
  incident_id: number;
  actor: string;
  from_state: HandlingState;
  to_state: HandlingState;
  reason: string;
  created_at: string;
}

export interface IncidentDetail extends IncidentSummary {
  occurrence_no: number;
  group_key: string;
  grouping_explanation: string;
  policy_version: number;
  created_at: string;
  members: AlertOut[];
  handling_history: HandlingAudit[];
}

export interface Health {
  status: string;
  source_mode: "REGISTRY" | "UNCONFIGURED";
  configuration_status: "configured" | "unconfigured" | "degraded";
  configuration_errors: string[];
  last_poll_at: string | null;
  last_poll_ok: boolean | null;
  last_poll_error: string | null;
  incident_count: number;
  last_backfill_at: string | null;
  last_backfill_ok: boolean | null;
  last_backfill_error: string | null;
  backfill_skipped: boolean;
  backfill_effective_hours: number;
  backfill_truncated_reason: string | null;
  alerts_reconstructed: number;
  sources: SourcePollHealth[];
  watchdog: {
    overall_status: "healthy" | "missing" | "unknown" | "no_data";
    summary: { total: number; healthy: number; missing: number; unknown: number };
    clusters: {
      cluster: string;
      status: "healthy" | "missing" | "unknown";
      last_seen_at: string | null;
      freshness_seconds: number | null;
    }[];
    monitored_source_count: number;
    unmonitored_source_count: number;
    sources: WatchdogSourceHealth[];
  };
  notifications: {
    status: "disabled" | "healthy" | "degraded";
    worker_last_run_at: string | null;
    pending: number;
    retrying: number;
    permanent_failed_24h: number;
    oldest_pending_seconds: number | null;
    active_channels: number;
    active_policies: number;
    last_error_code: string | null;
  };
  readiness: {
    status: "ready" | "not_ready";
    checked_at: string;
    checks: { name: string; status: "ready" | "not_ready"; code: string }[];
  };
  jobs: {
    scheduler_running: boolean;
    scheduler_heartbeat_at: string | null;
    runner_running: boolean;
    runner_heartbeat_at: string | null;
    pending: number;
    running: number;
    expired_leases: number;
    oldest_pending_seconds: number | null;
  };
}

export interface SourcePollHealth {
  source_id: string;
  source_name: string;
  lifecycle_state: string;
  health: "HEALTHY" | "DEGRADED" | "UNAVAILABLE" | "DISABLED" | "ARCHIVED";
  endpoint_total: number;
  endpoint_succeeded: number;
  endpoint_failed: number;
  last_poll_at: string | null;
  last_complete_success_at: string | null;
  last_any_success_at: string | null;
  safe_error_codes: string[];
}

export interface WatchdogSourceClusterHealth {
  id: number | null;
  source_id: string;
  identity_value: string;
  cluster: string;
  inventory_state: "DISCOVERED" | "EXPECTED" | "IGNORED" | null;
  health_state: "HEALTHY" | "MISSING" | "UNKNOWN" | null;
  status: "healthy" | "missing" | "unknown";
  first_discovered_at: string | null;
  monitoring_started_at: string | null;
  last_seen_at: string | null;
  ignored_at: string | null;
  ignored_reason: string | null;
  still_emitting: boolean;
  freshness_seconds: number | null;
  version: number | null;
}

export interface WatchdogSourceHealth {
  source_id: string;
  source_name: string;
  source_health: SourcePollHealth["health"];
  monitor_state: "ENABLED" | "DISABLED" | "SOURCE_DISABLED" | "SOURCE_ARCHIVED";
  summary: { total: number; healthy: number; missing: number; unknown: number };
  clusters: WatchdogSourceClusterHealth[];
}

export interface DiscoveredLabel {
  name: string;
  coverage: number;
  present_count: number;
  total_count: number;
  distinct_count: number;
  sample_values: string[];
  sources: ("current" | "history")[];
}

export interface AggregationLabelCatalog {
  lookback_hours: 24 | 168 | 720;
  history_status: "ok" | "unconfigured" | "error";
  history_error: string | null;
  history_series_count: number;
  labels: DiscoveredLabel[];
}

export type MatcherOperator = "=" | "!=" | "=~" | "!~";
export interface AggregationMatcher { label: string; operator: MatcherOperator; value: string }
export interface AggregationRuleDraft {
  name: string;
  priority: number;
  enabled: boolean;
  matchers: AggregationMatcher[];
  group_by_labels: string[];
  grouping_window_seconds?: 0 | 30 | 60 | 120 | 300;
  source_scope?: SourceScope;
}
export interface AggregationRule extends AggregationRuleDraft {
  id: number;
  version: number;
  grouping_window_seconds: 0 | 30 | 60 | 120 | 300;
  created_at: string;
  updated_at: string;
}
export interface AggregationPreviewGroup {
  group_key: string;
  title: string;
  fingerprints: string[];
  missing_labels: string[];
}
export interface AggregationRulePreview {
  matcher_alert_count: number;
  selected_alert_count: number;
  proposed_group_count: number;
  groups: AggregationPreviewGroup[];
  by_source: AggregationPreviewSource[];
}
export interface SourceScope { mode: "ALL" | "SELECTED"; source_ids: string[] }
export interface AggregationPreviewSource {
  source_id: string;
  source_name: string | null;
  matcher_alert_count: number;
  selected_alert_count: number;
  proposed_group_count: number;
}
export type SecretAction = "KEEP" | "REPLACE" | "CLEAR";
export interface SecretUpdate { action: SecretAction; value?: string }
