import { describe, expect, it } from "vitest";
import {
  buildImportItems,
  canSubmitCandidate,
  candidateKey,
  decisionsComplete,
  finalPromql,
  initialDraft,
  isSelectable,
  probeValueLabel,
  requiredLabels,
  selectedCount,
  statusLabel,
  type CandidateDraft,
} from "./grafanaImportDraft";
import type { ImportCandidate } from "./types";

function candidate(overrides: Partial<ImportCandidate> = {}): ImportCandidate {
  return {
    panel_id: 2,
    panel_title: "Connections",
    ref_id: "A",
    raw_promql: 'mysql_threads{instance="$instance"}',
    resolved_promql: 'mysql_threads{instance="$instance"}',
    status: "NEEDS_DECISION",
    suggested_name: "MySQL · Connections",
    order: 0,
    reason: "",
    substitutions: [],
    pending_variables: [
      { name: "instance", suggestion: "BIND_LABEL", sample_value: "10.0.0.5", multi: false },
    ],
    display_unit: "short",
    probe_value: 0.83,
    probe_note: "",
    diff_kind: "NEW",
    template_id: null,
    imported_promql: "",
    current_promql: "",
    ...overrides,
  };
}

function draftFor(item: ImportCandidate): CandidateDraft {
  return initialDraft([item])[candidateKey(item)];
}

function decidedDraftFor(item: ImportCandidate): CandidateDraft {
  const draft = draftFor(item);
  for (const variable of item.pending_variables) {
    draft.decisions[variable.name] =
      variable.suggestion === "BIND_LABEL"
        ? { kind: "BIND_LABEL", label: variable.name }
        : { kind: "PIN_VALUE", value: variable.sample_value };
  }
  return draft;
}

describe("initialDraft", () => {
  it("does not carry the retired reference-baseline surface", () => {
    const item = candidate({ status: "READY", pending_variables: [] });
    const draft = initialDraft([item]);
    draft[candidateKey(item)].checked = true;

    expect("baseline" in draft[candidateKey(item)]).toBe(false);
    expect("baseline" in buildImportItems("uid", "dashboard", [item], draft)[0]).toBe(false);
  });

  it("starts every candidate unticked", () => {
    const item = candidate();

    expect(draftFor(item).checked).toBe(false);
  });

  it("starts imported templates switched off", () => {
    // Same trade-off as the shipped catalogue: a query nobody has watched draw
    // anything is worse switched on, because an empty panel reads as broken.
    expect(draftFor(candidate()).enabled).toBe(false);
  });

  it("leaves a bind suggestion undecided until the user chooses", () => {
    const item = candidate();

    expect(draftFor(item).decisions.instance).toEqual({
      kind: "UNDECIDED",
    });
  });

  it("leaves a pin suggestion undecided until the user chooses", () => {
    const item = candidate({
      pending_variables: [
        { name: "cluster", suggestion: "PIN_VALUE", sample_value: "prod-a", multi: true },
      ],
    });

    expect(draftFor(item).decisions.cluster).toEqual({
      kind: "UNDECIDED",
    });
  });
});

describe("what can be ticked", () => {
  it("refuses a candidate with an unusable reason", () => {
    expect(isSelectable(candidate({ status: "UNSUPPORTED" }))).toBe(false);
  });

  it("refuses one Grafana no longer has", () => {
    expect(isSelectable(candidate({ diff_kind: "GONE" }))).toBe(false);
  });

  it("allows an unverified candidate", () => {
    // Unverified says nothing about the query — usually it means the source has
    // no history address. Refusing them would make import useless there.
    expect(isSelectable(candidate({ status: "UNVERIFIED" }))).toBe(true);
  });

  it("refuses a candidate with an undecided variable", () => {
    const item = candidate();
    const draft = draftFor(item);

    expect(decisionsComplete(item, draft)).toBe(false);
    expect(canSubmitCandidate(item, draft)).toBe(false);
  });

  it("allows the candidate after the user explicitly accepts the suggestion", () => {
    const item = candidate();

    expect(canSubmitCandidate(item, decidedDraftFor(item))).toBe(true);
  });

  it("refuses a bound variable with an empty label", () => {
    // The query would keep a literal `{{}}`, which matches nothing and reads
    // exactly like a metric that does not exist here.
    const item = candidate();
    const draft = draftFor(item);
    draft.decisions.instance = { kind: "BIND_LABEL", label: "  " };

    expect(canSubmitCandidate(item, draft)).toBe(false);
  });

  it("refuses a pinned variable with an empty value", () => {
    const item = candidate();
    const draft = draftFor(item);
    draft.decisions.instance = { kind: "PIN_VALUE", value: "" };

    expect(canSubmitCandidate(item, draft)).toBe(false);
  });

  it("drops a candidate whose variable the user chose to skip", () => {
    const item = candidate();
    const draft = draftFor(item);
    draft.decisions.instance = { kind: "SKIP" };

    expect(canSubmitCandidate(item, draft)).toBe(false);
  });

  it("refuses an empty name", () => {
    const item = candidate();
    const draft = decidedDraftFor(item);
    draft.name = "   ";

    expect(canSubmitCandidate(item, draft)).toBe(false);
  });

  it("allows a candidate with no variables at all", () => {
    const item = candidate({ status: "READY", pending_variables: [] });

    expect(canSubmitCandidate(item, draftFor(item))).toBe(true);
  });
});

describe("finalPromql", () => {
  it("turns a bound variable into a template placeholder", () => {
    // `{{instance}}` is how the built-in templates are written, so an imported
    // template ends up indistinguishable from a hand-written one.
    const item = candidate();
    const draft = decidedDraftFor(item);

    expect(finalPromql(item, draft)).toBe('mysql_threads{instance="{{instance}}"}');
  });

  it("lets the bound label differ from the variable name", () => {
    const item = candidate();
    const draft = draftFor(item);
    draft.decisions.instance = { kind: "BIND_LABEL", label: "node" };

    expect(finalPromql(item, draft)).toBe('mysql_threads{instance="{{node}}"}');
  });

  it("turns a pinned variable into a literal", () => {
    const item = candidate();
    const draft = draftFor(item);
    draft.decisions.instance = { kind: "PIN_VALUE", value: "10.0.0.5:9104" };

    expect(finalPromql(item, draft)).toBe('mysql_threads{instance="10.0.0.5:9104"}');
  });

  it.each([
    ['x{a="$instance"}', 'x{a="{{instance}}"}'],
    ['x{a="${instance}"}', 'x{a="{{instance}}"}'],
    ['x{a=~"${instance:regex}"}', 'x{a=~"{{instance}}"}'],
  ])("handles the %s form", (expression, expected) => {
    const item = candidate({ resolved_promql: expression });

    expect(finalPromql(item, decidedDraftFor(item))).toBe(expected);
  });

  it("replaces every occurrence, not only the first", () => {
    const item = candidate({
      resolved_promql: 'a{i="$instance"} / b{i="$instance"}',
    });

    expect(finalPromql(item, decidedDraftFor(item))).toBe(
      'a{i="{{instance}}"} / b{i="{{instance}}"}',
    );
  });

  it("does not eat a longer name that starts with the same text", () => {
    const item = candidate({
      resolved_promql: 'x{a="$instance",b="$instance_role"}',
      pending_variables: [
        { name: "instance", suggestion: "BIND_LABEL", sample_value: "", multi: false },
        {
          name: "instance_role",
          suggestion: "PIN_VALUE",
          sample_value: "primary",
          multi: false,
        },
      ],
    });

    expect(finalPromql(item, decidedDraftFor(item))).toBe(
      'x{a="{{instance}}",b="primary"}',
    );
  });

  it("leaves a candidate without variables untouched", () => {
    // Macros were already resolved server-side; nothing here rewrites a query
    // beyond substituting the user's own decisions.
    const item = candidate({
      status: "READY",
      pending_variables: [],
      resolved_promql: "rate(http_errors_total[5m])",
    });

    expect(finalPromql(item, draftFor(item))).toBe("rate(http_errors_total[5m])");
  });
});

describe("requiredLabels", () => {
  it("lists the labels the template will need", () => {
    const item = candidate();

    expect(requiredLabels(item, decidedDraftFor(item))).toEqual(["instance"]);
  });

  it("ignores pinned variables", () => {
    const item = candidate();
    const draft = decidedDraftFor(item);
    draft.decisions.instance = { kind: "PIN_VALUE", value: "10.0.0.5" };

    expect(requiredLabels(item, draft)).toEqual([]);
  });

  it("does not repeat a label bound twice", () => {
    const item = candidate({
      pending_variables: [
        { name: "instance", suggestion: "BIND_LABEL", sample_value: "", multi: false },
        { name: "node", suggestion: "BIND_LABEL", sample_value: "", multi: false },
      ],
    });
    const draft = decidedDraftFor(item);
    draft.decisions.node = { kind: "BIND_LABEL", label: "instance" };

    expect(requiredLabels(item, draft)).toEqual(["instance"]);
  });
});

describe("buildImportItems", () => {
  it("submits only what is ticked", () => {
    const first = candidate({ status: "READY", pending_variables: [] });
    const second = candidate({
      panel_id: 3,
      status: "READY",
      pending_variables: [],
      suggested_name: "second",
    });
    const draft = initialDraft([first, second]);
    draft[candidateKey(first)].checked = true;

    const items = buildImportItems("uid", "MySQL", [first, second], draft);

    expect(items).toHaveLength(1);
    expect(items[0].origin.panel_id).toBe(2);
  });

  it("submits nothing when nothing is ticked", () => {
    const item = candidate({ status: "READY", pending_variables: [] });

    expect(buildImportItems("uid", "MySQL", [item], initialDraft([item]))).toEqual([]);
  });

  it("skips a ticked candidate that is not yet valid", () => {
    // Ticking is not the same as being submittable; the two must not disagree
    // silently, or the request comes back 422 with nothing on screen to fix.
    const item = candidate();
    const draft = initialDraft([item]);
    draft[candidateKey(item)].checked = true;
    draft[candidateKey(item)].decisions.instance = { kind: "SKIP" };

    expect(buildImportItems("uid", "MySQL", [item], draft)).toEqual([]);
  });

  it("sends Grafana's text alongside the bound literal", () => {
    // The two are different strings once a variable is bound, and they answer
    // different questions. The next re-import asks "did Grafana change?", so
    // comparing against a literal whose `$instance` became `{{instance}}`
    // would answer "yes" forever — and "conflict" for anything also edited.
    const item = candidate();
    const draft = initialDraft([item]);
    draft[candidateKey(item)].checked = true;
    draft[candidateKey(item)].decisions.instance = {
      kind: "BIND_LABEL",
      label: "instance",
    };

    const [built] = buildImportItems("uid", "d", [item], draft);

    expect(built.final_promql).toBe('mysql_threads{instance="{{instance}}"}');
    expect(built.imported_promql).toBe('mysql_threads{instance="$instance"}');
  });

  it("sends the same text for both when there is nothing to bind", () => {
    const item = candidate({
      status: "READY",
      pending_variables: [],
      resolved_promql: "rate(http_errors_total[5m])",
    });
    const draft = initialDraft([item]);
    draft[candidateKey(item)].checked = true;

    const [built] = buildImportItems("uid", "d", [item], draft);

    expect(built.imported_promql).toBe(built.final_promql);
  });

  it("carries the dashboard identity on every item", () => {
    const item = candidate({ status: "READY", pending_variables: [] });
    const draft = initialDraft([item]);
    draft[candidateKey(item)].checked = true;

    const [built] = buildImportItems("mysql-overview", "MySQL Overview", [item], draft);

    expect(built.origin).toEqual({
      dashboard_uid: "mysql-overview",
      dashboard_title: "MySQL Overview",
      panel_id: 2,
      panel_title: "Connections",
      ref_id: "A",
    });
  });

  it("carries the template id when accepting an upstream change", () => {
    const item = candidate({
      status: "READY",
      pending_variables: [],
      diff_kind: "UPSTREAM_CHANGED",
      template_id: 7,
    });
    const draft = initialDraft([item]);
    draft[candidateKey(item)].checked = true;

    expect(buildImportItems("uid", "d", [item], draft)[0].template_id).toBe(7);
  });

  it("does not carry a template id for a new candidate", () => {
    // Sending one would turn an insert into an overwrite of an unrelated row.
    const item = candidate({
      status: "READY",
      pending_variables: [],
      diff_kind: "NEW",
      template_id: 7,
    });
    const draft = initialDraft([item]);
    draft[candidateKey(item)].checked = true;

    expect("template_id" in buildImportItems("uid", "d", [item], draft)[0]).toBe(false);
  });

  it("keeps the dashboard order so priority follows the layout", () => {
    const first = candidate({ status: "READY", pending_variables: [], order: 3 });
    const second = candidate({
      panel_id: 3,
      status: "READY",
      pending_variables: [],
      order: 9,
    });
    const draft = initialDraft([first, second]);
    draft[candidateKey(first)].checked = true;
    draft[candidateKey(second)].checked = true;

    const items = buildImportItems("uid", "d", [first, second], draft);

    expect(items.map((item) => item.order)).toEqual([3, 9]);
  });
});

describe("selectedCount", () => {
  it("counts only submittable ticked candidates", () => {
    const good = candidate({ status: "READY", pending_variables: [] });
    const bad = candidate({ panel_id: 3 });
    const draft = initialDraft([good, bad]);
    draft[candidateKey(good)].checked = true;
    draft[candidateKey(bad)].checked = true;
    draft[candidateKey(bad)].decisions.instance = { kind: "SKIP" };

    expect(selectedCount([good, bad], draft)).toBe(1);
  });
});

describe("what the badge says", () => {
  it.each([
    ["READY", "直接可用"],
    ["NEEDS_DECISION", "需你决定"],
    ["UNVERIFIED", "未验证"],
    ["UNSUPPORTED", "用不了"],
  ] as const)("labels %s", (status, expected) => {
    expect(statusLabel(candidate({ status }))).toBe(expected);
  });

  it("lets a diff classification win over the status", () => {
    // On a re-import "what changed" is the question; "this one runs fine" is
    // not news.
    expect(statusLabel(candidate({ diff_kind: "CONFLICT" }))).toBe("冲突");
    expect(statusLabel(candidate({ diff_kind: "UPSTREAM_CHANGED" }))).toBe("上游已变");
    expect(statusLabel(candidate({ diff_kind: "GONE" }))).toBe("已消失");
  });
});

describe("the observed probe value", () => {
  it("shows the number the query actually returns", () => {
    expect(probeValueLabel(candidate({ probe_value: 0.83 }))).toBe("当前：0.830");
  });

  it("says so rather than sitting empty when there is no value", () => {
    expect(probeValueLabel(candidate({ probe_value: null }))).toBe("无法显示当前值");
  });

  it("does not print a long float for a whole number", () => {
    expect(probeValueLabel(candidate({ probe_value: 42 }))).toBe("当前：42");
  });
});
