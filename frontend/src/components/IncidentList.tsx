/*
 * 告警组列表：主列表只放**活动**告警组，已恢复的组降级为下面一行折叠区。
 *
 * 恢复过的组会在库里留 30 天（retention 只删终结事实），所以一列平铺意味着列表
 * 越用越吵，而标题总数与 severity 汇总会把它们一起算进去——用户把那三个数字当
 * "现在有多少事"读，它实际是"30 天内有多少组"，那是在报假警。
 *
 * 但这是分层不是过滤：上游恢复不改人工处理（CAP-04.9），仍欠结论的组必须还能被
 * 打开、关闭或标记误报，所以折叠行始终在场并说出其中有多少组未处理（CAP-04.9a）。
 */
import { useState } from "react";
import type { EventSource, IncidentSummary, Severity } from "../types";
import { ageFrom, cardClass, sevPill, statePill } from "../util";
import { handlingLabel, showsHandlingPill } from "../handling";

interface Props {
  /** 活动告警组：firing / pending_resolution / unknown。 */
  incidents: IncidentSummary[];
  recoveredIncidents: IncidentSummary[];
  unsettledRecoveredCount: number;
  ruleOptions: { key: string; label: string; activeCount: number }[];
  selectedRuleKey: string | null;
  selectedId: number | null;
  selectedSourceIds: string[];
  sources: EventSource[];
  loading: boolean;
  error: boolean;
  onRuleChange: (key: string) => void;
  onSelect: (id: number) => void;
  onSourceFilterChange: (sourceIds: string[]) => void;
}

const SUMMARY: Severity[] = ["critical", "warning", "info"];

/**
 * One row, shared by the main list and the fold.
 *
 * The two must stay identical: a recovered group is only one level further
 * down, not a different kind of thing, and its handling controls are the whole
 * reason it is still reachable.
 */
function IncidentRow({
  incident,
  selected,
  onSelect,
}: {
  incident: IncidentSummary;
  selected: boolean;
  onSelect: (id: number) => void;
}) {
  return (
    <button
      className={`${cardClass(incident.severity, incident.source_state)}${
        selected ? " card--active" : ""
      }`}
      onClick={() => onSelect(incident.id)}
    >
      <div className="card__title">{incident.title}</div>
      <div className="card__source">{incident.source_name ?? incident.source_id}</div>
      <div className={`card__rule card__rule--${incident.aggregation_status}`}>
        {incident.aggregation_status === "unmatched"
          ? "未命中规则 · 安全隔离"
          : incident.aggregation_status === "missing_labels"
            ? `${incident.aggregation_rule_name ?? `规则 #${incident.aggregation_rule_id}`} · 缺少聚合标签`
            : incident.aggregation_rule_name ?? `规则 #${incident.aggregation_rule_id}`}
      </div>
      <div className="card__meta">
        <span className={sevPill(incident.severity)}>{incident.severity}</span>
        <span className={statePill(incident.source_state)}>{incident.source_state}</span>
        <span className="pill pill--count">{incident.member_count} 条</span>
        {showsHandlingPill(incident.handling_state) && (
          <span className={`pill pill--handling pill--handling-${incident.handling_state.toLowerCase()}`}>
            {handlingLabel(incident.handling_state)}
          </span>
        )}
        <span className="card__age">{ageFrom(incident.updated_at)}</span>
      </div>
    </button>
  );
}

export function IncidentList({
  incidents,
  recoveredIncidents,
  unsettledRecoveredCount,
  ruleOptions,
  selectedRuleKey,
  selectedId,
  selectedSourceIds,
  sources,
  loading,
  error,
  onRuleChange,
  onSelect,
  onSourceFilterChange,
}: Props) {
  /*
   * 三态而不是布尔：null 表示"用户还没表态，跟着选中项走"，于是深链到一个已恢复
   * 的组时折叠区自动是开的。一旦点过，用户的意愿优先——布尔 + effect 同步会让
   * 每 15 秒一次的列表刷新把用户刚收起的折叠区重新推开。
   */
  const [manualExpanded, setManualExpanded] = useState<boolean | null>(null);
  const foldHoldsSelection = recoveredIncidents.some((item) => item.id === selectedId);
  const expanded = manualExpanded ?? foldHoldsSelection;

  const counts = SUMMARY.map(
    (sev) => [sev, incidents.filter((i) => i.severity === sev).length] as const,
  );

  return (
    <div className="pane pane--list">
      <div className="list-head">
        <div className="list-head__top">
          <span className="list-head__title">聚合规则</span>
          <span className="list-head__total">{incidents.length}</span>
        </div>
        <label className="rule-picker">
          <span>全局告警组，可按规则收窄</span>
          <select
            aria-label="选择聚合规则"
            disabled={loading || error}
            onChange={(event) => onRuleChange(event.target.value)}
            value={selectedRuleKey ?? ""}
          >
            {selectedRuleKey === null && <option value="">加载中…</option>}
            {ruleOptions.map((option) => (
              <option key={option.key} value={option.key}>
                {option.label}（{option.activeCount} 组）
              </option>
            ))}
          </select>
        </label>
        {sources.length > 0 && (
          <div className="source-filter" aria-label="来源筛选">
            <div className="source-filter__head">
              <span>来源</span>
              <button
                onClick={() => onSourceFilterChange([])}
                type="button"
              >
                全部
              </button>
            </div>
            {sources.map((source) => (
              <label className="source-filter__item" key={source.id}>
                <input
                  checked={selectedSourceIds.length === 0 || selectedSourceIds.includes(source.id)}
                  onChange={() => {
                    const current = selectedSourceIds.length === 0
                      ? sources.map((item) => item.id)
                      : selectedSourceIds;
                    const next = current.includes(source.id)
                      ? current.filter((id) => id !== source.id)
                      : [...current, source.id];
                    onSourceFilterChange(next.length === sources.length ? [] : next);
                  }}
                  type="checkbox"
                />
                <span>{source.name}</span>
              </label>
            ))}
          </div>
        )}
        <div className="summary">
          {counts.map(([sev, n]) => (
            <div className="summary__item" key={sev}>
              <span className={`sdot sdot--${sev}`} />
              <span className="summary__count">{n}</span>
              <span className="summary__label">{sev}</span>
            </div>
          ))}
        </div>
      </div>

      {error && <div className="error">无法加载告警组，请检查后端。</div>}
      {!error && loading && incidents.length === 0 && <div className="empty">Loading…</div>}
      {!error && !loading && incidents.length === 0 && (
        <div className="empty">
          {recoveredIncidents.length > 0
            ? "当前选择下没有进行中的告警组。"
            : "当前选择下没有告警组。"}
        </div>
      )}

      <div className="rows">
        {incidents.map((inc) => (
          <IncidentRow
            incident={inc}
            key={inc.id}
            onSelect={onSelect}
            selected={inc.id === selectedId}
          />
        ))}
      </div>

      {recoveredIncidents.length > 0 && (
        <section className="recovered-fold">
          <button
            aria-expanded={expanded}
            className="recovered-fold__row"
            onClick={() => setManualExpanded(!expanded)}
            type="button"
          >
            <span className="recovered-fold__title">已恢复</span>
            <span className="recovered-fold__count">{recoveredIncidents.length} 组</span>
            <span className="recovered-fold__note">
              {unsettledRecoveredCount > 0
                ? `其中 ${unsettledRecoveredCount} 组仍未处理`
                : "全部已有处理结论"}
            </span>
            <span className="recovered-fold__more">
              {expanded ? "收起" : "展开"}
              <span aria-hidden="true">{expanded ? "↑" : "↓"}</span>
            </span>
          </button>

          {expanded && (
            <div className="rows rows--recovered">
              {recoveredIncidents.map((inc) => (
                <IncidentRow
                  incident={inc}
                  key={inc.id}
                  onSelect={onSelect}
                  selected={inc.id === selectedId}
                />
              ))}
            </div>
          )}
        </section>
      )}
    </div>
  );
}
