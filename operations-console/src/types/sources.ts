import type { SecretUpdate } from "./core";

export type EventSourceAuthType = "NONE" | "BEARER" | "BASIC";
export type EventSourceLifecycleState = "ENABLED" | "DISABLED" | "ARCHIVED";
export type EventSourceStatus = "ENABLED" | "DISABLED" | "CONNECTION_ERROR" | "ARCHIVED";
export interface EventSourceThanosDraft {
  url: string;
  auth_type: "NONE" | "BEARER";
  username: string;
  secret: SecretUpdate;
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
export interface SecretFieldStateRef { configured: boolean; input: string; cleared: boolean }
export interface EventSourceEndpointDraft {
  position: number | null;
  url: string;
  enabled: boolean;
  auth_type: EventSourceAuthType;
  username: string;
  secret: SecretUpdate;
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
export interface EventSourceCreate extends EventSourceCandidate { enable: boolean }
export interface EventSourceEndpointLastTest { ok: boolean; code: string; tested_at: string }
export interface EventSourceEndpoint {
  position: number;
  url: string;
  enabled: boolean;
  auth_type: EventSourceAuthType;
  username: string;
  secret_configured: boolean;
  last_test: EventSourceEndpointLastTest | null;
}
export interface EventSourceGrafana {
  url: string;
  secret_configured: boolean;
  timeout_seconds: number;
  last_test_status: string | null;
  tested_at: string | null;
  safe_error_code: string | null;
}
export interface EventSourceGrafanaDraft { url: string; secret: SecretUpdate; timeout_seconds: number }
export interface GrafanaDashboardSummary { uid: string; title: string; folder_title: string }
export type ImportCandidateStatus = "READY" | "NEEDS_DECISION" | "UNSUPPORTED" | "UNVERIFIED";
export type ImportDiffKind = "NEW" | "UNCHANGED" | "UPSTREAM_CHANGED" | "CONFLICT" | "GONE";
export interface ImportSubstitution { macro: string; replacement: string }
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
export interface GrafanaImportResult { created_template_ids: number[]; updated_template_ids: number[] }
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
  config: EventSourceConfig | null;
  created_at: string;
  updated_at: string;
  archived_at: string | null;
}
export interface EventSourceEndpointTest { position: number; ok: boolean; code: string }
export interface EventSourceTestResult { ok: boolean; code: string; endpoints: EventSourceEndpointTest[] }
export interface EventSourceAudit {
  id: number;
  action: string;
  actor: string;
  result: string;
  changes: Record<string, unknown>;
  created_at: string;
}
export interface SourceNoiseControls {
  source_id: string;
  flapping_enabled: boolean;
  storm_enabled: boolean;
  storm_alert_threshold: number;
  storm_occurrence_threshold: number;
  storm_active: boolean;
  storm_started_at: string | null;
  version: number;
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
export interface ConnectionTestResult { ok: boolean; code: string }
