// Handling-state vocabulary and transitions, kept pure so the list, the detail
// pane and the tests all agree. See PRODUCT_SPEC.md CAP-04/CAP-08.
import type { HandlingState } from "./types";

const LABELS: Record<HandlingState, string> = {
  NEW: "未处理",
  IN_PROGRESS: "处理中",
  CLOSED: "已关闭",
  FALSE_POSITIVE: "误报",
};

const ACTION_LABELS: Record<HandlingState, string> = {
  NEW: "标记为未处理",
  IN_PROGRESS: "开始处理",
  CLOSED: "关闭",
  FALSE_POSITIVE: "标记误报",
};

const TRANSITIONS: Partial<Record<HandlingState, HandlingState[]>> = {
  NEW: ["IN_PROGRESS", "CLOSED", "FALSE_POSITIVE"],
  IN_PROGRESS: ["CLOSED", "FALSE_POSITIVE"],
};

export function handlingLabel(state: HandlingState): string {
  return LABELS[state] ?? state;
}

export function handlingActionLabel(state: HandlingState): string {
  return ACTION_LABELS[state] ?? state;
}

export function handlingActions(state: HandlingState): HandlingState[] {
  return TRANSITIONS[state] ?? [];
}

/**
 * NEW is the resting state of every incident, so pinning a pill to every row
 * would be noise rather than signal. Only progress is worth showing.
 */
export function showsHandlingPill(state: HandlingState): boolean {
  return state !== "NEW";
}

/**
 * Whether a human has already reached a conclusion about this incident.
 *
 * CLOSED and FALSE_POSITIVE are conclusions; NEW and IN_PROGRESS still owe one.
 * The alert list needs this to say how much work hides inside the collapsed
 * recovered fold (CAP-04.9a): upstream recovery never settles handling on its
 * own, so folding those groups away without a number would hide the backlog.
 */
export function isHandlingSettled(state: HandlingState): boolean {
  return state === "CLOSED" || state === "FALSE_POSITIVE";
}

/**
 * The audit's value here is the transition, actor and timestamp — not prose.
 * A default keeps one-click marking possible while the record stays complete;
 * the backend still rejects an empty reason.
 */
export function defaultHandlingReason(state: HandlingState): string {
  return `本地标记为${handlingLabel(state)}`;
}

export function resolveHandlingReason(typed: string, state: HandlingState): string {
  return typed.trim() || defaultHandlingReason(state);
}
