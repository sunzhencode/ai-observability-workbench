import { handlingLabel } from "../handling";
import type { AlertOut, IncidentDetail as Detail } from "../types";
import { useIncidentNotification } from "../queries/notifications";
import { ageFrom, sevPill, statePill } from "../util";
import { MetricEvidence } from "./alerts/MetricEvidence";

interface Props {
  incident: Detail | null;
  loading: boolean;
  refreshDegraded?: boolean;
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

export function IncidentDetail({ incident, loading, refreshDegraded = false, onOpenSourceSettings }: Props) {
  const notificationQuery = useIncidentNotification(incident?.id ?? null);
  const notification = notificationQuery.data ?? null;
  const notificationLoading = notificationQuery.isPending;

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

  return (
    <div className="pane">
      <div className="detail">
        {refreshDegraded ? <p role="status">事件详情最新刷新未完成；继续显示上次成功读取的信息。</p> : null}
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
          <div className="section__label">Legacy handling snapshot</div>
          <div className="placeholder-card">
            <strong>{handlingLabel(incident.handling_state)}</strong>
            <p>此处只保留重构前的兼容读值，不再提供第二套写状态机。请到 <a href="/incidents">Incident Queue</a> 使用 Ack、状态推进与 Timeline。</p>
          </div>

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
          {notificationQuery.isError ? <p className="error" role="alert">通知状态暂时无法读取；不能据此判断没有匹配策略。{notification ? "以下保留上次读取的状态。" : "请重新读取。"}<button className="text-btn" type="button" onClick={() => void notificationQuery.refetch()}>重新读取通知</button></p> : null}
          {notificationLoading ? (
            <div className="placeholder-card">正在加载 Incident 级通知状态…</div>
          ) : notificationQuery.isError && !notification ? null : !notification?.route ? (
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
