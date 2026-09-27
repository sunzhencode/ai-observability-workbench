/**
 * 投递记录 — what was sent, what was suppressed, and why.
 *
 * Manual retry keeps its warning verbatim: delivery is at-least-once, so a
 * retry after an ambiguous result can duplicate a message in the group.
 */
import { useState } from "react";
import { describeRequestFailure } from "../../requestError";
import {
  useNotificationCommands,
  useNotificationDeliveries,
  useNotificationDelivery,
} from "../../queries/notifications";
import type { NotificationChannel } from "../../types";
import { describeDeliveryEvidence } from "../../notificationDeliveryEvidence";
import { EVENTS } from "./shared";

const STATES = [
  "PENDING",
  "IN_FLIGHT",
  "SUCCEEDED",
  "RETRY_WAIT",
  "PERMANENT_FAILED",
  "SUPPRESSED",
  "CANCELED",
];

export function DeliveriesPanel({
  channels,
  selectedId,
  onSelect,
}: {
  channels: NotificationChannel[];
  selectedId: number | null;
  onSelect: (deliveryId: number | null) => void;
}) {
  const [state, setState] = useState("");
  const [eventType, setEventType] = useState("");
  const [channelId, setChannelId] = useState("");
  const [incidentId, setIncidentId] = useState("");
  const [error, setError] = useState("");
  const [beforeId, setBeforeId] = useState<number | undefined>();

  const parsedIncidentId = Number(incidentId);
  const invalidIncidentId = incidentId !== "" && (!/^[1-9]\d*$/.test(incidentId) || !Number.isSafeInteger(parsedIncidentId));
  const deliveriesQuery = useNotificationDeliveries({
    state, eventType, channelId,
    incidentId: incidentId && !invalidIncidentId ? parsedIncidentId : undefined,
    beforeId,
  }, !invalidIncidentId);
  const detailQuery = useNotificationDelivery(selectedId);
  const commands = useNotificationCommands();

  const deliveries = invalidIncidentId ? [] : deliveriesQuery.data ?? [];
  const detail = detailQuery.data ?? null;
  const evidence = detail ? describeDeliveryEvidence(detail.payload_snapshot) : null;
  const busy = deliveriesQuery.isFetching || detailQuery.isFetching;

  const loadError =
    error ||
    (deliveriesQuery.isError ? "投递记录暂时不可用；请刷新后重试。" : "") ||
    (detailQuery.isError ? "投递详情暂时不可用；列表中的投递状态仍可查看。" : "");

  const retry = async () => {
    if (!detail) return;
    const warning = "人工重试采用 at-least-once 语义：若上一次结果不明确，极端情况下可能重复消息。确认重试？";
    if (!window.confirm(warning)) return;
    setError("");
    try {
      await commands.retryNotificationDelivery(detail.id);
    } catch (cause) {
      const failure = describeRequestFailure(cause, "人工重试失败。");
      setError(failure.detail ?? failure.title);
    }
  };

  return (
    <div className="delivery-page">
      <div className="delivery-filters">
        <label>
          <span>状态</span>
          <select onChange={(event) => { setState(event.target.value); setBeforeId(undefined); onSelect(null); }} value={state}>
            <option value="">全部</option>
            {STATES.map((item) => (
              <option key={item}>{item}</option>
            ))}
          </select>
        </label>
        <label>
          <span>事件</span>
          <select onChange={(event) => { setEventType(event.target.value); setBeforeId(undefined); onSelect(null); }} value={eventType}>
            <option value="">全部</option>
            {EVENTS.map((event) => (
              <option key={event.value} value={event.value}>
                {event.label}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>通道</span>
          <select onChange={(event) => { setChannelId(event.target.value); setBeforeId(undefined); onSelect(null); }} value={channelId}>
            <option value="">全部</option>
            {channels.map((channel) => (
              <option key={channel.id} value={channel.id}>
                {channel.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>Incident ID</span>
          <input
            min={1}
            onChange={(event) => { setIncidentId(event.target.value); setBeforeId(undefined); onSelect(null); }}
            type="number"
            value={incidentId}
          />
        </label>
        <button
          className="secondary-btn"
          disabled={busy || invalidIncidentId}
          onClick={() => { if (!invalidIncidentId) void deliveriesQuery.refetch(); }}
          type="button"
        >
          刷新
        </button>
      </div>

      {invalidIncidentId ? <p className="error" role="alert">请输入有效的正整数 Incident ID。</p> : null}
      <div className="history-pager" aria-label="投递记录分页">
        <small>每页最多 100 条，按记录编号从新到旧；筛选覆盖全部保留记录。</small>
        <button className="secondary-btn" disabled={busy || beforeId === undefined} onClick={() => { setBeforeId(undefined); onSelect(null); }} type="button">返回最新记录</button>
        <button className="secondary-btn" disabled={busy || deliveries.length < 100} onClick={() => { setBeforeId(deliveries.at(-1)?.id); onSelect(null); }} type="button">查看更早记录</button>
      </div>
      <div className="resource-split delivery-split">
        <section className="resource-list">
          {deliveries.map((item) => (
            <button
              className={`delivery-row${detail?.id === item.id ? " delivery-row--active" : ""}`}
              key={item.id}
              onClick={() => onSelect(item.id)}
              type="button"
            >
              <span
                className={`delivery-symbol delivery-symbol--${item.state.toLowerCase()}`}
                aria-hidden="true"
              >
                ●
              </span>
              <span>
                <strong>
                  #{item.incident_id} · {item.event_type}
                </strong>
                <small>
                  {item.source_name ?? item.source_id} · {item.channel_name} ·{" "}
                  {new Date(item.created_at).toLocaleString()}
                </small>
                {item.suppression_reason && <small>原因：{item.suppression_reason}</small>}
              </span>
              <i>
                {item.state} · {item.attempt_count}/5
              </i>
            </button>
          ))}
          {!busy && !invalidIncidentId && !deliveriesQuery.isError && deliveries.length === 0 && (
            <div className="empty">{beforeId === undefined ? "当前筛选没有投递记录。" : "当前筛选没有更早的投递记录。"}</div>
          )}
        </section>

        <section className="resource-editor">
          {!detail ? (
            <div className="placeholder-card">
              选择一条记录查看 attempt 时间线、失败/抑制原因和下一重试。
            </div>
          ) : (
            <>
              <header className="editor-head">
                <div>
                  <span className="eyebrow">Delivery #{detail.id}</span>
                  <h2>
                    Incident #{detail.incident_id} · {detail.event_type}
                  </h2>
                </div>
                <span className={`status-label status-label--${detail.state.toLowerCase()}`}>
                  {detail.state}
                </span>
              </header>
              <div className="delivery-summary">
                <span>
                  来源 <strong>{detail.source_name ?? detail.source_id}</strong>
                </span>
                <span>
                  通道 <strong>{detail.channel_name}</strong>
                </span>
                <span>
                  尝试 <strong>{detail.attempt_count}/5</strong>
                </span>
                <span>
                  下一动作 <strong>{new Date(detail.next_attempt_at).toLocaleString()}</strong>
                </span>
                {detail.suppression_reason && (
                  <span>
                    抑制原因 <strong>{detail.suppression_reason}</strong>
                  </span>
                )}
              </div>
              {evidence && (
                <section className="editor-section">
                  <h3>本次消息的证据与深链</h3>
                  {evidence.occurrenceId && (
                    <p>
                      <a href={`/incidents/${evidence.occurrenceId}`}>
                        打开 Incident #{evidence.occurrenceId}
                      </a>
                    </p>
                  )}
                  {evidence.metrics.length > 0 ? (
                    <div className="attempt-list">
                      {evidence.metrics.map((metric) => (
                        <div className="attempt-row" key={metric.id}>
                          <div>
                            <strong>{metric.label}</strong>
                            <small>{metric.summary}</small>
                          </div>
                        </div>
                      ))}
                    </div>
                  ) : evidence.status === "UNAVAILABLE" ? (
                    <div className="toast-line toast-line--error">
                      证据投影未完成，但该通知没有被延迟。safe code：
                      {evidence.safeCode ?? "NOTIFICATION_EVIDENCE_UNAVAILABLE"}。
                      请检查该 Incident 的证据快照；修复后只影响后续首次/升级消息。
                    </div>
                  ) : (
                    <div className="placeholder-card">
                      该事件未附加确定性指标；Reminder/Recovered 默认只带 Incident 深链。
                    </div>
                  )}
                  {evidence.links.length > 0 && (
                    <div className="inline-actions">
                      {evidence.links.map((link) => (
                        <a href={link.url} key={link.url} rel="noreferrer" target="_blank">
                          Grafana · {link.label}
                        </a>
                      ))}
                    </div>
                  )}
                </section>
              )}
              <section className="editor-section">
                <h3>Attempt 时间线</h3>
                {detail.attempts.length === 0 ? (
                  <div className="placeholder-card">
                    尚未开始 provider attempt，可能仍在排队或已提前抑制。
                  </div>
                ) : (
                  <div className="attempt-list">
                    {detail.attempts.map((attempt) => (
                      <div className="attempt-row" key={attempt.id}>
                        <span className="attempt-row__number">{attempt.attempt_no}</span>
                        <div>
                          <strong>
                            {attempt.outcome ?? "IN_FLIGHT"} · {attempt.trigger}
                          </strong>
                          <small>
                            {new Date(attempt.started_at).toLocaleString()} · HTTP{" "}
                            {attempt.http_status ?? "—"}
                          </small>
                          {(attempt.error_code || attempt.error_summary) && (
                            <small>
                              {attempt.error_code ?? "ERROR"} · {attempt.error_summary ?? "无详情"}
                            </small>
                          )}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </section>
              {detail.state === "PERMANENT_FAILED" && (
                <button
                  className="danger-primary-btn"
                  disabled={busy}
                  onClick={() => void retry()}
                  type="button"
                >
                  人工重试（可能重复）
                </button>
              )}
            </>
          )}
        </section>
      </div>

      {loadError && (
        <div className="toast-line toast-line--error" role="alert">
          {loadError}
        </div>
      )}
    </div>
  );
}
