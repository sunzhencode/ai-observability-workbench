import type { SourceScope } from "./core";

export interface CurveSeries {
  labels: Record<string, string>;
  points: number[][];
}
export interface MetricCurve {
  curve_id: string;
  kind: "PRIMARY" | "AUXILIARY";
  title: string;
  query: string;
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
export interface EvidenceNote { kind: string; subject: string; detail: string }
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
  origin: MetricTemplateOrigin | null;
}
