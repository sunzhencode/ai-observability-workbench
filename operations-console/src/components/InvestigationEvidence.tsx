import { useId } from "react";
import type { InvestigatorRun } from "../types";
import { investigatorMetricEvidencePresentation } from "../investigationPresentation";

export function evidenceElementId(prefix: string, evidenceId: string): string {
  return `${prefix}-${encodeURIComponent(evidenceId)}`;
}

export function EvidenceReferences({ ids, run, prefix }: {
  ids: string[];
  run: InvestigatorRun;
  prefix: string;
}) {
  const available = new Set([
    ...run.alert_evidence.map((alert) => alert.evidence_id),
    ...run.metric_evidence.map((metric) => metric.evidence_id),
  ]);
  return <div className="inline-actions">{ids.map((id) => available.has(id) ? (
    <button className="text-btn" key={id} type="button" aria-label={`查看证据 ${id}`}
      onClick={() => {
        const target = document.getElementById(evidenceElementId(prefix, id));
        target?.scrollIntoView({ block: "center" });
        target?.focus({ preventScroll: true });
      }}>证据：{id}</button>
  ) : <small key={id}>证据 {id}（不在本页有界展示范围内）</small>)}</div>;
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "未记录";
}

export function InvestigationEvidence({ run, prefix, alertsOnly = false }: {
  run: InvestigatorRun;
  prefix?: string;
  alertsOnly?: boolean;
}) {
  const fallbackPrefix = useId();
  const elementPrefix = prefix ?? fallbackPrefix;
  return <>
    <p className="incident-readonly-note">冻结于 {new Date(run.created_at).toLocaleString()}。以下是本次调查保存的有界证据，可能不包含全部告警；不会重新查询来源。</p>
    <div className="incident-alert-list" aria-label="调查告警快照">
      {run.alert_evidence.map((alert, index) => {
        const id = text(alert.evidence_id);
        return <article key={`${id}-${index}`} id={evidenceElementId(elementPrefix, id)} data-evidence-id={id} tabIndex={-1}>
          <header><strong>{text(alert.alertname)}</strong><span>{text(alert.severity)} · {text(alert.source_state)}</span></header>
          {typeof alert.summary === "string" && alert.summary ? <p>{alert.summary}</p> : null}
          {typeof alert.description === "string" && alert.description ? <p>{alert.description}</p> : null}
          <small>证据 ID：{id}</small>
        </article>;
      })}
      {run.alert_evidence.length === 0 ? <p>本次调查未保留可展示的告警正文。</p> : null}
    </div>
    {!alertsOnly ? <div className="metric-observation-grid" aria-label="指标证据">
      {run.metric_evidence.map((metric) => {
        const presentation = investigatorMetricEvidencePresentation(metric);
        return <article className="metric-observation" key={metric.evidence_id}
          id={evidenceElementId(elementPrefix, metric.evidence_id)} data-evidence-id={metric.evidence_id} tabIndex={-1}>
          <header><strong>{metric.metric_id}</strong><span>{metric.status === "DATA" ? "已取得" : metric.status === "EMPTY_NO_DATA" ? "窗口内无数据" : "来源不可用"}</span></header>
          <dl>
            <div><dt>最新值</dt><dd>{metric.summary.latest ?? "—"}</dd></div>
            <div><dt>最小值</dt><dd>{metric.summary.minimum ?? "—"}</dd></div>
            <div><dt>最大值</dt><dd>{metric.summary.maximum ?? "—"}</dd></div>
            <div><dt>L1 读取点数</dt><dd>{presentation.pointCount}</dd></div>
          </dl>
          {presentation.sample.length > 0 ? <details className="metric-observation__l2">
            <summary>L2 有界采样 · {presentation.sample.length} 个点</summary>
            <ol>{presentation.sample.map((point, index) => <li key={`${point.timestamp}-${index}`}>
              <time dateTime={new Date(point.timestamp * 1000).toISOString()}>{new Date(point.timestamp * 1000).toLocaleString()}</time>
              <code>{point.value}</code>
            </li>)}</ol>
          </details> : null}
          <small>证据 ID：<code>{metric.evidence_id}</code></small>
        </article>;
      })}
      {run.metric_evidence.length === 0 ? <p>本次调查没有持久指标证据。</p> : null}
    </div> : null}
  </>;
}
