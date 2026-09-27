/**
 * The rule editor: identity, scope, matchers, grouping labels.
 *
 * Markup is carried over from the single-file page unchanged. The one thing to
 * preserve is that every edit invalidates the preview — saving without a fresh
 * preview is not allowed, because the preview is the only place the priority
 * shadowing and group explosion become visible before they are published.
 */
import { useMemo, useState } from "react";
import { SourceScopeEditor } from "./SourceScopeEditor";
import type {
  AggregationLabelCatalog,
  AggregationMatcher,
  AggregationRuleDraft,
  EventSource,
  MatcherOperator,
} from "../../types";

const WINDOWS: { value: 24 | 168 | 720; label: string }[] = [
  { value: 24, label: "24 小时" },
  { value: 168, label: "7 天" },
  { value: 720, label: "30 天" },
];

const OPERATORS: MatcherOperator[] = ["=", "!=", "=~", "!~"];

export function RuleForm({
  draft,
  updateDraft,
  catalog,
  sources,
  lookback,
  onLookbackChange,
}: {
  draft: AggregationRuleDraft;
  updateDraft: (patch: Partial<AggregationRuleDraft>) => void;
  catalog: AggregationLabelCatalog | null;
  sources: EventSource[];
  lookback: 24 | 168 | 720;
  onLookbackChange: (value: 24 | 168 | 720) => void;
}) {
  const [customGroupLabel, setCustomGroupLabel] = useState("");
  const labels = catalog?.labels ?? [];
  const labelNames = labels.map((label) => label.name);
  const highCardinalityLabels = useMemo(
    () =>
      labels.filter(
        (label) => draft.group_by_labels.includes(label.name) && label.distinct_count >= 100,
      ),
    [draft.group_by_labels, labels],
  );

  const updateMatcher = (index: number, patch: Partial<AggregationMatcher>) => {
    updateDraft({
      matchers: draft.matchers.map((matcher, matcherIndex) =>
        matcherIndex === index ? { ...matcher, ...patch } : matcher,
      ),
    });
  };

  const removeMatcher = (index: number) => {
    updateDraft({
      matchers: draft.matchers.filter((_, matcherIndex) => matcherIndex !== index),
    });
  };

  const toggleGroupLabel = (name: string) => {
    updateDraft({
      group_by_labels: draft.group_by_labels.includes(name)
        ? draft.group_by_labels.filter((label) => label !== name)
        : [...draft.group_by_labels, name],
    });
  };

  const addCustomGroupLabel = () => {
    const value = customGroupLabel.trim();
    if (!value || draft.group_by_labels.includes(value)) return;
    updateDraft({ group_by_labels: [...draft.group_by_labels, value] });
    setCustomGroupLabel("");
  };

  return (
    <>
        <section className="config-section">
          <div className="config-section__head">
            <div>
              <h3>基本信息</h3>
              <p>数字越小越先匹配；同优先级按规则 ID 执行，第一条命中即停止。</p>
            </div>
          </div>
          <div className="rule-basics">
            <label>
              <span>规则名称</span>
              <input
                maxLength={128}
                onChange={(event) => updateDraft({ name: event.target.value })}
                placeholder="例如：CrashLoop 按 namespace 聚合"
                value={draft.name}
              />
            </label>
            <label>
              <span>优先级</span>
              <input
                min={0}
                max={100000}
                onChange={(event) => updateDraft({ priority: Number(event.target.value) })}
                type="number"
                value={draft.priority}
              />
            </label>
            <label className="rule-enabled">
              <input
                checked={draft.enabled}
                onChange={(event) => updateDraft({ enabled: event.target.checked })}
                type="checkbox"
              />
              <span>启用规则</span>
            </label>
            <label>
              <span>首次协作通知等待</span>
              <select
                onChange={(event) => updateDraft({
                  grouping_window_seconds: Number(event.target.value) as 0 | 30 | 60 | 120 | 300,
                })}
                value={draft.grouping_window_seconds ?? 30}
              >
                <option value={0}>立即发送</option>
                <option value={30}>等待 30 秒（推荐）</option>
                <option value={60}>等待 1 分钟</option>
                <option value={120}>等待 2 分钟</option>
                <option value={300}>等待 5 分钟</option>
              </select>
              <small>事件和原始告警立即可见；这里只等待同组告警汇合后再发第一条平台消息，严重度升级会立即发送。</small>
            </label>
          </div>
        </section>

        <SourceScopeEditor
          onChange={(source_scope) => updateDraft({ source_scope })}
          sources={sources}
          value={draft.source_scope}
        />

        <section className="config-section">
          <div className="config-section__head">
            <div>
              <h3>匹配条件</h3>
              <p>所有条件同时满足才命中；alertname 只是普通 label，不是页面父级。</p>
            </div>
            <button
              className="secondary-btn"
              onClick={() => updateDraft({
                matchers: [...draft.matchers, { label: "", operator: "=", value: "" }],
              })}
              type="button"
            >
              + 添加条件
            </button>
          </div>
          {draft.matchers.length === 0 ? (
            <div className="preview-warning">没有匹配条件：这是一条 catch-all 规则，会尝试匹配所有告警。</div>
          ) : (
            <div className="matcher-list">
              {draft.matchers.map((matcher, index) => {
                const values = labels.find((label) => label.name === matcher.label)?.sample_values ?? [];
                return (
                  <div className="matcher-row" key={`${index}-${matcher.label}`}>
                    <input
                      list="aggregation-label-names"
                      onChange={(event) => updateMatcher(index, { label: event.target.value })}
                      placeholder="label"
                      value={matcher.label}
                    />
                    <select
                      onChange={(event) => updateMatcher(index, { operator: event.target.value as MatcherOperator })}
                      value={matcher.operator}
                    >
                      {OPERATORS.map((operator) => <option key={operator}>{operator}</option>)}
                    </select>
                    <input
                      list={`matcher-values-${index}`}
                      onChange={(event) => updateMatcher(index, { value: event.target.value })}
                      placeholder="value / regex"
                      value={matcher.value}
                    />
                    <datalist id={`matcher-values-${index}`}>
                      {values.map((value) => <option key={value} value={value} />)}
                    </datalist>
                    <button onClick={() => removeMatcher(index)} type="button">移除</button>
                  </div>
                );
              })}
            </div>
          )}
          <datalist id="aggregation-label-names">
            {labelNames.map((name) => <option key={name} value={name} />)}
          </datalist>
        </section>

        <section className="config-section">
          <div className="config-section__head">
            <div>
              <h3>聚合标签</h3>
              <p>命中规则后，标签值完全相同的告警进入同一组；缺标签的告警保持 fingerprint 隔离。</p>
            </div>
            <select
              aria-label="标签发现窗口"
              onChange={(event) => onLookbackChange(Number(event.target.value) as 24 | 168 | 720)}
              value={lookback}
            >
              {WINDOWS.map((window) => <option key={window.value} value={window.value}>{window.label}</option>)}
            </select>
          </div>
          <div className={`source-note source-note--${catalog?.history_status ?? "unconfigured"}`}>
            标签候选：当前 Alertmanager + Thanos 历史（{catalog?.history_status ?? "loading"}）
          </div>
          <div className="label-table">
            {labels.map((label) => (
              <label className="label-option" key={label.name}>
                <input
                  checked={draft.group_by_labels.includes(label.name)}
                  onChange={() => toggleGroupLabel(label.name)}
                  type="checkbox"
                />
                <span className="label-option__name">{label.name}</span>
                <span>{Math.round(label.coverage * 100)}% coverage</span>
                <span>{label.distinct_count} values</span>
                <span>{label.sources.join(" + ")}</span>
                <code>{label.sample_values.join(", ") || "—"}</code>
              </label>
            ))}
          </div>
          <div className="custom-label-row">
            <input
              onChange={(event) => setCustomGroupLabel(event.target.value)}
              placeholder="候选中没有？直接输入 label name"
              value={customGroupLabel}
            />
            <button className="secondary-btn" onClick={addCustomGroupLabel} type="button">添加</button>
          </div>
          {highCardinalityLabels.length > 0 && (
            <div className="preview-warning">
              高基数提醒：{highCardinalityLabels.map((label) => `${label.name} (${label.distinct_count})`).join(", ")}。
              当前不会硬拦截，请务必先看预览组数。
            </div>
          )}
        </section>
    </>
  );
}
