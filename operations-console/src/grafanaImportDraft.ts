/**
 * The state of an import drawer: what is ticked, what each variable becomes,
 * and what finally gets submitted.
 *
 * Pure on purpose. `finalPromql` below is **the only producer of the query text
 * that gets stored** — the server takes that literal and does not rebuild it
 * (re-fetching the dashboard at confirmation time would open a window in which
 * it changed, leaving the stored query different from the approved one). A
 * function with that much authority belongs somewhere it can be tested directly
 * rather than through a rendered component.
 */
import type { ImportCandidate, GrafanaImportItem } from "./types";

/** What the user chose for one dashboard variable. */
export type VariableDecision =
  | { kind: "UNDECIDED" }
  | { kind: "BIND_LABEL"; label: string }
  | { kind: "PIN_VALUE"; value: string }
  | { kind: "SKIP" };

export interface CandidateDraft {
  checked: boolean;
  name: string;
  enabled: boolean;
  decisions: Record<string, VariableDecision>;
}

export type ImportDraft = Record<string, CandidateDraft>;

/** Stable within one dashboard, which is all the drawer needs. */
export function candidateKey(candidate: ImportCandidate): string {
  return `${candidate.panel_id}:${candidate.ref_id}`;
}

/**
 * Suggestions stay on the candidate for the UI to show, but the decision starts
 * empty. A preselected recommendation is indistinguishable from consent when a
 * user checks the row without opening its detail.
 */
export function initialDraft(candidates: readonly ImportCandidate[]): ImportDraft {
  const draft: ImportDraft = {};
  for (const candidate of candidates) {
    const decisions: Record<string, VariableDecision> = {};
    for (const variable of candidate.pending_variables) {
      decisions[variable.name] = { kind: "UNDECIDED" };
    }
    draft[candidateKey(candidate)] = {
      checked: false,
      name: candidate.suggested_name,
      // Imported templates arrive switched off, like the shipped ones: an
      // unverified query drawing nothing reads as a broken feature.
      enabled: false,
      decisions,
    };
  }
  return draft;
}

/** A candidate nothing can be done with — the reason is shown instead. */
export function isSelectable(candidate: ImportCandidate): boolean {
  return candidate.status !== "UNSUPPORTED" && candidate.diff_kind !== "GONE";
}

/**
 * Whether every variable has an answer that produces a valid query.
 *
 * A pinned empty value and a bound empty label both leave the query holding
 * something that is not a value, which upstream would either reject or — worse
 * — match nothing, looking exactly like a metric that does not exist here.
 */
export function decisionsComplete(
  candidate: ImportCandidate,
  draft: CandidateDraft,
): boolean {
  return candidate.pending_variables.every((variable) => {
    const decision = draft.decisions[variable.name];
    if (!decision) return false;
    if (decision.kind === "BIND_LABEL") return decision.label.trim() !== "";
    if (decision.kind === "PIN_VALUE") return decision.value.trim() !== "";
    return false; // SKIP means "do not import this one at all".
  });
}

/** True when the user chose to skip any variable — the candidate drops out. */
export function anyVariableSkipped(
  candidate: ImportCandidate,
  draft: CandidateDraft,
): boolean {
  return candidate.pending_variables.some(
    (variable) => draft.decisions[variable.name]?.kind === "SKIP",
  );
}

export function canSubmitCandidate(
  candidate: ImportCandidate,
  draft: CandidateDraft,
): boolean {
  if (!isSelectable(candidate)) return false;
  if (draft.name.trim() === "") return false;
  if (anyVariableSkipped(candidate, draft)) return false;
  return decisionsComplete(candidate, draft);
}

/**
 * Substitute the user's decisions into the query. **The only producer of the
 * text that gets stored.**
 *
 * A bound variable becomes `{{label}}`, which is exactly how the built-in
 * templates are written, so an imported template and a hand-written one are
 * indistinguishable afterwards. A pinned one becomes a literal. Nothing here
 * touches macros — those were resolved at parse time and are already part of
 * `resolved_promql`.
 */
export function finalPromql(
  candidate: ImportCandidate,
  draft: CandidateDraft,
): string {
  let query = candidate.resolved_promql;
  for (const variable of candidate.pending_variables) {
    const decision = draft.decisions[variable.name];
    if (
      !decision ||
      decision.kind === "UNDECIDED" ||
      decision.kind === "SKIP"
    )
      continue;
    const replacement =
      decision.kind === "BIND_LABEL"
        ? `{{${decision.label.trim()}}}`
        : decision.value;
    query = query.replace(variablePattern(variable.name), replacement);
  }
  return query;
}

/**
 * Matches `$name`, `${name}` and `${name:regex}` — the three forms Grafana
 * writes. Anchored on a word boundary so `$cluster` does not also eat the start
 * of `$cluster_role`.
 */
function variablePattern(name: string): RegExp {
  const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(`\\$(?:\\{${escaped}(?::[^}]*)?\\}|${escaped}\\b)`, "g");
}

/** Labels the template will require, derived from the bind decisions. */
export function requiredLabels(
  candidate: ImportCandidate,
  draft: CandidateDraft,
): string[] {
  const labels: string[] = [];
  for (const variable of candidate.pending_variables) {
    const decision = draft.decisions[variable.name];
    if (decision?.kind === "BIND_LABEL") {
      const label = decision.label.trim();
      if (label !== "" && !labels.includes(label)) labels.push(label);
    }
  }
  return labels;
}

export function selectedCount(
  candidates: readonly ImportCandidate[],
  draft: ImportDraft,
): number {
  return candidates.filter((candidate) => {
    const item = draft[candidateKey(candidate)];
    return item?.checked === true && canSubmitCandidate(candidate, item);
  }).length;
}

/** Everything ticked and valid, in the shape the confirm endpoint takes. */
export function buildImportItems(
  dashboardUid: string,
  dashboardTitle: string,
  candidates: readonly ImportCandidate[],
  draft: ImportDraft,
): GrafanaImportItem[] {
  const items: GrafanaImportItem[] = [];
  for (const candidate of candidates) {
    const item = draft[candidateKey(candidate)];
    if (!item || !item.checked || !canSubmitCandidate(candidate, item)) continue;
    items.push({
      final_promql: finalPromql(candidate, item),
      // Grafana's own text, kept apart from the literal above: the next
      // re-import asks "did Grafana change", and comparing against a literal
      // whose `$instance` became `{{instance}}` answers "yes" forever.
      imported_promql: candidate.resolved_promql,
      name: item.name.trim(),
      origin: {
        dashboard_uid: dashboardUid,
        dashboard_title: dashboardTitle,
        panel_id: candidate.panel_id,
        panel_title: candidate.panel_title,
        ref_id: candidate.ref_id,
      },
      required_labels: requiredLabels(candidate, item),
      enabled: item.enabled,
      display_unit: candidate.display_unit,
      order: candidate.order,
      ...(candidate.template_id !== null &&
      (candidate.diff_kind === "UPSTREAM_CHANGED" ||
        candidate.diff_kind === "CONFLICT")
        ? { template_id: candidate.template_id }
        : {}),
    });
  }
  return items;
}

/** The label shown on a candidate's badge, and the class that colours it. */
export function statusLabel(candidate: ImportCandidate): string {
  if (candidate.diff_kind === "GONE") return "已消失";
  if (candidate.diff_kind === "CONFLICT") return "冲突";
  if (candidate.diff_kind === "UPSTREAM_CHANGED") return "上游已变";
  switch (candidate.status) {
    case "READY":
      return "直接可用";
    case "NEEDS_DECISION":
      return "需你决定";
    case "UNVERIFIED":
      return "未验证";
    default:
      return "用不了";
  }
}

/**
 * The live value observed while validating the imported query.
 */
export function probeValueLabel(candidate: ImportCandidate): string {
  if (candidate.probe_value === null) return "无法显示当前值";
  return `当前：${formatProbeValue(candidate.probe_value)}`;
}

function formatProbeValue(value: number): string {
  if (Number.isInteger(value)) return String(value);
  if (Math.abs(value) >= 1000) return value.toFixed(0);
  if (Math.abs(value) >= 1) return value.toFixed(2);
  return value.toPrecision(3);
}
