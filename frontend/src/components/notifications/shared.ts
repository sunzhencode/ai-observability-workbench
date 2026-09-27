/**
 * Vocabulary and draft shapes shared by the three notification panels.
 *
 * `latestPolicies` is load-bearing: a policy is versioned, and only the highest
 * version of each `logical_id` is the current one. Showing every row would show
 * superseded routing as if it were live.
 */
import type {
  MatcherOperator,
  NotificationChannel,
  NotificationEventType,
  NotificationPolicy,
  NotificationPolicyDraft,
} from "../../types";

export type NotificationTab = "policies" | "channels" | "deliveries";

export const EVENTS: { value: NotificationEventType; label: string }[] = [
  { value: "FIRING_OPENED", label: "首次" },
  { value: "SEVERITY_ESCALATED", label: "升级" },
  { value: "REMINDER", label: "重复提醒" },
  { value: "RECOVERED", label: "恢复" },
];
export const OPERATORS: MatcherOperator[] = ["=", "!=", "=~", "!~"];
export const STABLE_FIELDS = ["aggregation_rule_id", "severity", "group.cluster", "group.namespace", "group.service"];

export function policyStateLabel(state: NotificationPolicy["state"]) {
  if (state === "ACTIVE") return "已启用";
  if (state === "DRAFT") return "未应用更改";
  if (state === "DISABLED") return "已停用";
  return "已替换";
}

export function revisionStateLabel(state: "DRAFT" | "ACTIVE" | "RETIRED") {
  if (state === "ACTIVE") return "已启用";
  if (state === "DRAFT") return "未应用更改";
  return "已替换";
}

export function channelStateLabel(state: NotificationChannel["state"]) {
  return state === "ENABLED" ? "已启用" : "已停用";
}

export function latestPolicies(items: NotificationPolicy[]) {
  const byLogical = new Map<string, NotificationPolicy>();
  items.forEach((item) => {
    const current = byLogical.get(item.logical_id);
    if (!current || item.version > current.version) byLogical.set(item.logical_id, item);
  });
  return [...byLogical.values()].sort((a, b) => a.priority - b.priority || a.id - b.id);
}

export function emptyPolicy(channelId?: number): NotificationPolicyDraft {
  return {
    name: "",
    priority: 100,
    matchers: [],
    repeat_interval_seconds: 14_400,
    channel_ids: channelId ? [channelId] : [],
    source_scope: { mode: "ALL", source_ids: [] },
  };
}

export function draftFromPolicy(item: NotificationPolicy): NotificationPolicyDraft {
  return {
    name: item.name,
    priority: item.priority,
    matchers: item.matchers.map((matcher) => ({ ...matcher })),
    repeat_interval_seconds: item.repeat_interval_seconds,
    channel_ids: [...item.channel_ids],
    source_scope: item.source_scope ?? { mode: "ALL", source_ids: [] },
  };
}

export function latestRevision(channel: NotificationChannel) {
  return [...channel.revisions].sort((a, b) => b.version - a.version)[0] ?? null;
}
