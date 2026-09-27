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
  group_key: string;
  grouping_explanation: string;
  policy_version: number;
  created_at: string;
  members: AlertOut[];
  handling_history: HandlingAudit[];
}

export interface Health {
  status: string;
  /** F21: the registry is the only source of truth, so there are two states. */
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
    summary: {
      total: number;
      healthy: number;
      missing: number;
      unknown: number;
    };
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
  summary: {
    total: number;
    healthy: number;
    missing: number;
    unknown: number;
  };
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

export interface AggregationMatcher {
  label: string;
  operator: MatcherOperator;
  value: string;
}

export interface AggregationRuleDraft {
  name: string;
  priority: number;
  enabled: boolean;
  matchers: AggregationMatcher[];
  group_by_labels: string[];
  source_scope?: SourceScope;
}

export interface AggregationRule extends AggregationRuleDraft {
  id: number;
  version: number;
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

export interface SourceScope {
  mode: "ALL" | "SELECTED";
  source_ids: string[];
}

export interface AggregationPreviewSource {
  source_id: string;
  source_name: string | null;
  matcher_alert_count: number;
  selected_alert_count: number;
  proposed_group_count: number;
}

export type SecretAction = "KEEP" | "REPLACE" | "CLEAR";

export interface SecretUpdate {
  action: SecretAction;
  value?: string;
}

export type EventSourceAuthType = "NONE" | "BEARER" | "BASIC";
export type EventSourceLifecycleState =
  | "ENABLED"
  | "DISABLED"
  | "ARCHIVED";
export type EventSourceStatus =
  | "ENABLED"
  | "DISABLED"
  | "CONNECTION_ERROR"
  | "ARCHIVED";

/** The optional history address. An empty URL means "do not backfill". */
export interface EventSourceThanosDraft {
  url: string;
  auth_type: "NONE" | "BEARER";
  username: string;
  secret: SecretUpdate;
  /** Form-only: what the secret box currently shows. Never sent. */
  secretField?: SecretFieldStateRef;
  timeout_seconds: number;
}

export interface EventSourceThanos {
  url: string;
  auth_type: "NONE" | "BEARER";
  username: string;
  secret_configured: boolean;
  timeout_seconds: number;
  last_test_status: string | null;
  tested_at: string | null;
  safe_error_code: string | null;
}

/**
 * The form's view of a write-only field, kept next to the wire value so the box
 * can say "已配置 · 留空表示不修改" without the API needing to know about boxes.
 */
export interface SecretFieldStateRef {
  configured: boolean;
  input: string;
  cleared: boolean;
}

export interface EventSourceEndpointDraft {
  /**
   * The slot this endpoint occupies, or null for a row the user just added.
   *
   * A position owns the stored credential that `KEEP` resolves against, so it
   * cannot be left to array order: removing an endpoint shifts every later one
   * down a slot and they inherit the previous occupant's secret.
   */
  position: number | null;
  url: string;
  enabled: boolean;
  auth_type: EventSourceAuthType;
  username: string;
  secret: SecretUpdate;
  /** Form-only: what the secret box currently shows. Never sent. */
  secretField?: SecretFieldStateRef;
}

export interface EventSourceCandidate {
  name: string;
  endpoints: EventSourceEndpointDraft[];
  thanos: EventSourceThanosDraft | null;
  poll_interval_seconds: number;
  resolution_grace_seconds: number;
  max_parallel_endpoints: number;
  watchdog_enabled: boolean;
  watchdog_alertname: string;
  watchdog_identity_label: string;
  watchdog_missing_after_seconds: number | null;
}

export interface EventSourceCreate extends EventSourceCandidate {
  enable: boolean;
}

export interface EventSourceEndpointLastTest {
  ok: boolean;
  code: string;
  tested_at: string;
}

export interface EventSourceEndpoint {
  position: number;
  url: string;
  enabled: boolean;
  auth_type: EventSourceAuthType;
  username: string;
  secret_configured: boolean;
  last_test: EventSourceEndpointLastTest | null;
}

/**
 * The optional Grafana address on a source. No auth type and no username:
 * Grafana here is Bearer-or-anonymous, and an anonymous internal instance is an
 * ordinary deployment rather than an unfinished one.
 */
export interface EventSourceGrafana {
  url: string;
  secret_configured: boolean;
  timeout_seconds: number;
  last_test_status: string | null;
  tested_at: string | null;
  safe_error_code: string | null;
}

export interface EventSourceGrafanaDraft {
  url: string;
  secret: SecretUpdate;
  timeout_seconds: number;
}

export interface GrafanaDashboardSummary {
  uid: string;
  title: string;
  folder_title: string;
}

export type ImportCandidateStatus =
  | "READY"
  | "NEEDS_DECISION"
  | "UNSUPPORTED"
  | "UNVERIFIED";

export type ImportDiffKind =
  | "NEW"
  | "UNCHANGED"
  | "UPSTREAM_CHANGED"
  | "CONFLICT"
  | "GONE";

export interface ImportSubstitution {
  macro: string;
  replacement: string;
}

export interface ImportPendingVariable {
  name: string;
  suggestion: "BIND_LABEL" | "PIN_VALUE";
  sample_value: string;
  multi: boolean;
}

export interface ImportCandidate {
  panel_id: number;
  panel_title: string;
  ref_id: string;
  raw_promql: string;
  resolved_promql: string;
  status: ImportCandidateStatus;
  suggested_name: string;
  order: number;
  reason: string;
  substitutions: ImportSubstitution[];
  pending_variables: ImportPendingVariable[];
  display_unit: string;
  probe_value: number | null;
  probe_note: string;
  diff_kind: ImportDiffKind;
  template_id: number | null;
  imported_promql: string;
  current_promql: string;
}

export interface GrafanaImportPreview {
  dashboard_uid: string;
  dashboard_title: string;
  candidates: ImportCandidate[];
  probed: boolean;
  enabled_template_count: number;
  max_auxiliary_curves: number;
}

export interface GrafanaImportItem {
  final_promql: string;
  /** Grafana's text, before variable binding. What re-import diffs against. */
  imported_promql: string;
  name: string;
  origin: {
    dashboard_uid: string;
    dashboard_title: string;
    panel_id: number;
    panel_title: string;
    ref_id: string;
  };
  required_labels: string[];
  enabled: boolean;
  display_unit: string;
  order: number;
  template_id?: number;
}

export interface GrafanaImportResult {
  created_template_ids: number[];
  updated_template_ids: number[];
}

export interface EventSourceConfig {
  poll_interval_seconds: number;
  resolution_grace_seconds: number;
  max_parallel_endpoints: number;
  watchdog_enabled: boolean;
  watchdog_alertname: string;
  watchdog_identity_label: string;
  watchdog_missing_after_seconds: number;
  last_test_status: string | null;
  tested_at: string | null;
  safe_error_code: string | null;
  endpoints: EventSourceEndpoint[];
  thanos: EventSourceThanos | null;
  grafana: EventSourceGrafana | null;
}

export interface EventSource {
  id: string;
  type: "ALERTMANAGER" | string;
  name: string;
  lifecycle_state: EventSourceLifecycleState;
  status: EventSourceStatus | string;
  version: number;
  /** One current configuration. Saving replaces it; there is no second copy. */
  config: EventSourceConfig | null;
  created_at: string;
  updated_at: string;
  archived_at: string | null;
}

export interface EventSourceEndpointTest {
  position: number;
  ok: boolean;
  code: string;
}

export interface EventSourceTestResult {
  ok: boolean;
  code: string;
  endpoints: EventSourceEndpointTest[];
}

export interface EventSourceAudit {
  id: number;
  action: string;
  actor: string;
  result: string;
  changes: Record<string, unknown>;
  created_at: string;
}

export interface WatchdogCluster {
  id: number;
  source_id: string;
  identity_value: string;
  cluster: string;
  inventory_state: "DISCOVERED" | "EXPECTED" | "IGNORED";
  health_state: "HEALTHY" | "MISSING" | "UNKNOWN" | null;
  status: "healthy" | "missing" | "unknown";
  first_discovered_at: string;
  monitoring_started_at: string;
  last_seen_at: string | null;
  ignored_at: string | null;
  ignored_reason: string | null;
  still_emitting: boolean;
  freshness_seconds: number | null;
  version: number;
}

/** A read-only reachability check on something the user just configured. */
export interface ConnectionTestResult {
  ok: boolean;
  code: string;
}

export type MentionMode = "NONE" | "USERS" | "ALL";

export type ChannelKind = "FEISHU_CUSTOM_BOT" | "SMTP" | "GENERIC_WEBHOOK";

export interface FeishuChannelConfigDraft {
  kind: "FEISHU_CUSTOM_BOT";
  webhook: SecretUpdate;
  signing_secret: SecretUpdate;
  required_keyword: string | null;
  mention_mode: MentionMode;
  mention_users: { open_id: string }[];
  mention_on: Record<NotificationEventType, boolean>;
}

export interface SmtpChannelConfigDraft {
  kind: "SMTP";
  host: string;
  port: number;
  tls_mode: "STARTTLS" | "TLS";
  username: string;
  password: SecretUpdate;
  from_addr: string;
  to_addrs: string[];
  subject_prefix: string;
}

export interface GenericWebhookChannelConfigDraft {
  kind: "GENERIC_WEBHOOK";
  url: string;
  /** The whole header map is a secret: a token in a header is the usual auth. */
  headers: SecretUpdate;
  signing_secret: SecretUpdate;
  timeout_seconds: number;
}

export type ChannelConfigDraft =
  | FeishuChannelConfigDraft
  | SmtpChannelConfigDraft
  | GenericWebhookChannelConfigDraft;

export interface NotificationChannelDraft {
  name: string;
  config: ChannelConfigDraft;
}

export interface ChannelConfigSummary {
  kind: string;
  target: string;
  detail: string;
  secret_configured: boolean;
}

export interface NotificationChannelRevision {
  id: number;
  version: number;
  state: "DRAFT" | "ACTIVE" | "RETIRED";
  provider: string;
  config_summary: ChannelConfigSummary;
  webhook_configured: boolean;
  signing_secret_configured: boolean;
  required_keyword: string | null;
  mention_mode: MentionMode;
  mention_users: { open_id: string }[];
  mention_on: Partial<Record<NotificationEventType, boolean>>;
  last_tested_at: string | null;
  last_test_status: string | null;
  last_test_error_code: string | null;
  created_at: string;
  updated_at: string;
  activated_at: string | null;
}

export interface NotificationChannel {
  id: number;
  name: string;
  state: "ENABLED" | "DISABLED";
  active_revision_id: number | null;
  created_at: string;
  updated_at: string;
  disabled_at: string | null;
  revisions: NotificationChannelRevision[];
}

export interface ModelChannelConfigDraft {
  kind: "OPENAI_COMPATIBLE";
  base_url: string;
  model: string;
  api_key: SecretUpdate;
}

export interface ModelChannelDraft {
  name: string;
  config: ModelChannelConfigDraft;
}

export interface ModelChannelRevision {
  id: number;
  state: "DRAFT" | "ACTIVE" | "RETIRED";
  base_url: string;
  base_host: string;
  model: string;
  secret_configured: boolean;
  tested_ok_at: string | null;
  created_at: string;
}

export interface ModelChannel {
  id: number;
  name: string;
  kind: "OPENAI_COMPATIBLE";
  enabled: boolean;
  active_revision_id: number | null;
  created_at: string;
  updated_at: string;
  revisions: ModelChannelRevision[];
}

export interface ModelVendor {
  id: string;
  label: string;
  /** Empty for CUSTOM, where the user supplies it. */
  base_url: string;
  key_hint: string;
  recommended_models: string[];
}

export interface ModelListResult {
  ok: boolean;
  code: string;
  detail: string;
  models: string[];
}

export interface ModelChannelTestResult {
  ok: boolean;
  code: string;
  possibly_billed: boolean;
  /** The safe sub-code (HTTP status, egress reason). Every kind sends the
   *  reader somewhere different, and that only holds if it is displayed. */
  detail: string;
}

export interface NotificationMatcher {
  field: string;
  operator: MatcherOperator;
  value: string;
}

export interface NotificationPolicyDraft {
  name: string;
  priority: number;
  matchers: NotificationMatcher[];
  repeat_interval_seconds: number;
  channel_ids: number[];
  source_scope?: SourceScope;
}

export interface NotificationPolicy extends NotificationPolicyDraft {
  id: number;
  logical_id: string;
  version: number;
  state: "DRAFT" | "ACTIVE" | "RETIRED" | "DISABLED";
  created_at: string;
  updated_at: string;
  activated_at: string | null;
}

export interface NotificationPolicyPreview {
  direct_match_count: number;
  shadowed_count: number;
  final_match_count: number;
  firing_match_count: number;
  unrouted_count: number;
  disabled_channel_count: number;
  samples: {
    incident_id: number;
    context: Record<string, unknown>;
    winner_policy_id: number | null;
    winner_policy_name: string | null;
    channel_ids: number[];
  }[];
  example_card: Record<string, unknown>;
}

export interface PreparedPolicyActivation {
  confirm_token: string;
  eligible_incident_count: number;
  expires_at_epoch: number;
}

export type NotificationEventType =
  | "FIRING_OPENED"
  | "SEVERITY_ESCALATED"
  | "REMINDER"
  | "RECOVERED";

export interface NotificationDelivery {
  id: number;
  event_key: string;
  incident_id: number;
  route_id: number;
  route_target_id: number;
  channel_id: number;
  channel_name: string;
  source_id: string;
  source_name: string | null;
  event_type: NotificationEventType;
  state: string;
  attempt_count: number;
  scheduled_at: string;
  next_attempt_at: string;
  suppression_reason: string | null;
  created_at: string;
  updated_at: string;
  succeeded_at: string | null;
}

export interface NotificationAttempt {
  id: number;
  attempt_no: number;
  trigger: string;
  started_at: string;
  finished_at: string | null;
  outcome: string | null;
  http_status: number | null;
  provider_request_id: string | null;
  error_code: string | null;
  error_summary: string | null;
}

export interface NotificationDeliveryDetail extends NotificationDelivery {
  payload_snapshot: Record<string, unknown>;
  attempts: NotificationAttempt[];
}

export interface IncidentNotification {
  incident_id: number;
  occurrence_no: number;
  route: {
    id: number;
    status: string;
    policy_name: string;
    policy_version: number;
    repeat_interval_seconds: number;
    next_reminder_at: string | null;
    termination_reason: string | null;
  } | null;
  targets: {
    id: number;
    channel_id: number;
    channel_name: string;
    channel_version: number;
    opened_success_at: string | null;
    last_success_at: string | null;
  }[];
  deliveries: NotificationDelivery[];
}

/** One ended occurrence (F26 / CAP-11). Self-sufficient: nothing is joined. */
export interface IncidentOccurrence {
  id: number;
  /** A jump hint that may already point at a deleted Incident. */
  incident_id: number;
  occurrence_no: number;
  source_id: string;
  source_name: string;
  group_key: string;
  title: string;
  aggregation_rule_id: number | null;
  aggregation_rule_name: string | null;
  started_at: string;
  recovered_at: string;
  member_count: number;
  /** Highest severity among members at seal time, not a running peak. */
  member_max_severity: Severity;
  handling_conclusion: HandlingState;
}

export interface IncidentOccurrencePage {
  items: IncidentOccurrence[];
  /** Pass back as `before_id`; null means this was the last page. */
  next_before_id: number | null;
}


/* ------------------------------------------------------------------ F27 */

export interface CurveSeries {
  labels: Record<string, string>;
  /** `[unix_seconds, value]`. Unusable samples are absent, never zeroed. */
  points: number[][];
}

export interface MetricCurve {
  curve_id: string;
  kind: "PRIMARY" | "AUXILIARY";
  title: string;
  query: string;
  /** Shown, not hidden: it changes how far the curve is worth trusting. */
  expr_origin: "RULES_API" | "GENERATOR_URL" | "TEMPLATE";
  tier: "THRESHOLD" | "METRIC" | "EXPRESSION_RESULT" | null;
  metric_type: "COUNTER" | "GAUGE" | "UNKNOWN_SUFFIX_GUESS" | "UNKNOWN";
  threshold: number | null;
  threshold_operator: string | null;
  display_unit: string;
  window_mode: string;
  queried_at: string | null;
  window_start: string;
  window_end: string;
  step_seconds: number;
  series: CurveSeries[];
}

/** A warning means the curve is there but part of it was inferred. */
export interface EvidenceNote {
  kind: string;
  subject: string;
  detail: string;
}

export interface MetricEvidence {
  alert_starts_at: string | null;
  curves: MetricCurve[];
  warnings: EvidenceNote[];
  failures: EvidenceNote[];
}

export interface MetricTemplateOrigin {
  source_id: string;
  dashboard_uid: string;
  dashboard_title: string;
  panel_id: number;
  panel_title: string;
}

export interface MetricTemplate {
  id: number;
  name: string;
  promql: string;
  required_labels: string[];
  description: string;
  builtin_key: string | null;
  user_modified: boolean;
  enabled: boolean;
  priority: number;
  display_unit: string;
  source_scope: SourceScope;
  /** Present only on imported templates; absent means hand-written. */
  origin: MetricTemplateOrigin | null;
}

export interface Recommendation {
  kind: "NEXT_CHECK" | "MITIGATION_CANDIDATE";
  text: string;
}

export interface Hypothesis {
  statement: string;
  verdict: "SUPPORTED" | "SYMPTOM" | "DISPROVEN" | "BLOCKED";
  supporting_fact_ids: string[];
  contradicting_fact_ids: string[];
  missing_evidence: string[];
  recommendations: Recommendation[];
}

export interface InvestigationFact {
  fact_id: string;
  statement: string;
}

export interface Investigation {
  hypotheses: Hypothesis[];
  /** What the claims cite, so a conclusion can be checked rather than believed. */
  facts: InvestigationFact[];
  failure: string | null;
  possibly_billed: boolean;
  model_calls: number;
  model_name: string;
  prompt_version: string;
}
