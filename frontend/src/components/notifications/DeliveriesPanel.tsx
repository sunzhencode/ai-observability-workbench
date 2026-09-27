/**
 * 投递记录 — what was sent, what was suppressed, and why.
 *
 * Manual retry keeps its warning verbatim: delivery is at-least-once, so a
 * retry after an ambiguous result can duplicate a message in the group.
 */
import { useState } from "react";
import { api } from "../../api/client";
import { describeRequestFailure } from "../../requestError";
import {
  useDeliveryInvalidation,
  useNotificationDeliveries,
  useNotificationDelivery,
} from "../../queries/notifications";
import type { NotificationChannel } from "../../types";
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

  const deliveriesQuery = useNotificationDeliveries({ state, eventType, channelId });
  const detailQuery = useNotificationDelivery(selectedId);
  const invalidateDelivery = useDeliveryInvalidation();

  // Incident id filters client-side: the endpoint does not take it, and a
  // single-incident view is a narrowing of what is already on screen.
  const all = deliveriesQuery.data ?? [];
  const deliveries = incidentId
    ? all.filter((item) => item.incident_id === Number(incidentId))
    : all;
  const detail = detailQuery.data ?? null;
  const busy = deliveriesQuery.isFetching || detailQuery.isFetching;

  const loadError =
    error ||
    (deliveriesQuery.isError ? "投递记录加载失败。" : "") ||
    (detailQuery.isError ? "投递详情加载失败。" : "");

  const retry = async () => {
    if (!detail) return;
    const warning = "人工重试采用 at-least-once 语义：若上一次结果不明确，极端情况下可能重复消息。确认重试？";
    if (!window.confirm(warning)) return;
    setError("");
    try {
      await api.retryNotificationDelivery(detail.id);
      await invalidateDelivery(detail.id);
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
          <select onChange={(event) => setState(event.target.value)} value={state}>
            <option value="">全部</option>
            {STATES.map((item) => (
              <option key={item}>{item}</option>
            ))}
          </select>
        </label>
        <label>
          <span>事件</span>
          <select onChange={(event) => setEventType(event.target.value)} value={eventType}>
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
          <select onChange={(event) => setChannelId(event.target.value)} value={channelId}>
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
            onChange={(event) => setIncidentId(event.target.value)}
            type="number"
            value={incidentId}
          />
        </label>
        <button
          className="secondary-btn"
          disabled={busy}
          onClick={() => void deliveriesQuery.refetch()}
          type="button"
        >
          刷新
        </button>
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
          {!busy && deliveries.length === 0 && (
            <div className="empty">当前筛选没有投递记录。</div>
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
