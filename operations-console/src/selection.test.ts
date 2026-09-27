import { describe, expect, it } from "vitest";
import type { AggregationRule, EventSource, IncidentSummary } from "./types";
import {
  UNMATCHED_RULE_KEY,
  resolveSelectedSource,
  visibleSources,
  buildRuleOptions,
  filterVisibleIncidents,
  isRecoveredGroup,
  parseSourceIds,
  partitionIncidents,
  resolveSelectedRuleKey,
  ruleKey,
} from "./selection";

function rule(id: number, name: string, enabled = true): AggregationRule {
  return {
    id,
    name,
    priority: 100,
    enabled,
    matchers: [],
    group_by_labels: [],
    grouping_window_seconds: 30,
    source_scope: { mode: "ALL", source_ids: [] },
    version: 1,
    created_at: "2026-07-26T00:00:00Z",
    updated_at: "2026-07-26T00:00:00Z",
  };
}

function incident(
  id: number,
  ruleId: number | null,
  status: IncidentSummary["aggregation_status"],
  patch: Partial<IncidentSummary> = {},
): IncidentSummary {
  return {
    id,
    aggregation_rule_id: ruleId,
    aggregation_status: status,
    ...patch,
  } as IncidentSummary;
}

/** A recovered group, optionally already concluded by a human. */
function recovered(
  id: number,
  ruleId: number | null,
  handling: IncidentSummary["handling_state"] = "NEW",
): IncidentSummary {
  return incident(id, ruleId, ruleId === null ? "unmatched" : "matched", {
    source_state: "recovered",
    handling_state: handling,
  });
}

describe("buildRuleOptions", () => {
  it("counts incidents per enabled rule and appends the unmatched entry last", () => {
    const options = buildRuleOptions(
      [rule(1, "crashloop"), rule(2, "disk")],
      [
        incident(10, 1, "matched"),
        incident(11, 1, "matched"),
        incident(12, 2, "matched"),
        incident(13, null, "unmatched"),
      ],
    );

    expect(options.map((option) => [option.key, option.activeCount])).toEqual([
      [ruleKey(1), 2],
      [ruleKey(2), 1],
      [UNMATCHED_RULE_KEY, 1],
    ]);
    expect(options.at(-1)?.key).toBe(UNMATCHED_RULE_KEY);
  });

  it("omits disabled rules", () => {
    const options = buildRuleOptions(
      [rule(1, "on"), rule(2, "off", false)],
      [],
    );
    expect(options.map((option) => option.key)).toEqual([
      ruleKey(1),
      UNMATCHED_RULE_KEY,
    ]);
  });

  // The dropdown says "how many things still need looking at", the same
  // question the header total and the severity summary answer. Two counting
  // rules on one screen is the worst outcome: the dropdown would say 2 groups
  // while the list says 0 and nothing on screen explains the gap.
  it("counts active and recovered groups separately, per rule", () => {
    const options = buildRuleOptions(
      [rule(1, "crashloop"), rule(2, "disk")],
      [
        incident(10, 1, "matched"),
        recovered(11, 1),
        recovered(12, 2),
        recovered(13, null),
      ],
    );

    expect(
      options.map((option) => [option.key, option.activeCount, option.recoveredCount]),
    ).toEqual([
      [ruleKey(1), 1, 1],
      [ruleKey(2), 0, 1],
      [UNMATCHED_RULE_KEY, 0, 1],
    ]);
  });
});

describe("isRecoveredGroup", () => {
  it("looks at the source state and nothing else", () => {
    expect(isRecoveredGroup(recovered(10, 1, "CLOSED"))).toBe(true);
    expect(isRecoveredGroup(recovered(11, 1, "NEW"))).toBe(true);
    for (const state of ["firing", "pending_resolution", "unknown"] as const) {
      expect(
        isRecoveredGroup(incident(12, 1, "matched", { source_state: state })),
      ).toBe(false);
    }
  });
});

describe("partitionIncidents", () => {
  it("keeps firing, pending and unknown groups in the active list", () => {
    const groups = [
      incident(10, 1, "matched", { source_state: "firing" }),
      incident(11, 1, "matched", { source_state: "pending_resolution" }),
      incident(12, 1, "matched", { source_state: "unknown" }),
      recovered(13, 1),
    ];

    const { active, recovered: folded } = partitionIncidents(groups);
    expect(active.map((item) => item.id)).toEqual([10, 11, 12]);
    expect(folded.map((item) => item.id)).toEqual([13]);
  });

  // 停用不是恢复: a disabled source keeps its last trusted source state, so a
  // group still firing there is unfinished work rather than history.
  it("does not fold a STALE group that is still firing", () => {
    const stale = incident(10, 1, "matched", {
      source_state: "firing",
      freshness_state: "STALE",
    });
    expect(partitionIncidents([stale]).active.map((item) => item.id)).toEqual([10]);
  });

  it("counts recovered groups that still owe a human conclusion", () => {
    const { unsettledRecoveredCount } = partitionIncidents([
      recovered(10, 1, "NEW"),
      recovered(11, 1, "IN_PROGRESS"),
      recovered(12, 1, "CLOSED"),
      recovered(13, 1, "FALSE_POSITIVE"),
    ]);
    // Recovery never settles handling on its own (CAP-04.9a), so the fold has
    // to say how much work is hiding inside it.
    expect(unsettledRecoveredCount).toBe(2);
  });

  it("preserves the backend's order on both sides", () => {
    const groups = [
      recovered(10, 1),
      incident(11, 1, "matched", { source_state: "firing" }),
      recovered(12, 1),
      incident(13, 1, "matched", { source_state: "firing" }),
    ];

    const { active, recovered: folded } = partitionIncidents(groups);
    expect(active.map((item) => item.id)).toEqual([11, 13]);
    expect(folded.map((item) => item.id)).toEqual([10, 12]);
  });
});

describe("resolveSelectedRuleKey", () => {
  it("selects the first rule that actually has groups, not merely the first rule", () => {
    const options = buildRuleOptions(
      [rule(1, "empty"), rule(2, "has-groups")],
      [incident(10, 2, "matched")],
    );
    expect(resolveSelectedRuleKey(null, options)).toBe(ruleKey(2));
  });

  it("keeps a still-valid current selection even when it has no groups", () => {
    const options = buildRuleOptions(
      [rule(1, "empty"), rule(2, "has-groups")],
      [incident(10, 2, "matched")],
    );
    expect(resolveSelectedRuleKey(ruleKey(1), options)).toBe(ruleKey(1));
  });

  it("drops a selection whose rule disappeared", () => {
    const options = buildRuleOptions([rule(2, "kept")], [incident(10, 2, "matched")]);
    expect(resolveSelectedRuleKey(ruleKey(99), options)).toBe(ruleKey(2));
  });

  it("does not land on 未命中规则 just because it has the only groups", () => {
    const options = buildRuleOptions(
      [rule(1, "empty")],
      [incident(10, null, "unmatched")],
    );
    // Unmatched is a separate entry, so the empty rule stays the default.
    expect(resolveSelectedRuleKey(null, options)).toBe(ruleKey(1));
  });

  it("falls back to unmatched when there are no enabled rules at all", () => {
    expect(resolveSelectedRuleKey(null, buildRuleOptions([], []))).toBe(
      UNMATCHED_RULE_KEY,
    );
  });

  it("prefers a rule with active groups over one holding only recovered ones", () => {
    const options = buildRuleOptions(
      [rule(1, "quiet"), rule(2, "burning")],
      [recovered(10, 1), incident(11, 2, "matched", { source_state: "firing" })],
    );
    expect(resolveSelectedRuleKey(null, options)).toBe(ruleKey(2));
  });

  // "Everything is currently fine" is the common case. Landing on a rule with
  // nothing at all then reads as a broken page, so a rule that at least has a
  // recovered fold to show beats an empty one.
  it("falls back to a rule with only recovered groups rather than a blank one", () => {
    const options = buildRuleOptions(
      [rule(1, "empty"), rule(2, "has-history")],
      [recovered(10, 2)],
    );
    expect(resolveSelectedRuleKey(null, options)).toBe(ruleKey(2));
  });

  it("still refuses to land on 未命中规则, even via the recovered fallback", () => {
    const options = buildRuleOptions([rule(1, "empty")], [recovered(10, null)]);
    expect(resolveSelectedRuleKey(null, options)).toBe(ruleKey(1));
  });
});

// The partition is additive: incidents built without a source_state — every
// pre-F25 fixture in this file — all land in the active list, so none of the
// selection behaviour above changes shape.
describe("the layering is additive", () => {
  it("treats a group with no source state as active", () => {
    const groups = [incident(10, 1, "matched")];
    expect(partitionIncidents(groups).active).toHaveLength(1);
    expect(partitionIncidents(groups).recovered).toHaveLength(0);
  });
});

describe("filterVisibleIncidents", () => {
  const incidents = [
    incident(10, 1, "matched"),
    incident(11, 2, "matched"),
    incident(12, null, "unmatched"),
    incident(13, null, "missing_labels"),
  ];

  it("returns only the selected rule's incidents", () => {
    expect(filterVisibleIncidents(incidents, ruleKey(1)).map((i) => i.id)).toEqual([10]);
  });

  it("returns only unmatched incidents for the unmatched entry", () => {
    expect(
      filterVisibleIncidents(incidents, UNMATCHED_RULE_KEY).map((i) => i.id),
    ).toEqual([12]);
  });

  it("returns nothing before a selection exists", () => {
    expect(filterVisibleIncidents(incidents, null)).toEqual([]);
  });
});

describe("parseSourceIds", () => {
  it("accepts repeated params, comma lists, and both together without duplicates", () => {
    expect(parseSourceIds("?source_ids=a&source_ids=b")).toEqual(["a", "b"]);
    expect(parseSourceIds("?source_ids=a,b")).toEqual(["a", "b"]);
    expect(parseSourceIds("?source_ids=a&source_ids=a")).toEqual(["a"]);
  });

  it("trims blanks and ignores an empty query", () => {
    expect(parseSourceIds("?source_ids=%20a%20,,b")).toEqual(["a", "b"]);
    expect(parseSourceIds("")).toEqual([]);
  });
});

describe("source list selection", () => {
  function source(patch: Partial<EventSource> = {}): EventSource {
    return {
      id: "src_1",
      name: "AM",
      lifecycle_state: "ENABLED",
      status: "ENABLED",
      ...patch,
    } as EventSource;
  }

  it("hides archived sources until they are asked for", () => {
    const rows = [source(), source({ id: "src_old", lifecycle_state: "ARCHIVED" })];

    expect(visibleSources(rows, false).map((item) => item.id)).toEqual(["src_1"]);
    expect(visibleSources(rows, true)).toHaveLength(2);
  });

  it("keeps a surviving selection and never leaves the pane blank", () => {
    const rows = [source(), source({ id: "src_2" })];

    expect(resolveSelectedSource("src_2", rows)).toBe("src_2");
    // The selected source was archived or deleted underneath the user.
    expect(resolveSelectedSource("src_gone", rows)).toBe("src_1");
    expect(resolveSelectedSource("src_1", [])).toBeNull();
  });
});
