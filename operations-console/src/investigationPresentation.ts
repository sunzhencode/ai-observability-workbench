import type { Investigation, InvestigatorRun } from "./types";

export const INVESTIGATION_STAGE_TITLES = {
  foundation: "基础证据",
  expansion: "AI 扩展调查",
  conclusion: "AI 调查结论",
} as const;

export function investigatorVerdictLabel(
  verdict: NonNullable<InvestigatorRun["report"]>["verdict"],
): string {
  return {
    INCIDENT_CONFIRMED: "旧版模型判断：事件证据充分",
    LIKELY_INCIDENT: "模型判断：可能是事件",
    INCONCLUSIVE: "模型判断：证据不足",
    NO_INCIDENT_EVIDENCE: "模型判断：现有证据不支持事件",
  }[verdict];
}

export function investigatorConfidenceAuditLabel(confidence: number): string {
  return `模型自评分（未校准）${Math.round(confidence * 100)}%`;
}

export function investigatorEvidenceReferenceLabel(evidenceIds: string[]): string {
  return evidenceIds.length > 0 ? "有证据引用" : "无证据引用";
}

const INVESTIGATOR_L2_DISPLAY_LIMIT = 20;

type InvestigatorMetricEvidence = InvestigatorRun["metric_evidence"][number];

export function investigatorMetricEvidencePresentation(metric: InvestigatorMetricEvidence) {
  const pointCount = metric.summary.point_count;
  return {
    pointCount: typeof pointCount === "number" && Number.isFinite(pointCount)
      ? `${pointCount} 个点`
      : "未记录",
    sample: metric.sample.slice(0, INVESTIGATOR_L2_DISPLAY_LIMIT),
  };
}

export function analystVerdictLabel(
  verdict: NonNullable<Investigation["analyst_result"]>["hypotheses"][number]["verdict"],
): string {
  return {
    SUPPORTED: "现有证据支持",
    SYMPTOM: "更像是伴随症状",
    DISPROVEN: "现有证据不支持",
    BLOCKED: "证据仍不足",
  }[verdict];
}

export function analystActionLabel(
  kind: NonNullable<Investigation["analyst_result"]>["recommended_actions"][number]["kind"],
): string {
  return {
    NEXT_CHECK: "继续核对",
    MANUAL_MITIGATION: "人工缓解建议",
    RUNBOOK: "操作手册建议",
  }[kind];
}

export function analystRecommendationLabel(
  index: number,
  kind: NonNullable<Investigation["analyst_result"]>["recommended_actions"][number]["kind"],
): string {
  return `${index === 0 ? "优先建议" : "后续建议"} · ${analystActionLabel(kind)}`;
}

export function analystEvidenceReferenceLabel(
  evidenceRef: string,
  alertEvidence: Investigation["alert_evidence"],
  metricEvidence: Array<Pick<Investigation["metric_observations"][number], "evidence_ref" | "metric_name">>,
): string {
  const alert = alertEvidence.find((item) => item.evidence_ref === evidenceRef);
  if (alert) return `告警事实：${alert.alertname}`;
  const metric = metricEvidence.find((item) => item.evidence_ref === evidenceRef);
  if (metric) return `指标证据：${metric.metric_name}`;
  return "窗口内无数据证据";
}

export function analystEvidenceGainMessage(evidenceGain: number): string | null {
  return evidenceGain === 0
    ? "本轮 AI 扩展未取得基础证据之外的新事实；结论仅基于已冻结的告警和基础证据。"
    : null;
}

export function uniqueGlobalMissingEvidence(
  globalMissing: string[],
  hypothesisMissing: string[][],
): string[] {
  const shownInHypotheses = new Set(hypothesisMissing.flat().map((item) => item.trim()));
  return [...new Set(globalMissing.map((item) => item.trim()).filter(Boolean))]
    .filter((item) => !shownInHypotheses.has(item));
}

const LEGACY_FINDING_LABELS: Record<string, string> = {
  "P1 证据扩展已记录；当前批次尚未生成模型判断":
    "AI 扩展调查已记录；当前尚未生成调查结论",
  "P0 证据简报已保留；后续证据扩展未启动":
    "基础证据已保留；AI 扩展调查未启动",
};

export function investigationFindingText(finding: string): string {
  return LEGACY_FINDING_LABELS[finding] ?? finding;
}

type MetricObservation = Investigation["metric_observations"][number];

function formatMetricValue(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return "未取得";
  return new Intl.NumberFormat("zh-CN", {
    maximumFractionDigits: 4,
    useGrouping: false,
  }).format(value);
}

export function metricObservationPresentation(observation: MetricObservation) {
  const first = Number(observation.sample[0]?.value);
  const last = Number(observation.sample.at(-1)?.value);
  const relativeChange = Math.abs(last - first) / Math.max(Math.abs(first), Math.abs(last), Number.EPSILON);
  const trend = !Number.isFinite(first) || !Number.isFinite(last)
    ? "采样趋势不可计算"
    : relativeChange <= 0.05
      ? "窗口内整体稳定"
      : last > first
      ? "窗口内整体上升"
      : last < first
        ? "窗口内整体下降"
        : "窗口内整体稳定";
  return {
    latest: formatMetricValue(observation.latest),
    range: `${formatMetricValue(observation.minimum)} – ${formatMetricValue(observation.maximum)}`,
    coverage: `${observation.point_count} 个点 · ${observation.series_count} 条序列`,
    trend,
  };
}

type InvestigationContinuation = {
  status: Investigation["status"];
  degradations: Investigation["degradations"];
  plannerStepCount: number;
  terminationReason?: string | null;
};

export function investigationContinuationMessage({
  status,
  plannerStepCount,
  degradations,
  terminationReason,
}: InvestigationContinuation): string | null {
  if (plannerStepCount > 0 || status === "PREPARING" || status === "QUEUED") {
    return null;
  }
  const reason = terminationReason ?? "";
  const metricUnavailable = degradations.some(
    (item) => item.domain === "metrics" && item.code === "SOURCE_UNAVAILABLE",
  ) || reason === "METRIC_SOURCE_UNAVAILABLE";
  if (metricUnavailable) {
    return "本次已完成基础证据，但指标源未配置或当前不可用，因此没有开始 AI 扩展调查。请在系统设置中核对该来源的指标连接后重新调查。";
  }
  const modelUnavailable = degradations.some((item) => item.domain === "model")
    || reason.startsWith("MODEL_");
  if (modelUnavailable) {
    return "本次已完成基础证据，但当前没有可用的模型服务，因此没有开始 AI 扩展调查。请在系统设置中核对模型服务后重新调查。";
  }
  return "本次已完成基础证据，当前没有可继续的 AI 扩展调查条件。已有证据会保留；核对系统设置后可重新调查。";
}
