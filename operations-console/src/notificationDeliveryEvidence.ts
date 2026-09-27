export interface DeliveryMetricSummary {
  id: string;
  label: string;
  summary: string;
}

export interface DeliveryEvidenceDescription {
  occurrenceId: number | null;
  status: string;
  safeCode: string | null;
  metrics: DeliveryMetricSummary[];
  links: { label: string; url: string }[];
}

function object(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

export function describeDeliveryEvidence(
  payload: Record<string, unknown>,
): DeliveryEvidenceDescription {
  const rawMetrics = Array.isArray(payload.metric_evidence) ? payload.metric_evidence : [];
  const metrics = rawMetrics.slice(0, 2).flatMap((value) => {
    const item = object(value);
    if (!item) return [];
    const id = String(item.metric_id ?? "");
    if (!id) return [];
    const unit = String(item.unit ?? "");
    return [{
      id,
      label: String(item.display_name ?? id),
      summary: `最新 ${String(item.latest ?? "—")} · 范围 ${String(item.minimum ?? "—")}–${String(item.maximum ?? "—")}${unit ? ` ${unit}` : ""}`,
    }];
  });
  const rawLinks = Array.isArray(payload.deep_links) ? payload.deep_links : [];
  const links = rawLinks.slice(0, 2).flatMap((value) => {
    const item = object(value);
    if (!item || typeof item.url !== "string" || !item.url) return [];
    return [{ label: String(item.label ?? "指标面板"), url: item.url }];
  });
  return {
    occurrenceId: typeof payload.operational_occurrence_id === "number"
      ? payload.operational_occurrence_id
      : null,
    status: String(payload.evidence_status ?? "LEGACY_NOT_RECORDED"),
    safeCode: typeof payload.evidence_safe_code === "string"
      ? payload.evidence_safe_code
      : null,
    metrics,
    links,
  };
}
