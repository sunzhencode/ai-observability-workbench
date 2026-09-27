/**
 * 聚合规则 — the container.
 *
 * The selected rule is in the URL (`?rule=<id>`, or `new` for the add form),
 * so a rule can be linked to. The draft is local: it is unsaved text.
 *
 * The label-discovery window stays local too. It changes which candidates the
 * form offers, not what the page is showing, and putting it in the address bar
 * would suggest it is part of the rule.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router";
import { MetricTemplateList } from "../components/rules/MetricTemplateList";
import { RuleForm } from "../components/rules/RuleForm";
import { RuleList } from "../components/rules/RuleList";
import { RulePreview } from "../components/rules/RulePreview";
import { useEventSources } from "../queries/catalog";
import { useRules } from "../queries/catalog";
import {
  useAggregationLabels,
  usePreviewAggregationRule,
  useSaveAggregationRule,
  type LabelWindow,
} from "../queries/rules";
import { describeRequestFailure } from "../requestError";
import type { AggregationRule, AggregationRuleDraft, AggregationRulePreview } from "../types";

const NEW_RULE = "new";

function newDraft(): AggregationRuleDraft {
  return {
    name: "",
    priority: 100,
    enabled: true,
    matchers: [{ label: "alertname", operator: "=", value: "" }],
    group_by_labels: [],
    grouping_window_seconds: 30,
    source_scope: { mode: "ALL", source_ids: [] },
  };
}

function draftFromRule(rule: AggregationRule): AggregationRuleDraft {
  return {
    name: rule.name,
    priority: rule.priority,
    enabled: rule.enabled,
    matchers: rule.matchers.map((matcher) => ({ ...matcher })),
    group_by_labels: [...rule.group_by_labels],
    grouping_window_seconds: rule.grouping_window_seconds,
    source_scope: rule.source_scope ?? { mode: "ALL", source_ids: [] },
  };
}

/** The rule the URL names, falling back to the first one so the pane is never blank. */
function resolveSelectedRule(param: string | null, rules: AggregationRule[]): number | null {
  if (param === NEW_RULE) return null;
  const asId = Number(param);
  if (param !== null && Number.isSafeInteger(asId) && rules.some((rule) => rule.id === asId)) {
    return asId;
  }
  return rules[0]?.id ?? null;
}

export function RuleWorkspace({ mode }: { mode: "groups" | "metrics" }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const search = searchParams.toString();
  const ruleParam = searchParams.get("rule");
  const creating = ruleParam === NEW_RULE;

  const [lookback, setLookback] = useState<LabelWindow>(168);
  const [draft, setDraft] = useState<AggregationRuleDraft>(newDraft());
  const [preview, setPreview] = useState<AggregationRulePreview | null>(null);
  const [saveState, setSaveState] = useState<"idle" | "saved" | "error">("idle");
  const [error, setError] = useState("");

  const rulesQuery = useRules();
  const catalogQuery = useAggregationLabels(lookback);
  const sourcesQuery = useEventSources();
  const runPreviewMutation = usePreviewAggregationRule();
  const saveMutation = useSaveAggregationRule();

  const rules = useMemo(() => rulesQuery.data ?? [], [rulesQuery.data]);
  const sources = useMemo(() => sourcesQuery.data ?? [], [sourcesQuery.data]);

  const selectedId = creating ? null : resolveSelectedRule(ruleParam, rules);
  const selectedRule = useMemo(
    () => rules.find((rule) => rule.id === selectedId) ?? null,
    [rules, selectedId],
  );

  // Load the selection into the draft when it changes identity or version.
  //
  // This deliberately does not clear `saveState`. Saving moves the selection —
  // a new rule gets an id, an edited one gets a version — so clearing here
  // would wipe the "已保存" line the save just produced. The result is cleared
  // where the user actually moves on: editing, or picking another rule.
  useEffect(() => {
    if (creating) {
      setDraft(newDraft());
    } else if (selectedRule) {
      setDraft(draftFromRule(selectedRule));
    }
    setPreview(null);
  }, [creating, selectedRule?.id, selectedRule?.version]);

  const setRuleParam = useCallback(
    (value: string | null) => {
      const next = new URLSearchParams(search);
      if (value === null) next.delete("rule");
      else next.set("rule", value);
      setSearchParams(next, { replace: true });
    },
    [search, setSearchParams],
  );

  /** Moving to another rule is where a previous save's result stops applying. */
  const chooseRule = useCallback(
    (value: string) => {
      setSaveState("idle");
      setError("");
      setRuleParam(value);
    },
    [setRuleParam],
  );

  /** Any edit invalidates the preview the save button is gated on. */
  const updateDraft = (patch: Partial<AggregationRuleDraft>) => {
    setDraft((current) => ({ ...current, ...patch }));
    setPreview(null);
    setSaveState("idle");
    setError("");
  };

  const runPreview = async () => {
    setError("");
    try {
      setPreview(await runPreviewMutation.mutateAsync({ draft, ruleId: selectedId }));
    } catch (cause) {
      const failure = describeRequestFailure(cause, "规则预览失败。");
      setError(failure.detail ?? failure.title);
    }
  };

  const saveRule = async () => {
    if (!preview || saveMutation.isPending) return;
    setSaveState("idle");
    setError("");
    try {
      const saved = await saveMutation.mutateAsync({ draft, ruleId: selectedId });
      setRuleParam(String(saved.id));
      setDraft(draftFromRule(saved));
      setPreview(null);
      setSaveState("saved");
    } catch (cause) {
      setSaveState("error");
      const failure = describeRequestFailure(cause, "规则保存失败。");
      setError(failure.detail ?? failure.title);
    }
  };

  const loadError =
    rulesQuery.isError || catalogQuery.isError || sourcesQuery.isError
      ? "规则或标签目录暂时不可用；请刷新后再编辑聚合规则。"
      : "";

  // `.domain-tabs` is the shape the notifications page already uses. Reusing it
  // keeps one tab convention in the product — and, more practically, `.config-page`
  // below is a two-column **grid**: a tab strip placed inside it becomes a grid
  // item and pushes the rule list into the second column. The tabs have to sit
  // outside that grid, which is what `rules-shell` is for.
  if (mode === "metrics") {
    return (
      <div className="rules-shell">
        <main className="management-page rules-metrics-page">
          <MetricTemplateList />
        </main>
      </div>
    );
  }

  return (
    <div className="rules-shell">
      <main className="config-page rules-page">
      <RuleList
        loading={rulesQuery.isPending}
        onSelect={(rule) => chooseRule(String(rule.id))}
        onStartNew={() => chooseRule(NEW_RULE)}
        rules={rules}
        selectedId={creating ? null : selectedId}
      />

      <section className="config-workspace">
        <header className="config-title">
          <div>
            <span className="eyebrow">{selectedRule ? `Rule #${selectedRule.id}` : "New rule"}</span>
            <h2>{selectedRule ? selectedRule.name : "新建聚合规则"}</h2>
          </div>
          <span className={`config-state${draft.enabled ? " config-state--ok" : ""}`}>
            {draft.enabled ? "enabled" : "disabled"}
          </span>
        </header>

        <div className="boundary-note">
          这里只决定“哪些告警命中规则、按哪些标签合成一组”。保存后，告警页按启用规则重新计算。
        </div>

        <RuleForm
          catalog={catalogQuery.data ?? null}
          draft={draft}
          lookback={lookback}
          onLookbackChange={setLookback}
          sources={sources}
          updateDraft={updateDraft}
        />

        <RulePreview
          error={error || loadError}
          isNew={selectedId === null}
          onRunPreview={() => void runPreview()}
          onSave={() => void saveRule()}
          preview={preview}
          previewing={runPreviewMutation.isPending}
          saveState={saveState}
          saving={saveMutation.isPending}
        />
      </section>
      </main>
    </div>
  );
}

export function RulesPage() {
  return <RuleWorkspace mode="groups" />;
}
