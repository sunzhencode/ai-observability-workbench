import type {
  HandlingState,
  MatcherOperator,
  SecretUpdate,
  Severity,
  SourceScope,
} from "./core";

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
  headers: SecretUpdate;
  signing_secret: SecretUpdate;
  timeout_seconds: number;
}
export type ChannelConfigDraft = FeishuChannelConfigDraft | SmtpChannelConfigDraft | GenericWebhookChannelConfigDraft;
export interface NotificationChannelDraft { name: string; config: ChannelConfigDraft }
export interface ChannelConfigSummary { kind: string; target: string; detail: string; secret_configured: boolean }
export interface NotificationChannelRevision {
  id: string;
  version: number;
  state: "DRAFT" | "ACTIVE" | "RETIRED";
  provider: string;
  config: Record<string, unknown>;
  secret_configured: Record<string, boolean>;
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
  id: string;
  name: string;
  state: "ENABLED" | "DISABLED";
  active_revision_id: string | null;
  created_at: string;
  updated_at: string;
  disabled_at: string | null;
  revisions: NotificationChannelRevision[];
}
export interface ModelChannelConfigDraft {
  kind: "OPENAI_COMPATIBLE";
  provider_id: ModelProviderId;
  base_url: string;
  model: string;
  api_key: SecretUpdate;
}
export type ModelProviderId = "OPENAI" | "DEEPSEEK" | "MOONSHOT" | "ZHIPU" | "DASHSCOPE" | "CUSTOM";
export interface ModelChannelDraft { name: string; config: ModelChannelConfigDraft }
export interface ModelChannelRevision {
  id: string;
  version: number;
  state: "DRAFT" | "ACTIVE" | "RETIRED";
  base_url: string;
  base_host: string;
  model: string;
  secret_configured: boolean;
  tested_ok_at: string | null;
  last_tested_at: string | null;
  last_test_code: string | null;
  created_at: string;
  provider_profile_id: string | null;
  provider_id: ModelProviderId;
  protocol_profile: "RESPONSES" | "CHAT_COMPLETIONS";
  support_level: "REVIEWED" | "COMPATIBLE" | "BEST_EFFORT";
}
export interface ModelChannel {
  id: string;
  name: string;
  kind: "OPENAI_COMPATIBLE";
  enabled: boolean;
  active_revision_id: string | null;
  created_at: string;
  updated_at: string;
  revisions: ModelChannelRevision[];
}
export interface ModelVendor {
  id: ModelProviderId;
  label: string;
  base_url: string;
  key_hint: string;
  recommended_models: string[];
  protocol_profile: "RESPONSES" | "CHAT_COMPLETIONS";
  support_level: "REVIEWED" | "COMPATIBLE" | "BEST_EFFORT";
}
export interface ModelListResult { ok: boolean; code: string; detail: string; models: string[] }
export interface ModelChannelTestResult { ok: boolean; code: string; possibly_billed: boolean; detail: string }
export interface PromptGuidance {
  organization_context: string;
  investigation_focus: string;
  terminology: string;
  response_style: string;
}
export interface PromptProfile {
  id: string;
  name: string;
  builtin: boolean;
  status: "DRAFT" | "ACTIVE" | "RETIRED";
  revision: number;
  active_revision: number | null;
  guidance: PromptGuidance;
  is_global_default: boolean;
  service_ids: number[];
  tested_at: string | null;
  last_test_code: string | null;
}
export interface PromptProfilePreview {
  profile_id: string;
  revision: number;
  safety_kernel: string[];
  operator_guidance: Record<string, string>;
  egress_categories: string[];
  estimated_tokens: number;
  maximum_cost: "UNKNOWN";
}
export interface PromptProfileRevision {
  revision: number;
  status: "DRAFT" | "ACTIVE" | "RETIRED";
  guidance: PromptGuidance;
  tested_at: string | null;
  last_test_code: string | null;
  created_at: string | null;
  changed_fields: Array<keyof PromptGuidance>;
}
export interface NotificationMatcher { field: string; operator: MatcherOperator; value: string }
export interface NotificationPolicyDraft {
  name: string;
  priority: number;
  matchers: NotificationMatcher[];
  repeat_interval_seconds: number;
  channel_ids: string[];
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
    channel_ids: string[];
  }[];
  example_card: Record<string, unknown>;
}
export interface PreparedPolicyActivation { confirm_token: string; eligible_incident_count: number; expires_at_epoch: number }
export type NotificationEventType = "FIRING_OPENED" | "SEVERITY_ESCALATED" | "REMINDER" | "RECOVERED";
export interface NotificationDelivery {
  id: number;
  event_key: string;
  incident_id: number;
  route_id: number;
  route_target_id: number;
  channel_id: string;
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
    channel_id: string;
    channel_name: string;
    channel_version: number;
    opened_success_at: string | null;
    last_success_at: string | null;
  }[];
  deliveries: NotificationDelivery[];
}
export interface IncidentOccurrence {
  id: number;
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
  member_max_severity: Severity;
  handling_conclusion: HandlingState;
}
export interface IncidentOccurrencePage { items: IncidentOccurrence[]; next_cursor: string | null }

export type QueueView =
  | "ALL"
  | "UNACKNOWLEDGED"
  | "SLA_AT_RISK"
  | "UNMAPPED"
  | "RESOLVED";
export type OperationalSignalState = "FIRING" | "RECOVERED" | "UNKNOWN" | "STALE";
export type OperationalResponseState =
  | "UNACKNOWLEDGED"
  | "IN_PROGRESS"
  | "RESOLVED";
export type ResolutionCode = "FIXED" | "SELF_RECOVERED" | "FALSE_POSITIVE" | "DUPLICATE" | "NO_ACTION";
export type AckSlaState = "NOT_STARTED" | "ON_TRACK" | "AT_RISK" | "BREACHED" | "STOPPED";
export type NoiseState = "NONE" | "GROUPING" | "FLAPPING" | "STORM" | "MAINTENANCE" | "SUPPRESSED";

export interface OperationalOccurrence {
  id: number;
  incident_id: number;
  occurrence_no: number;
  source_id: string;
  source_name: string;
  group_key: string;
  title: string;
  signal_state: OperationalSignalState;
  signal_severity: string;
  response_state: OperationalResponseState;
  resolution_code: ResolutionCode | null;
  duplicate_of_occurrence_id: number | null;
  service_id: number | null;
  service_name: string | null;
  assignment_origin: "UNMAPPED" | "MAPPING" | "MANUAL";
  service_assignment_state: "UNMAPPED" | "MAPPED" | "SERVICE_AMBIGUOUS" | "SERVICE_ARCHIVED";
  member_count: number;
  evidence_completeness: "COMPLETE" | "PARTIAL" | "FAILED" | "UNKNOWN";
  detected_at: string | null;
  source_started_at: string | null;
  ack_sla_seconds: number;
  ack_sla_due_at: string | null;
  acknowledged_at: string | null;
  resolved_at: string | null;
  ack_sla_state: AckSlaState;
  ack_sla_remaining_seconds: number | null;
  noise_state: NoiseState;
  noise_reason: string | null;
  noise_scope: string | null;
  noise_starts_at: string | null;
  noise_ends_at: string | null;
  noise_remaining_seconds: number | null;
  latest_activity_at: string;
  version: number;
}

export interface OperationalOccurrencePage {
  items: OperationalOccurrence[];
  next_cursor: string | null;
  evaluated_at: string;
}

export interface SimilarHistoryMatchReason {
  kind: "SERVICE" | "PRIMARY_ALERTNAME" | "AGGREGATION_RULE" | "GROUP_LABEL";
  field: string;
  value: string;
  points: number;
}

export interface SimilarHistoryItem {
  occurrence_id: number;
  occurrence_no: number;
  title: string;
  score: number;
  match_reasons: SimilarHistoryMatchReason[];
  resolution_code: ResolutionCode;
  operator_conclusion: string | null;
  task_outcome: string | null;
  handling_duration_seconds: number;
  resolved_at: string;
}

export interface Investigation {
  id: string;
  occurrence_id: number;
  incident_id: number;
  status: "PREPARING" | "EVIDENCE_ONLY" | "QUEUED" | "FAILED";
  phase: string;
  member_total: number;
  member_detailed: number;
  query_planned: number;
  query_completed: number;
  successful_metric_facts: number;
  empty_metric_facts: number;
  foundation_successful_metric_facts: number;
  foundation_empty_metric_facts: number;
  degraded_domains: string[];
  degradations: Array<{
    domain: string;
    code: string;
    message: string;
    impact: string;
    preserved: string;
    next_step: string;
  }>;
  findings: string[];
  job_id: string | null;
  model_cost_status: "NOT_INCURRED" | "UNKNOWN";
  snapshot_occurrence_version: number | null;
  planner_rounds: number;
  metric_queries_total: number;
  accounted_tokens: number;
  termination_reason: string | null;
  cancel_requested_at: string | null;
  planner_steps: Array<{
    sequence: number;
    action: string;
    outcome: string;
    metric_name: string | null;
    safe_code: string | null;
    result_metric_names: string[];
  }>;
  alert_evidence: Array<{
    evidence_ref: string;
    alertname: string;
  }>;
  metric_observations: Array<{
    evidence_ref: string;
    alert_ref: string;
    metric_name: string;
    series_count: number;
    point_count: number;
    minimum: number | null;
    maximum: number | null;
    latest: number | null;
    sample: Array<{ timestamp: number; value: string }>;
  }>;
  analyst_result: {
    summary: string;
    hypotheses: Array<{
      title: string;
      explanation: string;
      verdict: "SUPPORTED" | "SYMPTOM" | "DISPROVEN" | "BLOCKED";
      supporting_evidence_ids: string[];
      contradicting_evidence_ids: string[];
      missing_evidence: string[];
    }>;
    missing_evidence: string[];
    recommended_actions: Array<{
      kind: "NEXT_CHECK" | "MANUAL_MITIGATION" | "RUNBOOK";
      description: string;
      risk: string;
      evidence_ids: string[];
    }>;
  } | null;
  analyst_calls: number;
  analyst_prompt_tokens: number;
  analyst_completion_tokens: number;
  prompt_profile_id: string | null;
  prompt_profile_name: string | null;
  prompt_profile_revision: number | null;
  playbook_id: string | null;
  playbook_revision: number | null;
  usage: {
    initiator_kind: "INTERACTIVE_OPERATOR";
    request_id: string;
    source_ip: string;
    planner_calls: number;
    analyst_calls: number;
    metric_queries: number;
    accounted_tokens: number;
    model_execution_mode: "FAKE" | "EXTERNAL" | "UNKNOWN_LEGACY";
    model_channel_id: string | null;
    model_channel_revision: number | null;
    prompt_profile_revision: number | null;
    playbook_revision: number | null;
    egress_categories: string[];
    pricing_revision: number | null;
    cost_status: "NOT_INCURRED" | "UNKNOWN";
    p0_mtti_ms: number | null;
    p1_mtti_ms: number | null;
    p2_mtti_ms: number | null;
    query_yield: number | null;
    evidence_gain: number;
    degradation_count: number;
    canceled: boolean;
    tool_actions: Array<{
      sequence: number;
      action: string;
      outcome: string;
      metric_name: string | null;
      window: string | null;
      aggregation: string | null;
      label_names: string[];
      group_by: string[];
      safe_code: string | null;
    }>;
  };
  feedback: {
    sequence: number;
    rating: "USEFUL" | "NOT_USEFUL" | "ADOPTED";
    created_at: string;
  } | null;
  created_at: string;
  updated_at: string;
}

export interface InvestigatorRun {
  schema_version: "V2";
  id: string;
  occurrence_id: number;
  status: "QUEUED" | "RUNNING" | "COMPLETED" | "DEGRADED" | "FAILED" | "CANCELED";
  job_id: string | null;
  provider_profile_id: string | null;
  model_channel_id: string | null;
  model_revision: number | null;
  alert_evidence: Array<Record<string, unknown>>;
  metric_evidence: Array<{
    evidence_id: string;
    metric_id: string;
    status: "DATA" | "EMPTY_NO_DATA" | "SOURCE_UNAVAILABLE";
    summary: Record<string, number | string | null>;
    sample: Array<{ timestamp: number; value: string }>;
  }>;
  degraded_domains: string[];
  available_metric_count: number;
  activities: Array<{
    sequence: number;
    kind: string;
    status: string;
    safe_code: string;
    evidence_ids: string[];
  }>;
  report: {
    summary_zh: string;
    verdict: "INCIDENT_CONFIRMED" | "LIKELY_INCIDENT" | "INCONCLUSIVE" | "NO_INCIDENT_EVIDENCE";
    confidence: number;
    findings: Array<{ title_zh: string; analysis_zh: string; evidence_ids: string[] }>;
    recommended_actions: Array<{ title_zh: string; rationale_zh: string; evidence_ids: string[] }>;
    missing_evidence_zh: string[];
    degraded_domains: string[];
    evidence_gain: number;
  } | null;
  request_count: number;
  tool_call_count: number;
  input_tokens: number;
  output_tokens: number;
  safe_error_code: string | null;
  feedback: {
    sequence: number;
    rating: "USEFUL" | "NOT_USEFUL" | "ADOPTED";
    created_at: string;
  } | null;
  created_at: string;
  updated_at: string;
}

export interface IncidentTimelineEntry {
  id: number;
  occurrence_id: number;
  sequence: number;
  actor_type: "INTERACTIVE_OPERATOR" | "SYSTEM";
  event_type: "RESPONSE_HANDLING_STARTED" | "RESPONSE_ACKNOWLEDGED" | "ACKNOWLEDGED_IMPLICIT" | "RESPONSE_STATE_CHANGED" | "RESPONSE_PRIORITY_CHANGED" | "OCCURRENCE_RESOLVED" | "LEGACY_HANDLING_IMPORTED" | "TASK_CREATED" | "TASK_STATE_CHANGED" | "MANUAL_NOTE" | "NOTE_REDACTED" | "SERVICE_MAPPING_UPDATED" | "SERVICE_ASSIGNMENT_CHANGED" | "SUPPRESSION_STARTED" | "SUPPRESSION_ENDED";
  summary: string;
  detail: Record<string, unknown>;
  request_id: string;
  source_ip: string;
  created_at: string;
}

export type IncidentTaskState = "TODO" | "IN_PROGRESS" | "DONE" | "CANCELED";

export interface IncidentTask {
  id: number;
  occurrence_id: number;
  title: string;
  description: string | null;
  due_at: string | null;
  runbook_link: string | null;
  status: IncidentTaskState;
  result: string | null;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface CollaborationCommandResult {
  task: IncidentTask | null;
  timeline: IncidentTimelineEntry;
  replayed: boolean;
}

export interface ResponseCommandResult {
  occurrence_id: number;
  previous_state: OperationalResponseState;
  current_state: OperationalResponseState;
  resolution_code: ResolutionCode | null;
  version: number;
  timeline: IncidentTimelineEntry[];
  replayed: boolean;
}

export interface BatchStartHandlingResult {
  items: {
    occurrence_id: number;
    ok: boolean;
    command: ResponseCommandResult | null;
    error_code: string | null;
    message: string | null;
  }[];
  succeeded: number;
  failed: number;
}

export interface OccurrenceNoise {
  state: NoiseState;
  reason: string | null;
  scope: string | null;
  starts_at: string | null;
  ends_at: string | null;
  suppression_id: number | null;
  suppression_version: number | null;
}

export interface Suppression {
  id: number;
  occurrence_id: number;
  starts_at: string;
  ends_at: string;
  reason: string;
  status: "ACTIVE" | "ENDED";
  ended_at: string | null;
  version: number;
  created_at: string;
}

export interface MaintenanceWindow {
  id: number;
  scope_kind: "SOURCE" | "SERVICE" | "RULE";
  source_id: string | null;
  service_id: number | null;
  aggregation_rule_id: number | null;
  starts_at: string;
  ends_at: string;
  reason: string;
  status: "ACTIVE" | "ENDED";
  ended_at: string | null;
  version: number;
  created_at: string;
}

export interface MaintenanceWindowDraft {
  scope_kind: "SOURCE" | "SERVICE" | "RULE";
  source_id: string | null;
  service_id: number | null;
  aggregation_rule_id: number | null;
  starts_at: string;
  ends_at: string;
  reason: string;
}
