import { describe, expect, it } from "vitest";
import {
  APP_VIEWS,
  DEFAULT_NOTIFICATION_TAB,
  DEFAULT_VIEW,
  NOTIFICATION_TABS,
  notificationsPath,
  parseHistorySelection,
  parseNotificationTab,
  parseAlertsSelection,
  pathForView,
  resolveView,
  settingsArchivedFromSearch,
  settingsPathForSource,
  settingsSourceFromSearch,
  viewFromPathname,
  withAlertsSelection,
  withHistorySelection,
  RULE_TABS,
  SETTINGS_TABS,
  parseRuleTab,
  parseSettingsTab,
  rulesPath,
  settingsPath,
} from "./appUrl";

describe("view ⇄ path", () => {
  it("round-trips every view", () => {
    for (const view of APP_VIEWS) {
      expect(viewFromPathname(pathForView(view))).toBe(view);
    }
  });

  it("gives each view a distinct path", () => {
    const paths = APP_VIEWS.map(pathForView);
    expect(new Set(paths).size).toBe(APP_VIEWS.length);
  });

  it("ignores a trailing slash", () => {
    expect(viewFromPathname("/settings/")).toBe("settings");
  });

  it("reads only the first segment, so a future sub-route still resolves", () => {
    expect(viewFromPathname("/notifications/policies")).toBe("notifications");
  });

  it("is case-insensitive", () => {
    expect(viewFromPathname("/Alerts")).toBe("alerts");
  });

  it("returns null for the root and for unknown paths", () => {
    expect(viewFromPathname("/")).toBeNull();
    expect(viewFromPathname("")).toBeNull();
    expect(viewFromPathname("/nope")).toBeNull();
  });
});

describe("resolveView", () => {
  it("falls back rather than throwing, so a bad link still renders", () => {
    expect(resolveView("/nope")).toBe(DEFAULT_VIEW);
    expect(resolveView("/")).toBe(DEFAULT_VIEW);
  });

  it("keeps a readable path", () => {
    expect(resolveView("/rules")).toBe("rules");
  });
});

describe("settings source parameter", () => {
  it("reads the selected source", () => {
    expect(settingsSourceFromSearch("?source=source-a")).toBe("source-a");
  });

  it("treats missing and blank as no selection", () => {
    expect(settingsSourceFromSearch("")).toBeNull();
    expect(settingsSourceFromSearch("?other=1")).toBeNull();
    expect(settingsSourceFromSearch("?source=")).toBeNull();
    expect(settingsSourceFromSearch("?source=%20%20")).toBeNull();
  });

  it("survives a source id that needs encoding", () => {
    const id = "src/a b&c";
    const path = settingsPathForSource(id);
    expect(settingsSourceFromSearch(path.slice(path.indexOf("?")))).toBe(id);
  });

  it("links to the bare settings page when no source is given", () => {
    expect(settingsPathForSource("")).toBe(pathForView("settings"));
    expect(settingsPathForSource("   ")).toBe(pathForView("settings"));
  });
});

describe("settings archived flag", () => {
  it("is off unless the URL asks for it", () => {
    expect(settingsArchivedFromSearch("")).toBe(false);
    expect(settingsArchivedFromSearch("?archived=0")).toBe(false);
    expect(settingsArchivedFromSearch("?archived=nonsense")).toBe(false);
  });

  it("accepts the two spellings a link is likely to carry", () => {
    expect(settingsArchivedFromSearch("?archived=1")).toBe(true);
    expect(settingsArchivedFromSearch("?archived=true")).toBe(true);
  });
});

describe("alerts selection", () => {
  it("reads a rule and an incident", () => {
    expect(parseAlertsSelection("?rule=rule:12&incident=34")).toEqual({
      ruleKey: "rule:12",
      incidentId: 34,
    });
  });

  it("reports an absent selection as null rather than guessing", () => {
    expect(parseAlertsSelection("")).toEqual({ ruleKey: null, incidentId: null });
    expect(parseAlertsSelection("?rule=&incident=")).toEqual({
      ruleKey: null,
      incidentId: null,
    });
  });

  it("rejects an incident id that cannot be one, instead of throwing", () => {
    for (const bad of ["abc", "-1", "0", "1.5", "9e9", " ", "1,2"]) {
      expect(parseAlertsSelection(`?incident=${encodeURIComponent(bad)}`).incidentId).toBeNull();
    }
  });

  it("keeps the unmatched entry, which is a rule key like any other", () => {
    expect(parseAlertsSelection("?rule=unmatched").ruleKey).toBe("unmatched");
  });

  it("round-trips through withAlertsSelection", () => {
    const search = withAlertsSelection("", { ruleKey: "rule:7", incidentId: 3 }).toString();
    expect(parseAlertsSelection(search)).toEqual({ ruleKey: "rule:7", incidentId: 3 });
  });

  it("leaves the source filter alone", () => {
    const next = withAlertsSelection("?source_ids=a&source_ids=b", { incidentId: 9 });
    expect(next.getAll("source_ids")).toEqual(["a", "b"]);
  });

  it("clears a key set to null and leaves an omitted key untouched", () => {
    const start = "?rule=rule:1&incident=5";
    expect(withAlertsSelection(start, { incidentId: null }).toString()).toBe("rule=rule%3A1");
    expect(withAlertsSelection(start, {}).toString()).toBe(
      new URLSearchParams(start).toString(),
    );
  });
});

describe("notifications tab", () => {
  it("round-trips every tab through its path", () => {
    for (const tab of NOTIFICATION_TABS) {
      expect(notificationsPath(tab)).toBe(`/notifications/${tab}`);
      expect(parseNotificationTab(tab)).toBe(tab);
      expect(viewFromPathname(notificationsPath(tab))).toBe("notifications");
    }
  });

  it("falls back to 策略 for a missing or unreadable segment", () => {
    expect(parseNotificationTab(undefined)).toBe(DEFAULT_NOTIFICATION_TAB);
    expect(parseNotificationTab("")).toBe(DEFAULT_NOTIFICATION_TAB);
    expect(parseNotificationTab("nope")).toBe(DEFAULT_NOTIFICATION_TAB);
  });

  it("is case-insensitive, like the view paths", () => {
    expect(parseNotificationTab("Channels")).toBe("channels");
  });
});

describe("history selection", () => {
  it("is tolerant: a stale or hand-edited link still resolves", () => {
    // An unknown conclusion is dropped rather than forwarded — the backend would
    // answer 422 and turn a bad link into an error page.
    expect(parseHistorySelection("?conclusion=BOGUS").conclusion).toBeNull();
    expect(parseHistorySelection("?occurrence=abc").occurrenceId).toBeNull();
    expect(parseHistorySelection("?occurrence=-3").occurrenceId).toBeNull();
    expect(parseHistorySelection("?before_id=0").beforeId).toBeNull();
  });

  it("reads the selection a link carries", () => {
    expect(parseHistorySelection("?conclusion=CLOSED&occurrence=7&before_id=42")).toEqual(
      { conclusion: "CLOSED", occurrenceId: 7, beforeId: 42 },
    );
  });

  it("writes nothing for a default selection", () => {
    expect(withHistorySelection("", {}).toString()).toBe("");
    expect(withHistorySelection("", { conclusion: null }).toString()).toBe("");
  });

  it("drops the cursor and the open record when the filter changes", () => {
    // A page-2 cursor belongs to the old filter's ordering, and the open record
    // may not be in the new result at all.
    const next = withHistorySelection(
      "?conclusion=NEW&before_id=42&occurrence=7",
      { conclusion: "CLOSED" },
    );
    expect(next.get("conclusion")).toBe("CLOSED");
    expect(next.get("before_id")).toBeNull();
    expect(next.get("occurrence")).toBeNull();
  });

  it("drops the open record when paging, since it is a different set of rows", () => {
    const next = withHistorySelection("?occurrence=7", { beforeId: 42 });
    expect(next.get("before_id")).toBe("42");
    expect(next.get("occurrence")).toBeNull();
  });

  it("keeps other params it does not own", () => {
    const next = withHistorySelection("?source_ids=a&source_ids=b", {
      occurrenceId: 5,
    });
    expect(next.getAll("source_ids")).toEqual(["a", "b"]);
    expect(next.get("occurrence")).toBe("5");
  });

  it("puts 历史 in the primary views with its own path", () => {
    expect(APP_VIEWS).toContain("history");
    expect(pathForView("history")).toBe("/history");
    expect(resolveView("/history")).toBe("history");
    // Still falls back rather than 404-ing on an unreadable path.
    expect(resolveView("/historyy")).toBe(DEFAULT_VIEW);
  });
});

/* ------------------------------------------------------------- F27 tabs */

describe("rules tabs", () => {
  it("names the tab in the canonical path", () => {
    // A bare `/rules` in a shared link leaves the reader unable to tell which
    // half the sender meant — the same reason notifications does this.
    expect(pathForView("rules")).toBe("/rules/groups");
  });

  it("still resolves a bare /rules to the rules view", () => {
    expect(resolveView("/rules")).toBe("rules");
    expect(resolveView("/rules/metrics")).toBe("rules");
  });

  it("falls back to the default tab rather than throwing", () => {
    expect(parseRuleTab(undefined)).toBe("groups");
    expect(parseRuleTab("")).toBe("groups");
    expect(parseRuleTab("nonsense")).toBe("groups");
  });

  it("parses each known tab, case-insensitively", () => {
    expect(parseRuleTab("groups")).toBe("groups");
    expect(parseRuleTab("metrics")).toBe("metrics");
    expect(parseRuleTab("METRICS")).toBe("metrics");
  });

  it("builds a path per tab", () => {
    expect(rulesPath("groups")).toBe("/rules/groups");
    expect(rulesPath("metrics")).toBe("/rules/metrics");
  });

  it("round-trips every tab", () => {
    for (const tab of RULE_TABS) {
      expect(parseRuleTab(rulesPath(tab).split("/")[2])).toBe(tab);
    }
  });
});

describe("settings tabs", () => {
  it("falls back to 数据源 for anything unreadable", () => {
    expect(parseSettingsTab(undefined)).toBe("sources");
    expect(parseSettingsTab("")).toBe("sources");
    expect(parseSettingsTab("nonsense")).toBe("sources");
  });

  it("parses each known tab, case-insensitively", () => {
    expect(parseSettingsTab("sources")).toBe("sources");
    expect(parseSettingsTab("model")).toBe("model");
    expect(parseSettingsTab("MODEL")).toBe("model");
  });

  it("round-trips every tab", () => {
    for (const tab of SETTINGS_TABS) {
      expect(parseSettingsTab(settingsPath(tab).split("/")[2])).toBe(tab);
    }
  });

  it("keeps the model service addressable on its own", () => {
    // The point of the change: 模型服务 has a link of its own rather than being
    // a section you have to scroll past the data sources to reach.
    expect(settingsPath("model")).toBe("/settings/model");
    expect(viewFromPathname("/settings/model")).toBe("settings");
  });

  it("still opens a source on the sources tab", () => {
    expect(settingsPathForSource("src-a")).toContain("/settings/sources?source=");
  });
});
