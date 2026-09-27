import { useEffect, useState } from "react";
import { api } from "../api/client";
import {
  handlingActionLabel,
  handlingActions,
  handlingLabel,
  resolveHandlingReason,
} from "../handling";
import type { AlertOut, HandlingState, IncidentDetail as Detail, IncidentNotification } from "../types";
import { ageFrom, sevPill, statePill } from "../util";
import { MetricEvidence } from "./alerts/MetricEvidence";

interface Props {
  incident: Detail | null;
  loading: boolean;
  onHandlingChange: (incidentId: number, state: HandlingState, reason: string) => Promise<void>;
  onOpenSourceSettings: (sourceId: string) => void;
}

function LabelChips({ labels }: { labels: Record<string, string> }) {
  const entries = Object.entries(labels).filter(([key]) => key !== "alertname");
  if (entries.length === 0) return <span className="chip">no labels</span>;
  return (
    <div className="chips">
      {entries.map(([key, value]) => (
        <span className="chip" key={key}>
          <span className="chip__key">{key}=</span>
          {value}
        </span>
      ))}
    </div>
  );
}

function Member({ alert }: { alert: AlertOut }) {
  return (
    <div className="member">
      <span className={`member__dot member__dot--${alert.severity}`} />
      <div className="member__body">
        <div className="member__name">{alert.alertname}</div>
        <LabelChips labels={alert.labels} />
        <div className="member__meta">
          <span className={sevPill(alert.severity)}>{alert.severity}</span>
          <span>{alert.source_state}</span>
          <span>started {ageFrom(alert.starts_at)} ago</span>
          {alert.origin === "backfill" && <span>· reconstructed</span>}
        </div>
      </div>
    </div>
  );
}

export function IncidentDetail({ incident, loading, onHandlingChange, onOpenSourceSettings }: Props) {
  const [reason, setReason] = useState("");
  const [handlingBusy, setHandlingBusy] = useState(false);
  const [handlingError, setHandlingError] = useState(false);
  const [notification, setNotification] = useState<IncidentNotification | null>(null);
  const [notificationLoading, setNotificationLoading] = useState(false);

  useEffect(() => {
    setReason("");
    setHandlingError(false);
  }, [incident?.id]);

  useEffect(() => {
    if (!incident) {
      setNotification(null);
      return;
    }
    let cancelled = false;
    setNotificationLoading(true);
    api.incidentNotification(incident.id)
      .then((value) => { if (!cancelled) setNotification(value); })
      .catch(() => { if (!cancelled) setNotification(null); })
      .finally(() => { if (!cancelled) setNotificationLoading(false); });
    return () => { cancelled = true; };
  }, [incident]);

  if (!incident) {
    return (
      <div className="pane">
        <div className="placeholder">
          <span className="placeholder__glyph">◍ ◍ ◍</span>
          {loading ? "加载中…" : "选择一个告警组查看原始告警。"}
        </div>
      </div>
    );
  }

  const actions = handlingActions(incident.handling_state);
  const applyHandlingState = async (state: HandlingState) => {
    if (handlingBusy) return;
    setHandlingBusy(true);
    setHandlingError(false);
    try {
      await onHandlingChange(incident.id, state, resolveHandlingReason(reason, state));
      setReason("");
    } catch {
      setHandlingError(true);
    } finally {
      setHandlingBusy(false);
    }
  };

  return (
    <div className="pane">
      <div className="detail">
        <h1 className="detail__title">{incident.title}</h1>
        <div className="detail__pills">
          <span className={sevPill(incident.severity)}>{incident.severity}</span>
          <span className={statePill(incident.source_state)}>{incident.source_state}</span>
          <button
            className="pill pill--button"
            onClick={() => onOpenSourceSettings(incident.source_id)}
            type="button"
          >
            来源 {incident.source_name ?? incident.source_id}
          </button>
          <span className="pill pill--count">{incident.member_count} 条告警</span>
          <span className="pill pill--count">{handlingLabel(incident.handling_state)}</span>
        </div>

        <div className={`rule-result rule-result--${incident.aggregation_status}`}>
          <span>聚合规则</span>
          <strong>
            {incident.aggregation_status === "unmatched"
              ? "未命中规则 · 按 fingerprint 安全隔离"
              : incident.aggregation_rule_name ?? `规则 #${incident.aggregation_rule_id}`}
          </strong>
          {incident.aggregation_status === "missing_labels" && (
            <small>已命中规则，但缺少聚合标签，因此没有与其它告警合并。</small>
          )}
        </div>

        <div className="section">
          <div className="section__label">原始告警成员</div>
          {incident.members.map((alert) => (
            <Member key={alert.id} alert={alert} />
          ))}
        </div>

        {/* F27: evidence is per member, so this sits after the member list —
            you pick which one you are asking about. */}
        <MetricEvidence members={incident.members} />

        <div className="section">
          <div className="section__label">分组解释</div>
          <div className="codeblock">{incident.grouping_explanation}</div>
        </div>

        <div className="section">
          <div className="section__label">处理状态</div>
          {actions.length > 0 ? (
            <div className="handling-card">
              <label className="handling-card__label" htmlFor="handling-reason">
                本次状态变更原因（可选）
              </label>
              <textarea
                id="handling-reason"
                className="handling-card__reason"
                value={reason}
                onChange={(event) => setReason(event.target.value)}
                placeholder="留空即记录默认说明；需要时再补充你的判断"
                rows={2}
              />
              <div className="handling-card__actions">
                {actions.map((state) => (
                  <button
                    className="handling-action"
                    disabled={handlingBusy}
                    key={state}
                    onClick={() => applyHandlingState(state)}
                    type="button"
                  >
                    {handlingActionLabel(state)}
                  </button>
                ))}
              </div>
              {handlingError && <div className="handling-card__error">状态更新失败。</div>}
            </div>
          ) : (
            <div className="placeholder-card">该告警组已进入终态。</div>
          )}

          {incident.handling_history.length > 0 && (
            <div className="audit-list">
              {incident.handling_history.map((item) => (
                <div className="audit-item" key={item.id}>
                  <div className="audit-item__states">{item.from_state} → {item.to_state}</div>
                  <div className="audit-item__reason">{item.reason}</div>
                  <div className="audit-item__meta">
                    {item.actor} · {new Date(item.created_at).toLocaleString()}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="section">
          <div className="section__label">通知</div>
          {notificationLoading ? (
            <div className="placeholder-card">正在加载 Incident 级通知状态…</div>
          ) : !notification?.route ? (
            <div className="placeholder-card">当前 occurrence 没有匹配通知策略；不会隐式发送，也没有逐 Alert 发送动作。</div>
          ) : (
            <div className="incident-notification">
              <div className="incident-notification__head">
                <div><strong>{notification.route.policy_name}</strong><small>锁定策略 v{notification.route.policy_version} · occurrence #{notification.occurrence_no}</small></div>
                <span className={`status-label status-label--${notification.route.status.toLowerCase()}`}>{notification.route.status}</span>
              </div>
              <div className="incident-notification__meta">
                <span>重复提醒：{notification.route.repeat_interval_seconds === 0 ? "关闭" : `${Math.round(notification.route.repeat_interval_seconds / 60)} 分钟`}</span>
                <span>下次提醒：{notification.route.next_reminder_at ? new Date(notification.route.next_reminder_at).toLocaleString() : "—"}</span>
                {notification.route.termination_reason && <span>终止原因：{notification.route.termination_reason}</span>}
              </div>
              <div className="notification-targets">
                {notification.targets.map((target) => {
                  const related = notification.deliveries.filter((item) => item.route_target_id === target.id);
                  const last = related[related.length - 1];
                  return (
                    <div className="notification-target" key={target.id}>
                      <span className={`delivery-symbol delivery-symbol--${(last?.state ?? "pending").toLowerCase()}`} aria-hidden="true">●</span>
                      <div><strong>{target.channel_name}</strong><small>{last ? `${last.event_type} · ${last.state} · ${last.attempt_count}/5` : "等待首次事件"}</small><small>最近成功：{target.last_success_at ? new Date(target.last_success_at).toLocaleString() : "—"}</small></div>
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
