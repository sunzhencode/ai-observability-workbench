import { useEffect, useId, useRef, useState } from "react";
import { EvidenceReferences, InvestigationEvidence } from "./InvestigationEvidence";
import { backgroundReadState } from "../backgroundReadState";
import {
  useCancelInvestigation,
  useInvestigationFeedback,
  useLegacyOccurrenceInvestigations,
  useOccurrenceInvestigations,
  useStartOccurrenceInvestigation,
} from "../queries/investigations";
import type { InvestigatorRun, OperationalOccurrence } from "../types";
import {
  investigatorConfidenceAuditLabel,
  investigatorEvidenceReferenceLabel,
  investigatorVerdictLabel,
} from "../investigationPresentation";

const START_FEEDBACK_MS = 1_200;

const STATUS_LABELS: Record<InvestigatorRun["status"], string> = {
  QUEUED: "已排队",
  RUNNING: "调查中",
  COMPLETED: "已完成",
  DEGRADED: "仅交付现有证据",
  FAILED: "调查未完成",
  CANCELED: "已停止",
};

function activityLabel(kind: string): string {
  const labels: Record<string, string> = {
    run_started: "调查运行已开始",
    model_request_started: "已向模型提交冻结证据",
    model_request_completed: "模型响应已返回",
    tool_call_started: "只读指标读取已开始",
    tool_call_completed: "只读指标读取已记录",
    run_completed: "调查报告已完成",
  };
  return labels[kind] ?? kind;
}

export function InvestigatorPanel({ occurrence, mode = "REPORT" }: {
  occurrence: OperationalOccurrence;
  mode?: "REPORT" | "EVIDENCE" | "ALERTS";
}) {
  const prefix = useId();
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const runs = useOccurrenceInvestigations(occurrence.id);
  const start = useStartOccurrenceInvestigation(occurrence.id);
  const cancel = useCancelInvestigation(occurrence.id);
  const feedback = useInvestigationFeedback(occurrence.id);
  const legacyRuns = useLegacyOccurrenceInvestigations(occurrence.id);
  const selectedRun = runs.data?.find((run) => run.id === selectedRunId) ?? runs.data?.[0] ?? null;
  const activeRun = runs.data?.find((run) => run.status === "QUEUED" || run.status === "RUNNING") ?? null;
  const [notice, setNotice] = useState("");
  const [coolingDown, setCoolingDown] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const readState = backgroundReadState({
    hasData: runs.data !== undefined,
    isError: runs.isError,
    isPending: runs.isPending,
  });
  const active = activeRun !== null;

  useEffect(() => {
    setSelectedRunId(null);
    setNotice("");
    setCoolingDown(false);
    if (timer.current !== null) clearTimeout(timer.current);
    timer.current = null;
  }, [occurrence.id]);

  useEffect(() => () => {
    if (timer.current !== null) clearTimeout(timer.current);
  }, []);

  const startRun = () => {
    setNotice("");
    start.mutate(undefined, {
      onSuccess: (result) => {
        setSelectedRunId(result.id);
        setNotice(
          result.status === "QUEUED"
            ? "证据快照已保存，调查已排队。"
            : "证据快照已保存；当前条件不足，未调用模型。",
        );
        setCoolingDown(true);
        timer.current = setTimeout(() => {
          setCoolingDown(false);
          timer.current = null;
        }, START_FEEDBACK_MS);
      },
    });
  };

  const alerts = selectedRun?.alert_evidence.length ?? 0;
  const dataMetrics = selectedRun?.metric_evidence.filter((item) => item.status === "DATA").length ?? 0;
  const emptyMetrics = selectedRun?.metric_evidence.filter((item) => item.status === "EMPTY_NO_DATA").length ?? 0;
  const unavailableMetrics = selectedRun?.metric_evidence.filter((item) => item.status === "SOURCE_UNAVAILABLE").length ?? 0;

  return (
    <section className="incident-overview__section investigation-panel" aria-label={mode === "REPORT" ? "AI Investigator" : "历史调查证据"}>
      <div className="response-actions__heading">
        <div><span className="eyebrow">只读取证</span><h2>{mode === "REPORT" ? "AI 调查" : "调查冻结证据"}</h2></div>
        <small>打开页面不会启动调查</small>
      </div>
      {mode === "REPORT" ? <>
      <div className="investigation-disclosure">
        <strong>启动后会发生什么</strong>
        <p>平台冻结当前告警和指标证据，再由一个受限调查器按需调用“列出、说明、读取指标”三个只读工具，最后生成结构化中文报告。</p>
        <small>可能产生模型费用；不会执行命令、修改监控源或改变人工处置。恢复任务也不会自动重发结果不明的计费请求。</small>
      </div>
      <button
        className="primary-btn"
        disabled={start.isPending || coolingDown || active}
        onClick={startRun}
        type="button"
      >{start.isPending ? "正在保存证据…" : coolingDown ? "证据快照已保存" : selectedRun ? "重新调查当前事件" : "开始 AI 调查"}</button>
      {active ? (
        <button
          className="secondary-btn"
          disabled={cancel.isPending}
          onClick={() => activeRun && cancel.mutate(activeRun.id)}
          type="button"
        >{cancel.isPending ? "正在停止…" : "停止调查"}</button>
      ) : null}
      {cancel.error instanceof Error ? <div className="error" role="alert">停止请求未确认：{cancel.error.message}。请核对调查状态后重试。</div> : null}
      </> : null}
      {activeRun && activeRun.id !== selectedRun?.id && mode === "REPORT" ? <p role="status">当前另有调查正在运行；停止按钮只影响进行中的调查，不改变下方历史报告。</p> : null}
      {(runs.data?.length ?? 0) > 0 ? <label className="investigation-run-selector">
        <span>查看哪次调查</span>
        <select aria-label="查看哪次调查" value={selectedRun?.id ?? ""} onChange={(event) => setSelectedRunId(event.target.value)}>
          {runs.data?.map((run) => <option key={run.id} value={run.id}>{new Date(run.created_at).toLocaleString()} · {STATUS_LABELS[run.status]} · {run.id}</option>)}
        </select>
      </label> : null}
      {notice ? <div className="response-command-result" role="status">{notice}</div> : null}
      {start.error instanceof Error ? (
        <div className="error" role="alert">调查未启动：{start.error.message}。事件状态没有改变。</div>
      ) : null}
      {readState === "UNAVAILABLE" ? (
        <div className="error" role="alert">调查记录暂时无法读取；人工响应功能仍可使用。</div>
      ) : readState === "STALE" ? (
        <div className="incident-readonly-note" role="status">最新刷新未完成，当前继续显示上次成功读取的调查记录。</div>
      ) : null}

      {runs.isError ? <button className="secondary-btn" type="button"
        disabled={runs.isFetching} onClick={() => void runs.refetch()}>
        {runs.isFetching ? "正在重新读取调查记录…" : "重新读取调查记录"}
      </button> : null}

      {selectedRun ? (
        <article className="evidence-brief" aria-label="调查运行">
          <header>
            <div><span className="eyebrow">证据快照</span><h3>所选调查</h3></div>
            <span>{STATUS_LABELS[selectedRun.status]}</span>
          </header>
          <dl>
            <div><dt>已纳入告警</dt><dd>{alerts} 条</dd></div>
            <div><dt>指标证据</dt><dd>{dataMetrics} 条有数据</dd></div>
            <div><dt>窗口内无数据</dt><dd>{emptyMetrics} 条</dd></div>
            <div><dt>读取未完成</dt><dd>{unavailableMetrics} 条</dd></div>
          </dl>

          <InvestigationEvidence run={selectedRun} prefix={prefix} alertsOnly={mode === "ALERTS"} />

          {selectedRun.degraded_domains.length > 0 ? (
            <div className="evidence-brief__degraded" role="status">
              <strong>未取得的证据域</strong>
              <ul>{selectedRun.degraded_domains.map((domain) => <li key={domain}><code>{domain}</code></li>)}</ul>
              <small>缺失范围保持可见，不会被当作“没有异常”。</small>
            </div>
          ) : null}

          {mode === "REPORT" && selectedRun.activities.length > 0 ? (
            <section className="evidence-expansion" aria-label="调查活动">
              <div><span className="eyebrow">持久审计</span><h3>调查活动</h3></div>
              <ol>{selectedRun.activities.map((activity) => (
                <li key={activity.sequence}>
                  <strong>{activity.sequence}. {activityLabel(activity.kind)}</strong>
                  <span>{activity.status} · {activity.safe_code}</span>
                </li>
              ))}</ol>
            </section>
          ) : null}

          {mode === "REPORT" && selectedRun.report ? (
            <section className="analyst-result" aria-label="调查报告">
              <header>
                <div><span className="eyebrow">结构化输出已校验</span><h3>调查报告</h3></div>
                <span>{investigatorVerdictLabel(selectedRun.report.verdict)}</span>
              </header>
              <p className="analyst-result__summary">{selectedRun.report.summary_zh}</p>
              <div className="analyst-hypotheses">
                {selectedRun.report.findings.map((finding) => (
                  <article key={finding.title_zh}>
                    <header>
                      <strong>{finding.title_zh}</strong>
                      <span>{investigatorEvidenceReferenceLabel(finding.evidence_ids)}</span>
                    </header>
                    <p>{finding.analysis_zh}</p>
                    <EvidenceReferences ids={finding.evidence_ids} run={selectedRun} prefix={prefix} />
                  </article>
                ))}
              </div>
              {selectedRun.report.missing_evidence_zh.length > 0 ? (
                <div className="analyst-result__missing"><strong>还需要补充</strong><ul>{selectedRun.report.missing_evidence_zh.map((item) => <li key={item}>{item}</li>)}</ul></div>
              ) : null}
              {selectedRun.report.recommended_actions.length > 0 ? (
                <div className="analyst-actions">
                  <strong>建议下一步</strong>
                  {selectedRun.report.recommended_actions.map((action) => (
                    <article key={action.title_zh}>
                      <span>人工核对 · {investigatorEvidenceReferenceLabel(action.evidence_ids)}</span>
                      <p><strong>{action.title_zh}</strong>：{action.rationale_zh}</p>
                      <EvidenceReferences ids={action.evidence_ids} run={selectedRun} prefix={prefix} />
                    </article>
                  ))}
                  <small>建议不会自动执行，也不会改变事件状态。</small>
                </div>
              ) : null}
              <div className="analyst-feedback">
                <div>
                  <strong>这份报告是否改变了你的下一步？</strong>
                  <small>仅记录调查效果，不执行建议，也不改变事件状态。</small>
                </div>
                <div className="inline-actions">
                  <button className="secondary-btn" disabled={feedback.isPending} onClick={() => feedback.mutate({ investigationId: selectedRun.id, rating: "USEFUL" })} type="button">有帮助</button>
                  <button className="secondary-btn" disabled={feedback.isPending} onClick={() => feedback.mutate({ investigationId: selectedRun.id, rating: "NOT_USEFUL" })} type="button">没帮助</button>
                  <button className="secondary-btn" disabled={feedback.isPending} onClick={() => feedback.mutate({ investigationId: selectedRun.id, rating: "ADOPTED" })} type="button">已采用建议</button>
                </div>
                {selectedRun.feedback ? <small role="status">已记录：{selectedRun.feedback.rating === "ADOPTED" ? "已采用建议" : selectedRun.feedback.rating === "USEFUL" ? "有帮助" : "没帮助"}</small> : null}
                {feedback.error instanceof Error ? <div className="error" role="alert">反馈未记录：{feedback.error.message}。</div> : null}
              </div>
            </section>
          ) : mode === "REPORT" && (selectedRun.status === "FAILED" || selectedRun.status === "DEGRADED") ? (
            <div className="incident-readonly-note" role="status">
              本次没有生成调查报告；已取得的证据仍保留。{selectedRun.safe_error_code ? <code>{selectedRun.safe_error_code}</code> : null}
            </div>
          ) : null}

          <details className="investigation-usage-audit">
            <summary>查看调用与版本信息</summary>
            <dl>
              <div><dt>模型请求</dt><dd>{selectedRun.request_count} 次</dd></div>
              <div><dt>只读工具调用</dt><dd>{selectedRun.tool_call_count} 次</dd></div>
              <div><dt>文本用量</dt><dd>{selectedRun.input_tokens + selectedRun.output_tokens} tokens</dd></div>
              <div><dt>模型配置</dt><dd>{selectedRun.model_revision === null ? "未调用模型" : `版本 ${selectedRun.model_revision}`}</dd></div>
              {selectedRun.report ? <div><dt>报告评分</dt><dd>{investigatorConfidenceAuditLabel(selectedRun.report.confidence)}</dd></div> : null}
              <div><dt>可用指标目录</dt><dd>{selectedRun.available_metric_count} 项</dd></div>
              <div><dt>更新时间</dt><dd>{new Date(selectedRun.updated_at).toLocaleString()}</dd></div>
            </dl>
            <small>这里不展示凭证、PromQL、完整标签值或 L3 全量点位。</small>
          </details>
        </article>
      ) : (
        <div className="incident-readonly-note">{readState === "LOADING" ? "正在读取调查快照…" : readState === "READY" ? "本次事件没有保留的调查快照，无法还原当时的告警与指标。人工响应和时间线仍可查看。" : "暂时无法确认是否有调查快照，请重新读取。"}</div>
      )}
      {(legacyRuns.data?.length ?? 0) > 0 ? (
        <details className="investigation-usage-audit">
          <summary>旧版调查记录（只读，{legacyRuns.data?.length ?? 0} 条）</summary>
          <ol>{legacyRuns.data?.map((run) => (
            <li key={run.id}>{new Date(run.created_at).toLocaleString()} · {run.phase} · {run.status}</li>
          ))}</ol>
          <small>旧记录仅供审计；不能续跑、重试或写入，新的调查统一进入 V2 Investigator。</small>
        </details>
      ) : null}
    </section>
  );
}
