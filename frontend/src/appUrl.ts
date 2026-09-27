/**
 * The URL is the store for "what am I looking at".
 *
 * Before F23 the current view lived in a `useState` and never reached the
 * address bar, so all four pages shared one URL: refreshing went back to 告警,
 * the back button left the app, and no page could be bookmarked or sent to
 * anyone. Everything here is a pure function so that behaviour is testable
 * without a DOM — see `SYSTEM_SPEC.md` §15 on why that matters in this repo.
 *
 * Parsing is deliberately tolerant: an unreadable path resolves to a view
 * rather than throwing, because a bad link should land somewhere with the
 * navigation still on screen.
 */
export type AppView = "alerts" | "rules" | "notifications" | "history" | "settings";

export const APP_VIEWS: readonly AppView[] = [
  "alerts",
  "rules",
  "notifications",
  "history",
  "settings",
];

/** The view a bare "/" resolves to, and the fallback for anything unreadable. */
export const DEFAULT_VIEW: AppView = "alerts";

const VIEW_PATHS: Record<AppView, string> = {
  alerts: "/alerts",
  // Same reasoning as notifications below: the canonical path names the tab, so
  // a copied link says which half of the page the sender was on.
  rules: "/rules/groups",
  // The canonical path names the tab: a link should say which of the three the
  // sender was looking at, and `/notifications` alone leaves the reader (and
  // the back button) unable to tell.
  notifications: "/notifications/policies",
  history: "/history",
  // Same as the two above: the canonical path names the tab. A model service is
  // not a data source — it is a paid outbound target with its own safety gate —
  // so burying it under the data source list made it read as an accessory to
  // one. Two tabs, still five top-level entries.
  settings: "/settings/sources",
};

export function pathForView(view: AppView): string {
  return VIEW_PATHS[view];
}

/**
 * The view a pathname addresses, or `null` when it addresses none.
 *
 * Only the first segment is read, so `/notifications/policies` is already the
 * notifications view — the second segment becomes that page's tab in a later
 * stage, and links written now keep working when it does.
 */
export function viewFromPathname(pathname: string): AppView | null {
  const [segment] = pathname.split("/").filter(Boolean);
  if (segment === undefined) return null;
  const candidate = segment.toLowerCase();
  return APP_VIEWS.find((view) => view === candidate) ?? null;
}

/** Same as `viewFromPathname`, but never null: unknown paths land on 告警. */
export function resolveView(pathname: string): AppView {
  return viewFromPathname(pathname) ?? DEFAULT_VIEW;
}

/**
 * The source the settings page should open, from `?source=`.
 *
 * This replaces the `settingsSourceId` prop that 告警 详情 used to hand to the
 * settings page: a cross-page value that only existed in memory, so the target
 * could not be linked to.
 */
export function settingsSourceFromSearch(search: string): string | null {
  const value = new URLSearchParams(search).get("source");
  const trimmed = value?.trim() ?? "";
  return trimmed === "" ? null : trimmed;
}

/** The link that opens a given source in 系统设置. */
/* --------------------------------------------------------------- settings */

export const SETTINGS_TABS = ["sources", "model"] as const;
export type SettingsTabName = (typeof SETTINGS_TABS)[number];
export const DEFAULT_SETTINGS_TAB: SettingsTabName = "sources";

export function parseSettingsTab(segment: string | undefined): SettingsTabName {
  const candidate = segment?.trim().toLowerCase() ?? "";
  return SETTINGS_TABS.find((tab) => tab === candidate) ?? DEFAULT_SETTINGS_TAB;
}

export function settingsPath(tab: SettingsTabName): string {
  return `/settings/${tab}`;
}

export function settingsPathForSource(sourceId: string): string {
  const trimmed = sourceId.trim();
  return trimmed === ""
    ? settingsPath("sources")
    : `${settingsPath("sources")}?source=${encodeURIComponent(trimmed)}`;
}

/** Whether 系统设置 should also list archived sources, from `?archived=1`. */
export function settingsArchivedFromSearch(search: string): boolean {
  const value = new URLSearchParams(search).get("archived");
  return value === "1" || value === "true";
}

/**
 * What the alerts page is looking at, beyond the source filter.
 *
 * `null` means "the URL does not say" — the page then picks a default to show
 * without writing it back. A default that writes itself into the address bar
 * would rewrite the user's link the moment they opened it, and would fill the
 * back button with selections nobody made.
 */
export interface AlertsSelection {
  ruleKey: string | null;
  incidentId: number | null;
}

export function parseAlertsSelection(search: string): AlertsSelection {
  const params = new URLSearchParams(search);
  const ruleKey = params.get("rule")?.trim() ?? "";
  const rawIncident = params.get("incident")?.trim() ?? "";
  // Only a positive integer can be an incident id; anything else is a bad link,
  // and a bad link should reach the page and be explained there, not throw.
  const incidentId = /^\d+$/.test(rawIncident) ? Number(rawIncident) : NaN;
  return {
    ruleKey: ruleKey === "" ? null : ruleKey,
    incidentId: Number.isSafeInteger(incidentId) && incidentId > 0 ? incidentId : null,
  };
}

/** The same search string with the selection replaced by `next`. */
export function withAlertsSelection(
  search: string,
  next: Partial<AlertsSelection>,
): URLSearchParams {
  const params = new URLSearchParams(search);
  const apply = (key: string, value: string | number | null | undefined) => {
    if (value === undefined) return; // untouched
    if (value === null || value === "") params.delete(key);
    else params.set(key, String(value));
  };
  apply("rule", next.ruleKey);
  apply("incident", next.incidentId);
  return params;
}

/** The notifications tab a `/notifications/:tab` segment names. */
export const NOTIFICATION_TABS = ["policies", "channels", "deliveries"] as const;
export type NotificationTabName = (typeof NOTIFICATION_TABS)[number];
export const DEFAULT_NOTIFICATION_TAB: NotificationTabName = "policies";

export function parseNotificationTab(segment: string | undefined): NotificationTabName {
  const candidate = segment?.trim().toLowerCase() ?? "";
  return NOTIFICATION_TABS.find((tab) => tab === candidate) ?? DEFAULT_NOTIFICATION_TAB;
}

export function notificationsPath(tab: NotificationTabName): string {
  return `/notifications/${tab}`;
}

/**
 * The rules tab a `/rules/:tab` segment names.
 *
 * F27 gave the rules page a second half (metric templates). It is a tab, **not a
 * sixth top-level entry**: adding one of those needs a question the existing
 * pages cannot answer, and "configure some queries" is not that.
 */
export const RULE_TABS = ["groups", "metrics"] as const;
export type RuleTabName = (typeof RULE_TABS)[number];
export const DEFAULT_RULE_TAB: RuleTabName = "groups";

export function parseRuleTab(segment: string | undefined): RuleTabName {
  const candidate = segment?.trim().toLowerCase() ?? "";
  return RULE_TABS.find((tab) => tab === candidate) ?? DEFAULT_RULE_TAB;
}

export function rulesPath(tab: RuleTabName): string {
  return `/rules/${tab}`;
}

/* ---------------------------------------------------------------- history */

export const HISTORY_CONCLUSIONS = [
  "NEW",
  "IN_PROGRESS",
  "CLOSED",
  "FALSE_POSITIVE",
] as const;
export type HistoryConclusion = (typeof HISTORY_CONCLUSIONS)[number];

export interface HistorySelection {
  /** Filter by the human's verdict; null means "any". */
  conclusion: HistoryConclusion | null;
  /** The record open in the detail pane. */
  occurrenceId: number | null;
  /** Keyset cursor: the id of the last row on the previous page. */
  beforeId: number | null;
}

function positiveInt(raw: string | null): number | null {
  if (raw === null) return null;
  const value = Number(raw);
  return Number.isInteger(value) && value > 0 ? value : null;
}

/**
 * Tolerant on purpose: a stale or hand-edited link lands on a readable page
 * rather than throwing. An unknown conclusion is dropped instead of being passed
 * to the backend, which would answer 422 and turn a bad link into an error page.
 */
export function parseHistorySelection(search: string): HistorySelection {
  const params = new URLSearchParams(search);
  const conclusion = params.get("conclusion");
  return {
    conclusion: HISTORY_CONCLUSIONS.includes(conclusion as HistoryConclusion)
      ? (conclusion as HistoryConclusion)
      : null,
    occurrenceId: positiveInt(params.get("occurrence")),
    beforeId: positiveInt(params.get("before_id")),
  };
}

/**
 * Writes only what is set, so a default selection leaves the address bar alone
 * (CAP-08: defaults are derived, not written back).
 *
 * Changing the filter drops the cursor *and* the open record: a page-2 cursor
 * belongs to the old filter's ordering, and the open record may not be in the
 * new result at all.
 */
export function withHistorySelection(
  search: string,
  patch: Partial<HistorySelection>,
): URLSearchParams {
  const params = new URLSearchParams(search);
  const set = (key: string, value: string | number | null) => {
    if (value === null || value === "") params.delete(key);
    else params.set(key, String(value));
  };

  if ("conclusion" in patch) {
    set("conclusion", patch.conclusion ?? null);
    params.delete("before_id");
    params.delete("occurrence");
  }
  if ("beforeId" in patch) {
    set("before_id", patch.beforeId ?? null);
    // A cursor move is a different page of rows; the open record stays only if
    // the caller asks for it in the same patch.
    if (!("occurrenceId" in patch)) params.delete("occurrence");
  }
  if ("occurrenceId" in patch) set("occurrence", patch.occurrenceId ?? null);
  return params;
}
