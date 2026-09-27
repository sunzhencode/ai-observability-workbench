// Pure alert-page selection logic, extracted from App.tsx so it can be tested
// without a DOM. F20 phase 10 fixed a default-entry defect here that every
// automated check had passed, so this is the part worth pinning.
import { isHandlingSettled } from "./handling";
import type { AggregationRule, EventSource, IncidentSummary } from "./types";

export const UNMATCHED_RULE_KEY = "unmatched";

const RULE_PREFIX = "rule:";

export interface RuleOption {
  key: string;
  label: string;
  activeCount: number;
  recoveredCount: number;
}

export function ruleKey(ruleId: number): string {
  return `${RULE_PREFIX}${ruleId}`;
}

/**
 * Whether upstream has stopped reporting this group (F25 / CAP-08).
 *
 * The test is the source state and nothing else. Handling deliberately does not
 * take part: CAP-04.8 keeps the two decoupled, so hanging visibility on both
 * would scatter one source state across two lists and leave "what is still on
 * fire?" unanswerable from either. `freshness_state` stays out too — a disabled
 * source keeps its last trusted source state, and a group still `firing` there
 * is unfinished work, not history ("停用不是恢复").
 *
 * Every caller must go through this: an inline `=== "recovered"` anywhere is how
 * the list and the rule counts start disagreeing about the same group.
 */
export function isRecoveredGroup(incident: IncidentSummary): boolean {
  return incident.source_state === "recovered";
}

export interface IncidentPartition {
  active: IncidentSummary[];
  recovered: IncidentSummary[];
  /** Recovered groups that still owe a human conclusion (CAP-04.9a). */
  unsettledRecoveredCount: number;
}

/**
 * Split one rule's groups into what still needs looking at and what is history.
 *
 * Both sides keep the incoming order, which is the backend's severity +
 * updated_at sort — re-sorting here would silently fork the ordering contract.
 */
export function partitionIncidents(
  incidents: IncidentSummary[],
): IncidentPartition {
  const active: IncidentSummary[] = [];
  const recovered: IncidentSummary[] = [];
  let unsettledRecoveredCount = 0;

  for (const incident of incidents) {
    if (!isRecoveredGroup(incident)) {
      active.push(incident);
      continue;
    }
    recovered.push(incident);
    if (!isHandlingSettled(incident.handling_state)) unsettledRecoveredCount += 1;
  }

  return { active, recovered, unsettledRecoveredCount };
}

/** Enabled rules in their given order, with "未命中规则" always last. */
export function buildRuleOptions(
  rules: AggregationRule[],
  incidents: IncidentSummary[],
): RuleOption[] {
  const counts = (members: IncidentSummary[]) => ({
    activeCount: members.filter((item) => !isRecoveredGroup(item)).length,
    recoveredCount: members.filter(isRecoveredGroup).length,
  });

  return [
    ...rules
      .filter((rule) => rule.enabled)
      .map((rule) => ({
        key: ruleKey(rule.id),
        label: rule.name,
        ...counts(
          incidents.filter((item) => item.aggregation_rule_id === rule.id),
        ),
      })),
    {
      key: UNMATCHED_RULE_KEY,
      label: "未命中规则",
      ...counts(
        incidents.filter((item) => item.aggregation_status === "unmatched"),
      ),
    },
  ];
}

/**
 * Keep a still-valid selection, otherwise fall back to the first enabled rule
 * that actually has groups. "未命中规则" is only chosen when no rule has any,
 * because it is a separate entry rather than a default landing place.
 *
 * Active groups win, but a rule holding only recovered ones still beats a rule
 * holding nothing: "everything is currently fine" is the common case, and
 * landing on a blank rule then reads as a broken page rather than good news.
 */
export function resolveSelectedRuleKey(
  current: string | null,
  options: RuleOption[],
): string {
  if (current && options.some((option) => option.key === current)) {
    return current;
  }
  const rules = options.filter((option) => option.key !== UNMATCHED_RULE_KEY);
  const target =
    rules.find((option) => option.activeCount > 0) ??
    rules.find((option) => option.recoveredCount > 0);
  return target?.key ?? options[0]?.key ?? UNMATCHED_RULE_KEY;
}

export function filterVisibleIncidents(
  incidents: IncidentSummary[],
  selectedRuleKey: string | null,
): IncidentSummary[] {
  if (selectedRuleKey === UNMATCHED_RULE_KEY) {
    return incidents.filter((item) => item.aggregation_status === "unmatched");
  }
  if (!selectedRuleKey?.startsWith(RULE_PREFIX)) return [];
  const selectedRuleId = Number(selectedRuleKey.slice(RULE_PREFIX.length));
  return incidents.filter(
    (item) => item.aggregation_rule_id === selectedRuleId,
  );
}

/**
 * Accepts both repeated `?source_ids=a&source_ids=b` and `?source_ids=a,b`.
 *
 * Splitting every repeated value is deliberate: unioning `getAll()` with a
 * comma split of the first value (the shape this replaced) leaked the raw
 * `"a,b"` string in as a third source id that matches nothing.
 */
export function parseSourceIds(search: string): string[] {
  return [
    ...new Set(
      new URLSearchParams(search)
        .getAll("source_ids")
        .flatMap((value) => value.split(","))
        .map((item) => item.trim())
        .filter(Boolean),
    ),
  ];
}

/** Archived sources are history: they only appear when explicitly asked for. */
export function visibleSources(
  sources: EventSource[],
  showArchived: boolean,
): EventSource[] {
  return showArchived
    ? sources
    : sources.filter((source) => source.lifecycle_state !== "ARCHIVED");
}

/**
 * Which source to show after a refresh: keep the current one while it survives,
 * otherwise fall back to the first visible one so the pane is never blank for
 * a reason the user cannot see.
 */
export function resolveSelectedSource(
  current: string | null,
  sources: EventSource[],
): string | null {
  if (current && sources.some((source) => source.id === current)) return current;
  return sources[0]?.id ?? null;
}
