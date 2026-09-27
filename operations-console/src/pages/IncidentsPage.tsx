import { useEffect, useMemo, useState } from "react";
import { useLocation, useNavigate, useParams, useSearchParams } from "react-router";
import {
  OPERATIONAL_SIGNAL_STATES,
  QUEUE_VIEWS,
  parseQueueSelection,
  withQueueSelection,
} from "../appUrl";
import { useEventSources } from "../queries/catalog";
import {
  useAddOccurrenceNote,
  useOperationalOccurrence,
  useOperationalOccurrences,
  useStartHandlingOccurrence,
  useBatchStartHandlingOccurrences,
  useCreateOccurrenceTask,
  useOccurrenceTimeline,
  useOccurrenceTasks,
  useSimilarOccurrenceHistory,
  useRedactOccurrenceNote,
  useResolveOccurrence,
  useTransitionOccurrenceTask,
} from "../queries/operationalOccurrences";
import { useIncident } from "../queries/incidents";
import { useAssignOccurrenceService, useServices } from "../queries/services";
import {
  useCreateOccurrenceSuppression,
  useEndOccurrenceSuppression,
  useOccurrenceNoise,
} from "../queries/noise";
import { MetricEvidence } from "../components/alerts/MetricEvidence";
import { InvestigatorPanel } from "../components/InvestigatorPanel";
import {
  availableResolutionCodes,
  RESOLUTION_LABELS,
  resolutionHelp,
  resolutionReasonRequired,
} from "../responseActions";
import type {
  AckSlaState,
  OperationalOccurrence,
  IncidentTask,
  OperationalSignalState,
  QueueView,
  ResolutionCode,
  NoiseState,
} from "../types";
import { ageFrom } from "../util";
import { ApiError } from "../api/client";
import { backgroundReadState } from "../backgroundReadState";
import { occurrenceDetailState } from "../occurrenceDetailState";
import { queueSourceText } from "../incidentPresentation";

const SERVICE_CRITICALITY_LABELS = {
  TIER_0: "核心交易 · 5 分钟确认",
  TIER_1: "关键服务 · 15 分钟确认",
  TIER_2: "重要服务 · 30 分钟确认",
  TIER_3: "一般服务 · 60 分钟确认",
} as const;

const VIEW_LABELS: Record<QueueView, string> = {
  ALL: "全部未解决",
  UNACKNOWLEDGED: "待确认",
  SLA_AT_RISK: "SLA 风险",
  UNMAPPED: "未映射服务",
  RESOLVED: "已结束",
};

const SIGNAL_LABELS: Record<OperationalSignalState, string> = {
  FIRING: "持续触发",
  RECOVERED: "上游已恢复",
  UNKNOWN: "来源状态未知",
  STALE: "来源已停用",
};

const RESPONSE_LABELS = {
  UNACKNOWLEDGED: "待确认",
  IN_PROGRESS: "处理中",
  RESOLVED: "已结束",
} as const;

const TASK_STATUS_LABELS = {
  TODO: "待开始",
  IN_PROGRESS: "进行中",
  DONE: "已完成",
  CANCELED: "已取消",
} as const;

const ACTOR_LABELS = {
  INTERACTIVE_OPERATOR: "人工操作",
  SYSTEM: "系统记录",
} as const;

function responseHistoryLabel(value: unknown): string {
  const labels: Record<string, string> = {
    UNACKNOWLEDGED: "待确认",
    IN_PROGRESS: "处理中",
    RESOLVED: "已结束",
    ACKNOWLEDGED: "已确认（历史状态）",
    INVESTIGATING: "调查中（历史状态）",
    MITIGATING: "缓解中（历史状态）",
    MONITORING: "观察中（历史状态）",
  };
  return labels[String(value)] ?? "其他历史状态";
}

function taskHistoryLabel(value: unknown): string {
  return TASK_STATUS_LABELS[String(value) as keyof typeof TASK_STATUS_LABELS]
    ?? "其他任务状态";
}

function severityTone(severity: string): string {
  if (severity.toLowerCase() === "critical") return "critical";
  if (severity.toLowerCase() === "warning") return "warning";
  return "info";
}

function severityLabel(severity: string): string {
  if (severity.toLowerCase() === "critical") return "严重";
  if (severity.toLowerCase() === "warning") return "警告";
  if (severity.toLowerCase() === "info") return "提示";
  return "未标明";
}

const SLA_LABELS: Record<AckSlaState, string> = {
  NOT_STARTED: "等待可信发现",
  ON_TRACK: "确认时限正常",
  AT_RISK: "确认时限临近超时",
  BREACHED: "确认已超时",
  STOPPED: "确认时限已停止",
};

const EVIDENCE_LABELS = {
  COMPLETE: "完整",
  PARTIAL: "部分取得",
  FAILED: "未取得",
  UNKNOWN: "尚未确认",
} as const;

const NOISE_LABELS: Record<NoiseState, string> = {
  NONE: "正常通知",
  GROUPING: "等待同组告警",
  FLAPPING: "反复触发合并",
  STORM: "告警风暴聚合",
  MAINTENANCE: "维护窗口",
  SUPPRESSED: "本次事件暂停通知",
};

function noiseWindow(item: OperationalOccurrence): string {
  if (item.noise_remaining_seconds !== null) {
    return `剩余 ${duration(item.noise_remaining_seconds)}`;
  }
  if (item.noise_starts_at) return `自 ${new Date(item.noise_starts_at).toLocaleString()}`;
  return "当前生效";
}

const SERVICE_ASSIGNMENT_LABELS = {
  UNMAPPED: "尚未映射服务",
  MAPPED: "已确定服务归属",
  SERVICE_AMBIGUOUS: "成员指向多个服务，需人工确认",
  SERVICE_ARCHIVED: "所引用服务已归档",
} as const;

type IncidentDetailTab = "OVERVIEW" | "ALERTS" | "EVIDENCE" | "AI" | "ACTIONS" | "TIMELINE";

const DETAIL_TABS: { id: IncidentDetailTab; label: string }[] = [
  { id: "OVERVIEW", label: "概览" },
  { id: "ALERTS", label: "原始告警" },
  { id: "EVIDENCE", label: "证据" },
  { id: "AI", label: "AI 调查" },
  { id: "ACTIONS", label: "处置" },
  { id: "TIMELINE", label: "时间线" },
];

function duration(seconds: number): string {
  const absolute = Math.abs(seconds);
  if (absolute < 60) return `${absolute}s`;
  const minutes = Math.floor(absolute / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}

function slaText(item: OperationalOccurrence): string {
  if (item.ack_sla_remaining_seconds === null) return SLA_LABELS[item.ack_sla_state];
  if (item.ack_sla_state === "BREACHED") {
    return `已超时 ${duration(item.ack_sla_remaining_seconds)}`;
  }
  if (item.ack_sla_state === "AT_RISK") {
    return `临近超时 · 剩余 ${duration(item.ack_sla_remaining_seconds)}`;
  }
  return `剩余 ${duration(item.ack_sla_remaining_seconds)}`;
}

function toggle<T extends string>(values: readonly T[], value: T): T[] {
  return values.includes(value)
    ? values.filter((item) => item !== value)
    : [...values, value];
}

function QueueCard({
  item,
  selected,
  onSelect,
  batchSelected,
  onBatchSelect,
}: {
  item: OperationalOccurrence;
  selected: boolean;
  onSelect: () => void;
  batchSelected: boolean;
  onBatchSelect: () => void;
}) {
  return (
    <div className="incident-queue-card-wrap">
      <label className="incident-batch-check">
        <input
          aria-label={`批量选择 ${item.title}`}
          checked={batchSelected}
          disabled={item.response_state !== "UNACKNOWLEDGED"}
          onChange={onBatchSelect}
          type="checkbox"
        />
      </label>
      <button
        aria-current={selected ? "true" : undefined}
        className={`incident-queue-card incident-queue-card--${severityTone(item.signal_severity)}${
          selected ? " incident-queue-card--active" : ""
        }`}
        onClick={onSelect}
        type="button"
      >
        <div className="incident-queue-card__lead">
          <span className={`severity-mark severity-mark--${severityTone(item.signal_severity)}`}>
            {severityLabel(item.signal_severity)}
          </span>
          <strong>{item.title}</strong>
          <span className={`sla-state sla-state--${item.ack_sla_state.toLowerCase()}`}>
            {slaText(item)}
          </span>
        </div>
        <div className="incident-queue-card__facts">
          <span>{queueSourceText(item.source_name)}</span>
          <span>{SIGNAL_LABELS[item.signal_state]}</span>
          <span>{RESPONSE_LABELS[item.response_state]}</span>
          <span>{item.service_name ?? "未映射服务"}</span>
          <span>{item.member_count} 条告警</span>
          <span>活动于 {ageFrom(item.latest_activity_at)} 前</span>
        </div>
        {item.noise_state !== "NONE" ? (
          <div className="incident-noise-inline">
            <strong>{item.noise_state === "STORM" ? "风暴中" : item.noise_state === "FLAPPING" ? "反复触发" : NOISE_LABELS[item.noise_state]}</strong>
            <span>{item.noise_state === "STORM" ? "平台消息已汇总" : item.noise_state === "FLAPPING" ? "平台消息已合并" : `${item.noise_reason ?? "平台通知正在按确定性规则聚合"} · ${noiseWindow(item)}`}</span>
          </div>
        ) : null}
      </button>
    </div>
  );
}

function TaskCard({
  occurrenceId,
  task,
  readOnly = false,
}: {
  occurrenceId: number;
  task: IncidentTask;
  readOnly?: boolean;
}) {
  const transition = useTransitionOccurrenceTask(occurrenceId);
  const [terminalText, setTerminalText] = useState("");
  const text = terminalText.trim();
  return (
    <article className={`incident-task incident-task--${task.status.toLowerCase()}`}>
      <header>
        <div><strong>{task.title}</strong><small>{TASK_STATUS_LABELS[task.status]}</small></div>
        {task.due_at ? <span>期望完成 {new Date(task.due_at).toLocaleString()}</span> : <span>未设置期望完成时间</span>}
      </header>
      {task.description ? <p>{task.description}</p> : null}
      {task.runbook_link ? (
        <a href={task.runbook_link} rel="noreferrer" target="_blank">
          打开操作手册（平台不会访问）↗
        </a>
      ) : null}
      {task.result ? <div className="incident-task__result"><strong>核验结果</strong><p>{task.result}</p></div> : null}
      {!readOnly && (task.status === "TODO" || task.status === "IN_PROGRESS") ? (
        <div className="incident-task__actions">
          {task.status === "TODO" ? (
            <button
              className="secondary-btn"
              disabled={transition.isPending}
              onClick={() => transition.mutate({
                taskId: task.id,
                expectedVersion: task.version,
                target: "IN_PROGRESS",
                result: null,
                reason: null,
              })}
              type="button"
            >标记为进行中</button>
          ) : null}
          <input
            maxLength={4000}
            onChange={(event) => setTerminalText(event.target.value)}
            placeholder="完成时填写核验结果；取消时填写判断理由"
            value={terminalText}
          />
          <button
            className="secondary-btn"
            disabled={transition.isPending || text.length === 0}
            onClick={() => transition.mutate({
              taskId: task.id,
              expectedVersion: task.version,
              target: "DONE",
              result: text,
              reason: null,
            })}
            type="button"
          >完成并记录结果</button>
          <button
            className="secondary-btn"
            disabled={transition.isPending || text.length === 0}
            onClick={() => transition.mutate({
              taskId: task.id,
              expectedVersion: task.version,
              target: "CANCELED",
              result: null,
              reason: text,
            })}
            type="button"
          >取消并记录理由</button>
          {transition.error instanceof Error ? <small className="error" role="alert">任务未更新：{transition.error.message}</small> : null}
        </div>
      ) : null}
    </article>
  );
}

function CollaborationActions({
  occurrenceId,
  readOnly = false,
}: {
  occurrenceId: number;
  readOnly?: boolean;
}) {
  const tasks = useOccurrenceTasks(occurrenceId);
  const createTask = useCreateOccurrenceTask(occurrenceId);
  const addNote = useAddOccurrenceNote(occurrenceId);
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [dueAt, setDueAt] = useState("");
  const [runbookLink, setRunbookLink] = useState("");
  const [note, setNote] = useState("");

  return (
    <>
      <section className="incident-overview__section incident-collaboration" aria-label="人工处置任务">
        <div className="response-actions__heading">
          <div><span className="eyebrow">执行 · 核验</span><h2>人工处置任务</h2></div>
          <small>{readOnly ? "本次处理已结束，只读" : "仅用于记录和审计；平台不会执行命令或访问链接"}</small>
        </div>
        {!readOnly ? <div className="incident-task-create">
          <label><span>要完成什么</span><input maxLength={200} onChange={(event) => setTitle(event.target.value)} placeholder="例如：核对本次告警的影响范围" value={title} /></label>
          <label><span>检查范围与完成标准（选填）</span><textarea maxLength={4000} onChange={(event) => setDescription(event.target.value)} placeholder="例如：检查近 30 分钟的错误率，确认已回落并保持稳定" rows={2} value={description} /></label>
          <label><span>期望完成时间（选填）</span><input onChange={(event) => setDueAt(event.target.value)} type="datetime-local" value={dueAt} /></label>
          <label><span>操作手册链接（选填，仅支持 HTTPS）</span><input maxLength={2048} onChange={(event) => setRunbookLink(event.target.value)} placeholder="https://…" type="url" value={runbookLink} /></label>
          <button
            className="secondary-btn"
            disabled={createTask.isPending || title.trim().length === 0 || (runbookLink.trim().length > 0 && !runbookLink.trim().startsWith("https://"))}
            onClick={() => createTask.mutate({
              title: title.trim(),
              description: description.trim() || null,
              due_at: dueAt ? new Date(dueAt).toISOString() : null,
              runbook_link: runbookLink.trim() || null,
            }, {
              onSuccess: () => {
                setTitle(""); setDescription(""); setDueAt(""); setRunbookLink("");
              },
            })}
            type="button"
          >添加任务</button>
          {createTask.error instanceof Error ? <small className="error" role="alert">任务未添加：{createTask.error.message}</small> : null}
        </div> : null}
        {tasks.isError ? <div className="error">人工处置任务暂时无法读取；当前处理状态不受影响。</div> : null}
        {!tasks.isPending && (tasks.data?.length ?? 0) === 0 ? <div className="incident-readonly-note">当前没有人工处置任务。</div> : null}
        <div className="incident-task-list">
          {tasks.data?.map((task) => <TaskCard key={task.id} occurrenceId={occurrenceId} readOnly={readOnly} task={task} />)}
        </div>
      </section>

      {!readOnly ? <section className="incident-overview__section incident-note-create" aria-label="处置记录">
        <div className="response-actions__heading">
          <div><span className="eyebrow">审计</span><h2>处置记录</h2></div>
          <small>提交后保留历史，不覆盖旧记录 · 最多 4,000 字</small>
        </div>
        <textarea
          maxLength={4000}
          onChange={(event) => setNote(event.target.value)}
          placeholder="记录人工判断、核验结果或更正；如误填敏感内容，可在时间线中追加脱敏记录。"
          rows={4}
          value={note}
        />
        <button
          className="secondary-btn"
          disabled={addNote.isPending || note.trim().length === 0}
          onClick={() => addNote.mutate(note.trim(), { onSuccess: () => setNote("") })}
          type="button"
        >添加处置记录</button>
        {addNote.error instanceof Error ? <small className="error" role="alert">处置记录未添加：{addNote.error.message}</small> : null}
      </section> : null}
    </>
  );
}

function OccurrenceOverview({
  item,
  refreshDegraded = false,
}: {
  item: OperationalOccurrence | null;
  refreshDegraded?: boolean;
}) {
  const navigate = useNavigate();
  const location = useLocation();
  const occurrenceId = item?.id ?? null;
  const timeline = useOccurrenceTimeline(occurrenceId);
  const tasks = useOccurrenceTasks(occurrenceId);
  const similarHistory = useSimilarOccurrenceHistory(occurrenceId);
  const incidentDetail = useIncident(item?.incident_id ?? null);
  const redactNote = useRedactOccurrenceNote(occurrenceId);
  const startHandling = useStartHandlingOccurrence(occurrenceId);
  const resolve = useResolveOccurrence(occurrenceId);
  const services = useServices(false);
  const assignService = useAssignOccurrenceService(occurrenceId);
  const occurrenceNoise = useOccurrenceNoise(occurrenceId);
  const createSuppression = useCreateOccurrenceSuppression(occurrenceId);
  const endSuppression = useEndOccurrenceSuppression(occurrenceId);
  const [startReason, setStartReason] = useState("");
  const [resolutionReason, setResolutionReason] = useState("");
  const [resolutionCode, setResolutionCode] = useState<ResolutionCode>("FIXED");
  const [duplicateOf, setDuplicateOf] = useState("");
  const [actionNotice, setActionNotice] = useState("");
  const [redactionSequence, setRedactionSequence] = useState<number | null>(null);
  const [redactionReason, setRedactionReason] = useState("");
  const [serviceId, setServiceId] = useState(0);
  const [serviceNotice, setServiceNotice] = useState("");
  const [suppressionDuration, setSuppressionDuration] = useState<900 | 3600 | 14400 | 86400>(900);
  const [suppressionReason, setSuppressionReason] = useState("");
  const [suppressionNotice, setSuppressionNotice] = useState("");

  useEffect(() => {
    setStartReason("");
    setResolutionReason("");
    setResolutionCode(item?.signal_state === "RECOVERED" ? "FIXED" : "FALSE_POSITIVE");
    setDuplicateOf("");
    setActionNotice("");
    setRedactionSequence(null);
    setRedactionReason("");
    setServiceId(item?.service_id ?? 0);
    setServiceNotice("");
    setSuppressionDuration(900);
    setSuppressionReason("");
    setSuppressionNotice("");
  }, [item?.id]);

  useEffect(() => {
    if (item && !availableResolutionCodes(item.signal_state).includes(resolutionCode)) {
      setResolutionCode(item.signal_state === "RECOVERED" ? "FIXED" : "FALSE_POSITIVE");
    }
  }, [item?.signal_state, resolutionCode]);

  if (item === null) {
    return (
      <section className="incident-overview incident-overview--empty">
        <p>从队列选择一次事件，核对影响范围、信号新鲜度和确认时限。</p>
      </section>
    );
  }
  const pending = startHandling.isPending || resolve.isPending;
  const commandError = startHandling.error ?? resolve.error;
  const normalizedStartReason = startReason.trim() || null;
  const normalizedResolutionReason = resolutionReason.trim() || null;
  const resolutionOptions = availableResolutionCodes(item.signal_state);
  const unfinishedTaskCount = tasks.data?.filter(
    (task) => task.status === "TODO" || task.status === "IN_PROGRESS",
  ).length ?? 0;
  const taskGateUnverified = tasks.isPending || tasks.isError;
  const suppressionVersion = occurrenceNoise.data?.suppression_version ?? null;
  const activeTab = DETAIL_TABS.find(
    (tab) => location.hash === `#${tab.id.toLowerCase()}`,
  )?.id ?? "OVERVIEW";
  const currentMembers = item.response_state !== "RESOLVED"
    && incidentDetail.data?.id === item.incident_id
    && incidentDetail.data?.occurrence_no === item.occurrence_no;
  const beginHandling = () => startHandling.mutate(
    { expectedVersion: item.version, reason: normalizedStartReason },
    { onSuccess: () => setActionNotice("已开始处理，确认时限已停止。接下来可添加人工处置任务或处置记录。") },
  );
  return (
    <section className="incident-overview" aria-label="Incident Overview">
      <header className="incident-overview__header">
        <div>
          <span className="eyebrow">事件 #{item.id} · 第 {item.occurrence_no} 次发生</span>
          <h1>{item.title}</h1>
        </div>
        <span className={`severity-mark severity-mark--${severityTone(item.signal_severity)}`}>
          {severityLabel(item.signal_severity)}
        </span>
      </header>

      {activeTab !== "ACTIONS" && item.response_state !== "RESOLVED" ? <div className="response-actions incident-primary-action">
        {item.response_state === "UNACKNOWLEDGED" ? <>
          <button className="primary-btn" disabled={pending} onClick={beginHandling} type="button">确认并开始处理</button>
          <small>停止确认时限和重复提醒；升级与恢复通知仍保留。</small>
          <button className="text-btn" onClick={() => navigate(`${location.pathname}${location.search}#actions`)} type="button">先填写处理说明</button>
        </> : <button className="primary-btn" onClick={() => navigate(`${location.pathname}${location.search}#actions`)} type="button">继续处置与核验</button>}
        {commandError instanceof Error ? <div className="error" role="alert">开始处理未确认：{commandError.message}。请核对当前状态。</div> : null}
        {actionNotice ? <small role="status">{actionNotice}</small> : null}
      </div> : null}

      {refreshDegraded ? (
        <div className="incident-evidence-warning" role="status">
          最新刷新暂时未完成；当前继续显示上次成功读取的事件信息。
        </div>
      ) : null}

      <div className="incident-state-pair">
        <div>
          <span>信号状态</span>
          <strong>{SIGNAL_LABELS[item.signal_state]}</strong>
          <small>严重度：{severityLabel(item.signal_severity)}</small>
        </div>
        <div>
          <span>人工处理</span>
          <strong>{RESPONSE_LABELS[item.response_state]}</strong>
          <small>{item.response_state === "UNACKNOWLEDGED" ? "尚未有人开始处理" : item.response_state === "IN_PROGRESS" ? "正在记录处置与核验" : "本次处理已结束"}</small>
        </div>
      </div>

      {item.evidence_completeness !== "COMPLETE" ? (
        <div className="incident-evidence-warning" role="status">
          本次范围证据为“{EVIDENCE_LABELS[item.evidence_completeness]}”；未取得的成员不能视为已恢复。请先核对来源状态，再推进人工处理。
        </div>
      ) : null}

      {item.noise_state !== "NONE" ? (
        <div className="incident-noise-status" role="status">
          <div><strong>{NOISE_LABELS[item.noise_state]}</strong><span>{noiseWindow(item)}</span></div>
          <p>{item.noise_reason ?? "平台协作通知正在按确定性规则合并。"}</p>
          <small>作用范围：{item.noise_scope ?? "当前事件"}。事件排序、原始告警和权威 Alertmanager 通知链路均不受影响。</small>
        </div>
      ) : null}

      <nav className="incident-detail-tabs" aria-label="Incident Detail">
        {DETAIL_TABS.map((tab) => (
          <button
            aria-current={activeTab === tab.id ? "page" : undefined}
            className={activeTab === tab.id ? "incident-detail-tabs__active" : ""}
            key={tab.id}
            onClick={() => navigate(`${location.pathname}${location.search}#${tab.id.toLowerCase()}`)}
            type="button"
          >{tab.label}</button>
        ))}
      </nav>

      {activeTab === "ACTIONS" && actionNotice ? (
        <div className="response-command-result" role="status">{actionNotice}</div>
      ) : null}

      {activeTab === "OVERVIEW" ? <div aria-label="Overview" className="incident-overview__section" role="region">
        <h2>事件概览</h2>
        <dl className="incident-overview-grid">
          <div><dt>来源</dt><dd>{item.source_name}</dd></div>
          <div><dt>服务</dt><dd>{item.service_name ?? "未映射服务 · 使用默认 15 分钟确认时限"}</dd></div>
          <div><dt>范围</dt><dd>{item.member_count} 条告警</dd></div>
          <div><dt>证据完整度</dt><dd>{EVIDENCE_LABELS[item.evidence_completeness]}</dd></div>
          <div><dt>确认时限</dt><dd>{slaText(item)}</dd></div>
          <div><dt>首次可信发现</dt><dd>{item.detected_at ? new Date(item.detected_at).toLocaleString() : "尚未开始计时"}</dd></div>
          <div><dt>上游开始时间</dt><dd>{item.source_started_at ? new Date(item.source_started_at).toLocaleString() : "未提供"}</dd></div>
          <div><dt>最新活动</dt><dd>{new Date(item.latest_activity_at).toLocaleString()}</dd></div>
        </dl>
        <div className="occurrence-service-assignment">
          <div>
            <strong>{SERVICE_ASSIGNMENT_LABELS[item.service_assignment_state]}</strong>
            <small>
              {item.assignment_origin === "MANUAL"
                ? "当前归属由操作者明确指定；后续规则重算不会覆盖。"
                : "可选择标准建议，也可人工改为其他有效服务。人工修改只影响本次事件。"}
            </small>
          </div>
          {item.response_state !== "RESOLVED" ? (
            <div className="occurrence-service-assignment__control">
              <label>
                <span>调整本次事件归属</span>
                <select onChange={(event) => setServiceId(Number(event.target.value))} value={serviceId}>
                  <option value={0}>请选择有效服务</option>
                  {services.data?.map((service) => <option key={service.id} value={service.id}>{service.name} · {SERVICE_CRITICALITY_LABELS[service.criticality]}</option>)}
                </select>
              </label>
              <button
                className="secondary-btn"
                disabled={serviceId < 1 || serviceId === item.service_id || assignService.isPending}
                onClick={() => assignService.mutate(
                  { serviceId, expectedVersion: item.version },
                  { onSuccess: (result) => setServiceNotice(`已将本次事件归属到 ${result.service_name}；原确认时限保持不变。`) },
                )}
                type="button"
              >保存本次归属</button>
            </div>
          ) : null}
          {services.isError ? <small className="error">有效服务列表暂时无法读取；事件其他功能仍可使用。</small> : null}
          {assignService.error instanceof Error ? <small className="error">服务归属未更新：{assignService.error.message}</small> : null}
          {serviceNotice ? <small role="status">{serviceNotice}</small> : null}
        </div>
        <details className="similar-history" open>
          <summary>过去类似事件</summary>
          <p>只比较同一告警来源中已经结束的事件；按服务、主告警、聚合规则和关键标签固定打分。</p>
          {similarHistory.isPending ? (
            <div className="incident-readonly-note">正在读取已结束事件…</div>
          ) : similarHistory.isError ? (
            <div className="error">过去类似事件暂时无法读取；当前事件和人工处置不受影响。</div>
          ) : (similarHistory.data?.length ?? 0) === 0 ? (
            <div className="incident-readonly-note">没有已结束事件达到相似条件（最低 50 分）。这表示正常空结果，不代表历史能力不可用。</div>
          ) : (
            <div className="similar-history__list">
              {similarHistory.data?.map((history) => (
                <article key={history.occurrence_id}>
                  <header>
                    <div><strong>{history.title}</strong><small>事件 #{history.occurrence_id} · 第 {history.occurrence_no} 次发生</small></div>
                    <span>{history.score} 分</span>
                  </header>
                  <ul>
                    {history.match_reasons.map((reason) => (
                      <li key={`${reason.kind}-${reason.field}-${reason.value}`}>
                        {reason.kind === "SERVICE" ? "同一服务" : reason.kind === "PRIMARY_ALERTNAME" ? "同一主告警" : reason.kind === "AGGREGATION_RULE" ? "同一聚合规则" : `关键标签相同：${reason.field}=${reason.value}`} · +{reason.points}
                      </li>
                    ))}
                  </ul>
                  <dl>
                    <div><dt>结束分类</dt><dd>{RESOLUTION_LABELS[history.resolution_code]}</dd></div>
                    <div><dt>处置用时</dt><dd>{duration(history.handling_duration_seconds)}</dd></div>
                    <div><dt>人工结论</dt><dd>{history.operator_conclusion ?? "未记录判断说明"}</dd></div>
                    <div><dt>任务结果</dt><dd>{history.task_outcome ?? "未记录任务结果"}</dd></div>
                  </dl>
                  <small>结束于 {new Date(history.resolved_at).toLocaleString()}</small>
                </article>
              ))}
            </div>
          )}
        </details>
        <details>
          <summary>确定性分组范围</summary>
          <code>{item.group_key}</code>
        </details>
      </div> : null}

      {activeTab === "ALERTS" ? (
        <section className="incident-overview__section" aria-label="Occurrence Alerts">
          <div className="response-actions__heading">
            <div><span className="eyebrow">确认范围</span><h2>原始告警</h2></div>
            <small>{item.member_count} 条成员</small>
          </div>
          {incidentDetail.isError ? <div className="error">原始告警暂时无法读取；事件状态和时间线仍可使用。</div> : null}
          {currentMembers ? <>
          <p className="incident-readonly-note">当前发生周期的最新观测，不是调查时的冻结快照。</p>
          <div className="incident-alert-list">
            {incidentDetail.data?.members.map((member) => (
              <article key={member.id}>
                <header><strong>{member.alertname}</strong><span>{member.severity}</span></header>
                <p>{member.annotations.summary ?? member.annotations.description ?? "上游未提供摘要"}</p>
                <small>{member.cluster || "未标注 cluster"} · {member.source_state} · 最近观测 {new Date(member.last_seen_at).toLocaleString()}</small>
              </article>
            ))}
          </div>
          </> : incidentDetail.isPending && item.response_state !== "RESOLVED" ? <p>正在核对当前发生周期…</p> : <>
            <p className="incident-readonly-note">当前成员不能作为本次事件的历史成员；以下只回看本次调查保存的快照。</p>
            <InvestigatorPanel occurrence={item} mode="ALERTS" />
          </>}
        </section>
      ) : null}

      {activeTab === "EVIDENCE" ? (
        <section className="incident-overview__section" aria-label="Occurrence Evidence">
          <span className="eyebrow">观察</span><h2>证据</h2>
          {currentMembers ? <>
            <p className="incident-readonly-note">当前告警的确定性指标；只读查询指标源，不调用模型。下方调查证据为单独保存的冻结快照。</p>
            <MetricEvidence members={incidentDetail.data?.members ?? []} />
          </> : <p className="incident-readonly-note">{incidentDetail.isPending && item.response_state !== "RESOLVED" ? "正在核对当前发生周期…" : "无法确认是当前未结束周期，不重新查询指标冒充历史；只展示已保存的调查证据。"}</p>}
          {incidentDetail.isError && item.response_state !== "RESOLVED" ? <p className="error">当前成员暂时无法读取；已保存的调查证据仍可回看。</p> : null}
          <InvestigatorPanel occurrence={item} mode="EVIDENCE" />
        </section>
      ) : null}

      {activeTab === "AI" ? <InvestigatorPanel occurrence={item} /> : null}

      {activeTab === "ACTIONS" && item.response_state === "UNACKNOWLEDGED" ? (
        <section className="incident-overview__section response-actions" aria-label="开始人工处理">
          <div className="response-actions__heading">
            <div><span className="eyebrow">开始处理</span><h2>确认并开始处理</h2></div>
            <small>开始后停止确认时限与重复提醒；升级和恢复通知仍保留</small>
          </div>
          <label className="response-reason">
            <span>处理说明（选填）</span>
            <textarea
              maxLength={2000}
              onChange={(event) => setStartReason(event.target.value)}
              placeholder="例如：已联系值班同事，先核对影响范围"
              rows={3}
              value={startReason}
            />
          </label>
          <button
            className="primary-btn"
            disabled={pending}
            onClick={beginHandling}
            type="button"
          >确认并开始处理</button>
          {commandError instanceof Error ? (
            <div className="response-command-result response-command-result--error" role="alert">
              未能开始处理：{commandError.message}。当前状态未改变；如有人刚刚更新了本事件，请刷新后再核对。
            </div>
          ) : null}
        </section>
      ) : null}

      {activeTab === "ACTIONS" && item.response_state === "IN_PROGRESS" ? (
        <>
          <CollaborationActions key={item.id} occurrenceId={item.id} />
          <section className="incident-overview__section response-actions" aria-label="结束本次处理">
            <div className="response-actions__heading">
              <div><span className="eyebrow">核验 · 结束</span><h2>结束本次处理</h2></div>
              <small>{resolutionHelp(item.signal_state)}</small>
            </div>
            <div className={`response-command-card response-resolve${item.signal_state === "RECOVERED" ? "" : " response-resolve--warning"}`}>
              <div>
                <strong>确认已完成核验</strong>
                <small>结束后，当前平台不再发送本次事件的升级、提醒或恢复通知。</small>
              </div>
              <div className="response-resolve__fields">
                {taskGateUnverified ? (
                  <div className="incident-readonly-note">正在核对人工处置任务；核对完成前不能结束本次处理。</div>
                ) : unfinishedTaskCount > 0 ? (
                  <div className="incident-readonly-note">
                    还有 {unfinishedTaskCount} 项任务未完成。请先完成，或填写理由取消任务。
                  </div>
                ) : null}
                <label>
                  <span>结束分类</span>
                  <select onChange={(event) => setResolutionCode(event.target.value as ResolutionCode)} value={resolutionCode}>
                    {resolutionOptions.map((code) => <option key={code} value={code}>{RESOLUTION_LABELS[code]}</option>)}
                  </select>
                </label>
                {resolutionCode === "DUPLICATE" ? (
                  <label>
                    <span>归并到的事件编号</span>
                    <input inputMode="numeric" min={1} onChange={(event) => setDuplicateOf(event.target.value)} type="number" value={duplicateOf} />
                  </label>
                ) : null}
                <label className="response-reason">
                  <span>结束说明{resolutionReasonRequired(item.signal_state) ? "（必填）" : "（选填）"}</span>
                  <textarea
                    maxLength={2000}
                    onChange={(event) => setResolutionReason(event.target.value)}
                    placeholder="说明核验结果，以及为什么可以结束本次处理"
                    rows={3}
                    value={resolutionReason}
                  />
                </label>
                <button
                  className="danger-primary-btn"
                  disabled={pending || taskGateUnverified || unfinishedTaskCount > 0
                    || (resolutionReasonRequired(item.signal_state) && normalizedResolutionReason === null)
                    || (resolutionCode === "DUPLICATE" && !/^\d+$/.test(duplicateOf))}
                  onClick={() => resolve.mutate(
                    {
                      expectedVersion: item.version,
                      resolutionCode,
                      duplicateOfOccurrenceId: resolutionCode === "DUPLICATE" ? Number(duplicateOf) : null,
                      reason: normalizedResolutionReason,
                    },
                    { onSuccess: () => setActionNotice(`本次处理已结束，分类为“${RESOLUTION_LABELS[resolutionCode]}”；当前平台通知生命周期已停止。`) },
                  )}
                  type="button"
                >结束本次处理</button>
              </div>
            </div>
            {commandError instanceof Error ? (
              <div className="response-command-result response-command-result--error" role="alert">
                未能结束本次处理：{commandError.message}。当前状态未改变，请核对信号状态和结束说明。
              </div>
            ) : null}
          </section>
        </>
      ) : activeTab === "ACTIONS" && item.response_state === "RESOLVED" ? (
        <>
          <div className="incident-readonly-note">
            本次处理已结束（{item.resolution_code ? RESOLUTION_LABELS[item.resolution_code] : "未记录分类"}）。
            任务与处置记录保持只读；再次收到可信的持续触发信号时会建立新的一次事件。
            {item.duplicate_of_occurrence_id ? ` 已归并到事件 #${item.duplicate_of_occurrence_id}。` : ""}
          </div>
          <CollaborationActions key={item.id} occurrenceId={item.id} readOnly />
        </>
      ) : null}

      {activeTab === "ACTIONS" && item.response_state !== "RESOLVED" ? (
        <details className="incident-overview__section response-actions occurrence-suppression" aria-label="暂停本次事件平台通知">
          <summary>暂停或恢复本次事件的平台通知</summary>
          <div className="response-actions__heading">
            <div><span className="eyebrow">通知控制</span><h2>暂停本次事件的平台通知</h2></div>
            <small>事件仍保留在队列；原始告警和 Alertmanager 通知不受影响</small>
          </div>
          {occurrenceNoise.isError ? <div className="error">当前通知降噪状态暂时无法核对，因此没有开放变更按钮。</div> : null}
          {occurrenceNoise.data?.state === "SUPPRESSED" ? (
            <div className="response-command-card">
              <div><strong>已暂停后续平台协作消息</strong><small>{occurrenceNoise.data.reason} · 截止 {occurrenceNoise.data.ends_at ? new Date(occurrenceNoise.data.ends_at).toLocaleString() : "未提供"}</small></div>
              <button
                className="secondary-btn"
                disabled={endSuppression.isPending || suppressionVersion === null}
                onClick={() => suppressionVersion !== null && endSuppression.mutate(
                  suppressionVersion,
                  { onSuccess: () => setSuppressionNotice("已恢复本次事件未来的平台协作消息；暂停期间跳过的消息不会补发。") },
                )}
                type="button"
              >提前恢复平台通知</button>
            </div>
          ) : occurrenceNoise.data ? (
            <div className="response-command-card occurrence-suppression__form">
              <label><span>暂停时长</span><select onChange={(event) => setSuppressionDuration(Number(event.target.value) as 900 | 3600 | 14400 | 86400)} value={suppressionDuration}><option value={900}>15 分钟</option><option value={3600}>1 小时</option><option value={14400}>4 小时</option><option value={86400}>24 小时</option></select></label>
              <label className="response-reason"><span>暂停原因（必填）</span><textarea maxLength={1000} onChange={(event) => setSuppressionReason(event.target.value)} placeholder="例如：相同事件已在发布群持续同步，暂时合并本平台重复消息" rows={3} value={suppressionReason} /></label>
              <button
                className="secondary-btn"
                disabled={createSuppression.isPending || suppressionReason.trim().length === 0}
                onClick={() => createSuppression.mutate(
                  { durationSeconds: suppressionDuration, reason: suppressionReason.trim() },
                  { onSuccess: () => { setSuppressionReason(""); setSuppressionNotice("已暂停本次事件后续的平台协作消息。到期或提前结束后，不会补发暂停期间的消息。"); } },
                )}
                type="button"
              >暂停平台通知</button>
            </div>
          ) : <div className="incident-readonly-note">正在核对当前通知降噪状态…</div>}
          {suppressionNotice ? <small role="status">{suppressionNotice}</small> : null}
          {(createSuppression.error ?? endSuppression.error) instanceof Error ? <small className="error" role="alert">通知状态未改变：{String((createSuppression.error ?? endSuppression.error)?.message)}</small> : null}
        </details>
      ) : null}

      {activeTab === "TIMELINE" ? <section className="incident-overview__section incident-timeline" aria-label="Incident Timeline">
        <div className="response-actions__heading">
          <div><span className="eyebrow">审计</span><h2>时间线</h2></div>
          <small>只追加记录 · 顺序号稳定</small>
        </div>
        {timeline.isError ? (
          <div className="response-command-result response-command-result--error">时间线暂时无法读取。当前状态和已提交内容不会因此改变；请稍后刷新，必要时用页面请求编号核对本地日志。</div>
        ) : null}
        {!timeline.isPending && (timeline.data?.length ?? 0) === 0 ? (
          <div className="incident-readonly-note">当前还没有人工处理记录；信号变化不会被写成人工处置。</div>
        ) : null}
        <ol>
          {timeline.data?.map((entry) => (
            <li key={entry.id}>
              <span>#{entry.sequence}</span>
              <div>
                <strong>{entry.summary}</strong>
                <small>{new Date(entry.created_at).toLocaleString()} · {ACTOR_LABELS[entry.actor_type]}</small>
                {entry.event_type === "MANUAL_NOTE" ? (
                  <>
                    <p>{String(entry.detail.text)}</p>
                    {entry.detail.redacted !== true ? (
                      redactionSequence === entry.sequence ? (
                        <div className="timeline-redaction">
                          <input
                            maxLength={2000}
                            onChange={(event) => setRedactionReason(event.target.value)}
                            placeholder="说明脱敏原因"
                            value={redactionReason}
                          />
                          <button
                            className="secondary-btn"
                            disabled={redactNote.isPending || redactionReason.trim().length === 0}
                            onClick={() => redactNote.mutate({
                              sequence: entry.sequence,
                              reason: redactionReason.trim(),
                            }, {
                              onSuccess: () => { setRedactionSequence(null); setRedactionReason(""); },
                            })}
                            type="button"
                          >追加脱敏审计</button>
                        </div>
                      ) : (
                        <button className="text-btn" onClick={() => setRedactionSequence(entry.sequence)} type="button">脱敏此记录</button>
                      )
                    ) : <small>原文已隔离加密，并将在脱敏后 7 天内清除。</small>}
                  </>
                ) : (
                  <p>
                    {entry.detail.before_response_state && entry.detail.after_response_state
                      ? `${responseHistoryLabel(entry.detail.before_response_state)} → ${responseHistoryLabel(entry.detail.after_response_state)}`
                      : entry.detail.before_status && entry.detail.after_status
                        ? `${taskHistoryLabel(entry.detail.before_status)} → ${taskHistoryLabel(entry.detail.after_status)}`
                        : entry.detail.title ? String(entry.detail.title) : "已记录结构化审计事实"}
                    {entry.detail.reason ? ` · ${String(entry.detail.reason)}` : ""}
                    {entry.detail.result ? ` · ${String(entry.detail.result)}` : ""}
                  </p>
                )}
              </div>
            </li>
          ))}
        </ol>
      </section> : null}
    </section>
  );
}

export function IncidentsPage() {
  const navigate = useNavigate();
  const params = useParams();
  const [searchParams] = useSearchParams();
  const search = searchParams.toString();
  const selection = useMemo(() => parseQueueSelection(search), [search]);
  const requestedId = /^\d+$/.test(params.occurrenceId ?? "")
    ? Number(params.occurrenceId)
    : null;
  const queue = useOperationalOccurrences({
    view: selection.view,
    sourceIds: selection.sourceIds,
    signalStates: selection.signalStates,
    cursor: selection.cursor,
  });
  const sources = useEventSources();
  const items = useMemo(() => queue.data?.items ?? [], [queue.data?.items]);
  const queueReadState = backgroundReadState({
    hasData: queue.data !== undefined,
    isError: queue.isError,
    isPending: queue.isPending,
  });
  const sourcesReadState = backgroundReadState({
    hasData: sources.data !== undefined,
    isError: sources.isError,
    isPending: sources.isPending,
  });
  const selectedId = requestedId ?? items[0]?.id ?? null;
  const overview = useOperationalOccurrence(selectedId);
  const overviewState = selectedId === null
    ? "EMPTY"
    : occurrenceDetailState({
        hasData: overview.data !== undefined,
        isError: overview.isError,
        isPending: overview.isPending,
        errorStatus: overview.error instanceof ApiError ? overview.error.status : null,
      });
  const [batchSelection, setBatchSelection] = useState<Set<number>>(new Set());
  const [batchReason, setBatchReason] = useState("");
  const batchStartHandling = useBatchStartHandlingOccurrences();
  const stormSources = useMemo(() => {
    const grouped = new Map<string, {
      sourceName: string;
      reason: string;
      startsAt: string | null;
      visibleOccurrences: number;
    }>();
    for (const item of items) {
      if (item.noise_state !== "STORM") continue;
      const existing = grouped.get(item.source_id);
      if (existing) {
        existing.visibleOccurrences += 1;
      } else {
        grouped.set(item.source_id, {
          sourceName: item.source_name,
          reason: item.noise_reason ?? "最近 5 分钟达到告警风暴阈值",
          startsAt: item.noise_starts_at,
          visibleOccurrences: 1,
        });
      }
    }
    return [...grouped.entries()].map(([sourceId, value]) => ({ sourceId, ...value }));
  }, [items]);

  useEffect(() => {
    if (requestedId === null && selectedId !== null) {
      navigate(`/incidents/${selectedId}${search ? `?${search}` : ""}`, { replace: true });
    }
  }, [navigate, requestedId, search, selectedId]);

  useEffect(() => {
    const visible = new Set(items.filter((item) => item.response_state === "UNACKNOWLEDGED").map((item) => item.id));
    setBatchSelection((current) => new Set([...current].filter((id) => visible.has(id))));
  }, [items]);

  const patch = (next: URLSearchParams) => {
    const nextSearch = next.toString();
    navigate(`/incidents${nextSearch ? `?${nextSearch}` : ""}`, { replace: true });
  };
  const select = (id: number) => navigate(`/incidents/${id}${search ? `?${search}` : ""}`);
  const toggleBatch = (id: number) => setBatchSelection((current) => {
    const next = new Set(current);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    return next;
  });
  const submitBatch = () => {
    const selected = items
      .filter((item) => batchSelection.has(item.id))
      .map((item) => ({ occurrence_id: item.id, expected_version: item.version }));
    batchStartHandling.mutate(
      { items: selected, reason: batchReason.trim() || null },
      {
        onSuccess: (result) => {
          const failed = new Set(result.items.filter((item) => !item.ok).map((item) => item.occurrence_id));
          setBatchSelection(failed);
        },
      },
    );
  };

  return (
    <div className="incidents-page">
      <section className="incident-queue-pane" aria-label="Incident Queue">
        <header className="incident-queue-head">
          <div>
            <span className="eyebrow">Observe · Qualify</span>
            <h1>事件队列</h1>
          </div>
          <span>{items.length} 条</span>
        </header>

        <div className="queue-view-tabs" aria-label="系统视图">
          {QUEUE_VIEWS.map((view) => (
            <button
              aria-pressed={selection.view === view}
              className={selection.view === view ? "queue-view-tabs__active" : ""}
              key={view}
              onClick={() => patch(withQueueSelection(search, { view }))}
              type="button"
            >
              {VIEW_LABELS[view]}
            </button>
          ))}
        </div>

        <details className="queue-filters">
          <summary>筛选 · URL 可复制</summary>
          <fieldset>
            <legend>信号状态</legend>
            {OPERATIONAL_SIGNAL_STATES.map((state) => (
              <label key={state}>
                <input
                  checked={selection.signalStates.includes(state)}
                  onChange={() => patch(withQueueSelection(search, {
                    signalStates: toggle(selection.signalStates, state as OperationalSignalState),
                  }))}
                  type="checkbox"
                /> {SIGNAL_LABELS[state]}
              </label>
            ))}
          </fieldset>
          {(sources.data?.length ?? 0) > 0 ? (
            <fieldset>
              <legend>告警来源</legend>
              {sources.data?.map((source) => (
                <label key={source.id}>
                  <input
                    checked={selection.sourceIds.includes(source.id)}
                    onChange={() => patch(withQueueSelection(search, {
                      sourceIds: toggle(selection.sourceIds, source.id),
                    }))}
                    type="checkbox"
                  /> {source.name}
                </label>
              ))}
            </fieldset>
          ) : null}
        </details>

        {queueReadState === "UNAVAILABLE" ? (
          <div className="error">事件队列暂时无法读取。请确认本地服务已启动后重新读取。</div>
        ) : null}
        {queueReadState === "STALE" ? (
          <div className="incident-readonly-note" role="status">
            事件列表最新刷新暂时未完成；当前继续显示上次成功读取的 {items.length} 条事件。
          </div>
        ) : null}
        {sourcesReadState === "UNAVAILABLE" ? (
          <div className="incident-readonly-note" role="status">告警来源筛选暂时不可用；事件列表仍可查看和处理。</div>
        ) : null}
        {sourcesReadState === "STALE" ? (
          <div className="incident-readonly-note" role="status">告警来源筛选最新刷新暂时未完成；当前筛选项保持不变。</div>
        ) : null}
        {queueReadState === "LOADING" && items.length === 0 ? <div className="empty">正在读取事件队列…</div> : null}
        {queueReadState === "READY" && items.length === 0 ? (
          <div className="empty">当前视图和筛选条件下没有事件。</div>
        ) : null}

        {stormSources.map((storm) => (
          <div className="incident-storm-summary" key={storm.sourceId} role="status">
            <div><strong>{storm.sourceName} 正在汇总平台消息</strong><span>{storm.startsAt ? `自 ${new Date(storm.startsAt).toLocaleString()}` : "当前生效"}</span></div>
            <p>{storm.reason}。当前筛选中的 {storm.visibleOccurrences} 个事件仍保留，排序未改变。</p>
          </div>
        ))}

        {items.some((item) => item.response_state === "UNACKNOWLEDGED") ? (
          <div className="incident-batch-bar">
            <div><strong>批量开始处理</strong><small>最多 50 条；每条独立检查，单条失败不会影响已经成功的事件。</small></div>
            <input
              maxLength={2000}
              onChange={(event) => setBatchReason(event.target.value)}
              placeholder="选填：这批事件的统一处理说明"
              value={batchReason}
            />
            <button
              className="secondary-btn"
              disabled={batchSelection.size === 0 || batchStartHandling.isPending}
              onClick={submitBatch}
              type="button"
            >开始处理已选 {batchSelection.size} 条</button>
            {batchStartHandling.data ? (
              <small role="status">成功 {batchStartHandling.data.succeeded}；失败 {batchStartHandling.data.failed}。失败事件保持选中，请刷新后核对。</small>
            ) : null}
            {batchStartHandling.error instanceof Error ? (
              <small className="error" role="alert">批量开始处理未完成：{batchStartHandling.error.message}</small>
            ) : null}
          </div>
        ) : null}

        <div className="incident-queue-rows">
          {items.map((item) => (
            <QueueCard
              item={item}
              key={item.id}
              onSelect={() => select(item.id)}
              selected={item.id === selectedId}
              batchSelected={batchSelection.has(item.id)}
              onBatchSelect={() => toggleBatch(item.id)}
            />
          ))}
        </div>

        {selection.cursor !== null || queue.data?.next_cursor ? (
          <div className="history-pager">
            <button
              className="secondary-btn"
              disabled={selection.cursor === null}
              onClick={() => patch(withQueueSelection(search, { cursor: null }))}
              type="button"
            >
              回到最紧急
            </button>
            <button
              className="secondary-btn"
              disabled={!queue.data?.next_cursor}
              onClick={() => queue.data?.next_cursor && patch(withQueueSelection(search, { cursor: queue.data.next_cursor }))}
              type="button"
            >
              下一页 →
            </button>
          </div>
        ) : null}
      </section>

      {overviewState === "MISSING" ? (
        <section className="incident-overview incident-overview--empty">
          <p>指定 Occurrence 不存在或已不可见。返回队列选择仍存在的对象。</p>
          <button className="secondary-btn" onClick={() => navigate(`/incidents${search ? `?${search}` : ""}`)} type="button">返回队列</button>
        </section>
      ) : overviewState === "UNAVAILABLE" ? (
        <section className="incident-overview incident-overview--empty">
          <p>事件详情暂时无法读取。请确认服务仍在运行后刷新页面。</p>
          <button className="secondary-btn" onClick={() => void overview.refetch()} type="button">重新读取</button>
        </section>
      ) : overviewState === "LOADING" ? (
        <section className="incident-overview incident-overview--empty">
          <p>正在读取事件详情…</p>
        </section>
      ) : (
        <OccurrenceOverview
          key={selectedId}
          item={overview.data ?? null}
          refreshDegraded={overviewState === "STALE"}
        />
      )}
    </div>
  );
}
