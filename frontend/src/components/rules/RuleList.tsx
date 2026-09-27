/**
 * The rule library, on the left.
 *
 * Rules are an independent resource: they are not created from whatever alert
 * happens to be on screen, which is why this list has its own "新建" and no
 * dependence on the alerts page (`AGENTS.md`, 长期产品边界).
 */
import type { AggregationRule } from "../../types";

export function RuleList({
  rules,
  selectedId,
  loading,
  onSelect,
  onStartNew,
}: {
  rules: AggregationRule[];
  selectedId: number | null;
  loading: boolean;
  onSelect: (rule: AggregationRule) => void;
  onStartNew: () => void;
}) {
  return (
    <aside className="config-sidebar">
      <div className="config-sidebar__head">
        <div>
          <span className="eyebrow">Rule library</span>
          <h1>聚合规则</h1>
        </div>
        <button className="secondary-btn" onClick={onStartNew} type="button">
          + 新建
        </button>
      </div>
      <p className="sidebar-help">规则独立存在，不从某条当前告警创建。</p>
      <div className="rule-list">
        {rules.map((rule) => (
          <button
            className={`rule-row${selectedId === rule.id ? " rule-row--active" : ""}`}
            key={rule.id}
            onClick={() => onSelect(rule)}
            type="button"
          >
            <span>
              <strong>{rule.name}</strong>
              <small>
                优先级 {rule.priority} · v{rule.version}
              </small>
            </span>
            <i className={`rule-row__status${rule.enabled ? " rule-row__status--on" : ""}`} />
          </button>
        ))}
        {!loading && rules.length === 0 && (
          <div className="empty">还没有规则。新建后，告警页会按规则重新分组。</div>
        )}
      </div>
    </aside>
  );
}
