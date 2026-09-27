import type { IncidentOccurrence } from "../../types";
import { handlingLabel } from "../../handling";
import { sevPill } from "../../util";

interface Props {
  occurrence: IncidentOccurrence | null;
  linkedButMissing: boolean;
  onOpenIncident: (incidentId: number) => void;
  onBackToList: () => void;
}

function stamp(iso: string): string {
  const value = new Date(iso);
  if (Number.isNaN(value.getTime())) return "—";
  return value.toLocaleString();
}

export function OccurrenceDetail({
  occurrence,
  linkedButMissing,
  onOpenIncident,
  onBackToList,
}: Props) {
  if (linkedButMissing) {
    return (
      <section className="resource-editor">
        <div className="placeholder-card">
          <strong>这条记录不在当前筛选结果里。</strong>
          <p>它可能属于另一个来源或另一个结论，也可能已过保留期。</p>
          <button className="secondary-btn" onClick={onBackToList} type="button">
            回到列表
          </button>
        </div>
      </section>
    );
  }

  if (occurrence === null) {
    return (
      <section className="resource-editor">
        <div className="placeholder-card">
          <strong>选择一条发生记录。</strong>
          <p>每条记录是一个告警组从开始到恢复的一次完整发生。</p>
        </div>
      </section>
    );
  }

  return (
    <section className="resource-editor">
      <div className="editor-head">
        <h2>{occurrence.title}</h2>
      </div>

      <div className="card__meta">
        <span className={sevPill(occurrence.member_max_severity)}>
          {occurrence.member_max_severity}
        </span>
        <span className="pill pill--count">{occurrence.member_count} 条告警</span>
        <span
          className={`pill pill--handling pill--handling-${occurrence.handling_conclusion.toLowerCase()}`}
        >
          {handlingLabel(occurrence.handling_conclusion)}
        </span>
        <span className="pill">第 {occurrence.occurrence_no} 次发生</span>
      </div>

      <div className="section">
        <div className="section__label">这一次</div>
        <dl className="occurrence-kv">
          <dt>开始</dt>
          <dd>{stamp(occurrence.started_at)}</dd>
          <dt>恢复</dt>
          <dd>{stamp(occurrence.recovered_at)}</dd>
          <dt>来源</dt>
          <dd>{occurrence.source_name}</dd>
          <dt>聚合规则</dt>
          <dd>{occurrence.aggregation_rule_name ?? "未命中规则 · 安全隔离"}</dd>
          <dt>分组标识</dt>
          <dd><code>{occurrence.group_key}</code></dd>
          <dt>成员最高严重度</dt>
          {/* Named for what it is: the max among members when the occurrence was
              sealed, not a running peak over its whole life. */}
          <dd>{occurrence.member_max_severity}（恢复时的成员最高值，非全程峰值）</dd>
        </dl>
      </div>

      <div className="section">
        <div className="section__label">对应的告警组</div>
        <p className="occurrence-note">
          这条记录是独立保存的，不依赖告警组还在。跳过去可能会发现它已被保留期清理。
        </p>
        <button
          className="secondary-btn"
          onClick={() => onOpenIncident(occurrence.incident_id)}
          type="button"
        >
          在告警页打开 #{occurrence.incident_id}
        </button>
      </div>
    </section>
  );
}
