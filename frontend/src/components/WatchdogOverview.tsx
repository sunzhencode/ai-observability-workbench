/*
 * Watchdog 回答的是"监控链路本身还活着吗"，是背景信息，不是这一页的主角。
 * 所以它是页面底部的一条状态条，而不是顶部的大卡片：收起时占一行，仍然给出
 * 健康/异常/未知的文字与数字（CAP-08：状态必须有文字，不能只靠颜色），
 * 点开才向上展开按来源、按 cluster 的下钻。告警组永远排在它前面。
 */
import { useState } from "react";
import type { Health } from "../types";
import { ageFrom } from "../util";

interface Props {
  health: Health | null;
  healthError: boolean;
}

const statusLabel = {
  healthy: "健康",
  missing: "异常",
  unknown: "未知",
} as const;

const sourceHealthLabel = {
  HEALTHY: "Endpoint 健康",
  DEGRADED: "Endpoint 降级",
  UNAVAILABLE: "Endpoint 不可用",
  DISABLED: "来源停用",
  ARCHIVED: "来源归档",
} as const;

const monitorStateLabel = {
  ENABLED: "已监测",
  DISABLED: "未监测",
  SOURCE_DISABLED: "来源停用",
  SOURCE_ARCHIVED: "来源归档",
} as const;

function sourceHealthClass(value: keyof typeof sourceHealthLabel) {
  if (value === "HEALTHY") return "healthy";
  if (value === "DEGRADED") return "unknown";
  return "missing";
}

function WatchdogStrip({ status, children }: { status: string; children: React.ReactNode }) {
  return <section className={`watchdog-strip watchdog-strip--${status}`}>{children}</section>;
}

export function WatchdogOverview({ health, healthError }: Props) {
  const [expanded, setExpanded] = useState(false);
  const watchdog = health?.watchdog;
  const overall = healthError ? "unknown" : (watchdog?.overall_status ?? "no_data");

  if (!watchdog || healthError) {
    return (
      <WatchdogStrip status={overall}>
        <div className="watchdog-strip__row watchdog-strip__row--static">
          <span className="watchdog-strip__dot" aria-hidden="true" />
          <strong className="watchdog-strip__title">Watchdog</strong>
          <span className="watchdog-strip__note">
            {healthError ? "无法读取本地后端健康状态" : "正在等待首次轮询"}
          </span>
        </div>
      </WatchdogStrip>
    );
  }

  if (watchdog.overall_status === "no_data" && watchdog.sources.length === 0) {
    return (
      <WatchdogStrip status="no_data">
        <div className="watchdog-strip__row watchdog-strip__row--static">
          <span className="watchdog-strip__dot" aria-hidden="true" />
          <strong className="watchdog-strip__title">Watchdog</strong>
          <span className="watchdog-strip__note">暂无 Watchdog 数据，尚不能判断监控链路是否健康。</span>
        </div>
      </WatchdogStrip>
    );
  }

  const metrics = [
    { key: "healthy", label: "健康", value: watchdog.summary.healthy },
    { key: "missing", label: "异常", value: watchdog.summary.missing },
    { key: "unknown", label: "未知", value: watchdog.summary.unknown },
  ] as const;
  const hasSourceDrilldown = watchdog.sources.length > 0;

  return (
    <WatchdogStrip status={watchdog.overall_status}>
      <button
        aria-expanded={expanded}
        className="watchdog-strip__row watchdog-strip__toggle"
        onClick={() => setExpanded((value) => !value)}
        type="button"
      >
        <span className="watchdog-strip__dot" aria-hidden="true" />
        <strong className="watchdog-strip__title">Watchdog</strong>
        <span className="watchdog-strip__counts">
          {metrics.map((metric) => (
            <span className={`watchdog-count watchdog-count--${metric.key}`} key={metric.key}>
              {metric.label}
              <strong>{metric.value}</strong>
            </span>
          ))}
        </span>
        <span className="watchdog-strip__note">
          共 {watchdog.summary.total} 个受监测集群
          {hasSourceDrilldown && ` · ${watchdog.monitored_source_count} 个来源已监测 · ${watchdog.unmonitored_source_count} 个来源未监测`}
        </span>
        <span className="watchdog-strip__more">
          {expanded ? "收起" : "明细"}
          <span aria-hidden="true">{expanded ? "↑" : "↓"}</span>
        </span>
      </button>

      {expanded && (
        <div className="watchdog-clusters">
          {hasSourceDrilldown ? (
            watchdog.sources.map((source) => (
              <div className="watchdog-source" key={source.source_id}>
                <div className={`watchdog-source__head watchdog-cluster--${sourceHealthClass(source.source_health)}`}>
                  <span className="watchdog-cluster__dot" aria-hidden="true" />
                  <div>
                    <strong>{source.source_name}</strong>
                    <small>{sourceHealthLabel[source.source_health]} · {monitorStateLabel[source.monitor_state]} · {source.summary.total} clusters</small>
                  </div>
                  <span>{source.summary.missing} missing · {source.summary.unknown} unknown</span>
                </div>
                {source.clusters.length === 0 ? (
                  <div className="watchdog-cluster watchdog-cluster--unknown">
                    <span className="watchdog-cluster__dot" aria-hidden="true" />
                    <div className="watchdog-cluster__name">
                      <strong>未监测</strong>
                      <small>该来源未启用 Watchdog 或尚无 inventory。</small>
                    </div>
                    <span className="watchdog-cluster__status">未知</span>
                    <span className="watchdog-cluster__time">等待来源设置</span>
                  </div>
                ) : source.clusters.map((item) => (
                  <div className={`watchdog-cluster watchdog-cluster--${item.status}`} key={`${source.source_id}:${item.identity_value}`}>
                    <span className="watchdog-cluster__dot" aria-hidden="true" />
                    <div className="watchdog-cluster__name" title={item.cluster}>
                      <strong>{item.cluster}</strong>
                      <small>{item.inventory_state ?? "DISCOVERED"} · {item.health_state ?? "UNKNOWN"}</small>
                    </div>
                    <span className="watchdog-cluster__status">{statusLabel[item.status]}</span>
                    <span className="watchdog-cluster__time">
                      {item.status === "unknown"
                        ? "Endpoint 当前不可观测"
                        : item.last_seen_at
                          ? `最后看到 ${ageFrom(item.last_seen_at)} 前`
                          : "没有 last seen 时间"}
                    </span>
                  </div>
                ))}
              </div>
            ))
          ) : (
            watchdog.clusters.map((item) => (
              <div className={`watchdog-cluster watchdog-cluster--${item.status}`} key={item.cluster}>
                <span className="watchdog-cluster__dot" aria-hidden="true" />
                <div className="watchdog-cluster__name" title={item.cluster}>
                  <strong>{item.cluster}</strong>
                  {item.cluster === "<no-cluster>" && <small>上游缺少 cluster label</small>}
                </div>
                <span className="watchdog-cluster__status">{statusLabel[item.status]}</span>
                <span className="watchdog-cluster__time">
                  {item.status === "unknown"
                    ? "Alertmanager 当前不可观测"
                    : item.last_seen_at
                      ? `最后看到 ${ageFrom(item.last_seen_at)} 前`
                      : "没有 last seen 时间"}
                </span>
              </div>
            ))
          )}
        </div>
      )}
    </WatchdogStrip>
  );
}
