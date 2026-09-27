import { useMemo, useRef } from "react";
import { Link, useSearchParams } from "react-router";
import type { AnalyticsOverview } from "../api/client";
import {
  ANALYTICS_RANGES,
  ANALYTICS_SEVERITIES,
  parseAnalyticsSelection,
  withAnalyticsSelection,
  type AnalyticsSelection,
} from "../appUrl";
import { useEventSources } from "../queries/catalog";
import { useAnalyticsOverview } from "../queries/analytics";
import { useServices } from "../queries/services";

type Ratio = AnalyticsOverview["signal"]["compression"];
type Duration = AnalyticsOverview["response"]["mtta"]["completed"];

const RANGE_LABELS = { "24h": "24 小时", "7d": "7 天", "30d": "30 天" } as const;
const SEVERITY_LABELS = {
  critical: "严重",
  warning: "警告",
  info: "提示",
  unknown: "未知",
} as const;
const MODE_LABELS: Record<string, string> = {
  FAKE: "本地 Fake",
  EXTERNAL: "外部真实调用",
  UNKNOWN_LEGACY: "历史模式未知",
};

function duration(value: number | null): string {
  if (value === null) return "—";
  if (value < 1_000) return `${value} ms`;
  if (value < 60_000) return `${(value / 1_000).toFixed(1)} 秒`;
  if (value < 3_600_000) return `${(value / 60_000).toFixed(1)} 分钟`;
  return `${(value / 3_600_000).toFixed(1)} 小时`;
}

function percent(value: Ratio): string {
  return value.ratio === null ? "—" : `${(value.ratio * 100).toFixed(1)}%`;
}

function RatioCard({
  label,
  value,
  help,
  asPercent = true,
}: {
  label: string;
  value: Ratio;
  help: string;
  asPercent?: boolean;
}) {
  return (
    <article className="analytics-card">
      <span>{label}</span>
      <strong>
        {asPercent
          ? percent(value)
          : value.ratio === null ? "—" : value.ratio.toFixed(2)}
      </strong>
      <small>{value.numerator} / {value.denominator} · {help}</small>
    </article>
  );
}

function DurationCard({ label, value }: { label: string; value: Duration }) {
  return (
    <article className="analytics-card">
      <span>{label}</span>
      <strong>{duration(value.median_ms)}</strong>
      <small>{value.count} 个样本 · P90 {duration(value.p90_ms)}</small>
    </article>
  );
}

function Counts({ values, empty }: { values: Record<string, number>; empty: string }) {
  const rows = Object.entries(values).sort(([left], [right]) => left.localeCompare(right));
  if (rows.length === 0) return <p className="analytics-empty-inline">{empty}</p>;
  return (
    <dl className="analytics-counts">
      {rows.map(([label, count]) => (
        <div key={label}><dt>{label}</dt><dd>{count}</dd></div>
      ))}
    </dl>
  );
}

export function AnalyticsPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const search = searchParams.toString();
  const selection = useMemo(() => parseAnalyticsSelection(search), [search]);
  const overview = useAnalyticsOverview(selection);
  const sources = useEventSources();
  const services = useServices();
  const data = overview.data;
  const pendingSelection = useRef(selection);
  pendingSelection.current = selection;
  const updateSelection = (patch: Partial<AnalyticsSelection>) => {
    const next = { ...pendingSelection.current, ...patch };
    pendingSelection.current = next;
    setSearchParams(withAnalyticsSelection("", next), { replace: true });
  };

  return (
    <main className="analytics-page management-page">
      <header className="analytics-hero">
        <div>
          <p className="eyebrow">确定性运营读模型</p>
          <h1>Analytics</h1>
          <p>回答响应效率、信号质量和外部调用结果；数字来自持久汇总，不由模型撰写。</p>
        </div>
        <Link className="secondary-btn" to="/history">查看事件历史</Link>
      </header>

      <section className="analytics-filters" aria-label="Analytics 筛选">
        <div className="analytics-range" role="group" aria-label="统计范围">
          {ANALYTICS_RANGES.map((range) => (
            <button
              aria-pressed={selection.range === range}
              className={selection.range === range ? "analytics-range--active" : ""}
              key={range}
              onClick={() => updateSelection({ range })}
              type="button"
            >
              {RANGE_LABELS[range]}
            </button>
          ))}
        </div>
        <label>
          <span>来源</span>
          <select
            onChange={(event) => updateSelection({
              sourceId: event.target.value || null,
            })}
            value={selection.sourceId ?? ""}
          >
            <option value="">全部来源</option>
            {(sources.data ?? []).map((source) => (
              <option key={source.id} value={source.id}>{source.name}</option>
            ))}
          </select>
        </label>
        <label>
          <span>服务</span>
          <select
            onChange={(event) => updateSelection({
              serviceId: event.target.value ? Number(event.target.value) : null,
            })}
            value={selection.serviceId ?? ""}
          >
            <option value="">全部服务</option>
            {(services.data ?? []).map((service) => (
              <option key={service.id} value={service.id}>{service.name}</option>
            ))}
          </select>
        </label>
        <label>
          <span>信号严重度</span>
          <select
            onChange={(event) => updateSelection({
              signalSeverity: (event.target.value || null) as AnalyticsSelection["signalSeverity"],
            })}
            value={selection.signalSeverity ?? ""}
          >
            <option value="">全部严重度</option>
            {ANALYTICS_SEVERITIES.map((severity) => (
              <option key={severity} value={severity}>{SEVERITY_LABELS[severity]}</option>
            ))}
          </select>
        </label>
      </section>

      {overview.isError ? (
        <div className="toast-line toast-line--error" role="alert">
          Analytics 汇总暂时无法读取；请刷新后重试。
        </div>
      ) : overview.isPending ? (
        <div className="analytics-state" role="status">正在读取运营汇总…</div>
      ) : data ? (
        <>
          <div className={`analytics-state analytics-state--${data.freshness.toLowerCase()}`} role="status">
            <strong>{data.freshness === "READY" ? "汇总已就绪" : "首次汇总排队中"}</strong>
            <span>
              UTC {data.from_utc} → {data.to_utc}
              {data.generated_at ? ` · 生成于 ${new Date(data.generated_at).toLocaleString()}` : ""}
            </span>
            <small>范围按完整 UTC 小时计算；时间显示使用当前浏览器时区。</small>
          </div>

          <section className="analytics-section">
            <div className="analytics-section__head">
              <div><p className="eyebrow">Observe → Verify</p><h2>响应结果</h2></div>
              <span>完成与未完成分开计算</span>
            </div>
            <div className="analytics-grid analytics-grid--four">
              <DurationCard label="确认用时 · 已完成中位数" value={data.response.mtta.completed} />
              <DurationCard label="确认用时 · 仍未完成年龄" value={data.response.mtta.unfinished} />
              <DurationCard label="解决用时 · 已完成中位数" value={data.response.resolution.completed} />
              <DurationCard label="解决用时 · 仍未完成年龄" value={data.response.resolution.unfinished} />
            </div>
            <div className="analytics-grid analytics-grid--two analytics-subsections">
              <article><h3>任务结果</h3><Counts empty="此范围内没有任务结果" values={data.response.task_outcomes} /></article>
              <article><h3>解决分类</h3><Counts empty="此范围内没有已解决事件" values={data.response.resolution_codes} /></article>
            </div>
          </section>

          <section className="analytics-section">
            <div className="analytics-section__head">
              <div><p className="eyebrow">Signal quality</p><h2>信号质量</h2></div>
              <span>{data.signal.new_occurrences} 个新事件 · {data.signal.new_alert_instances} 个告警实例</span>
            </div>
            <div className="analytics-grid analytics-grid--four">
              <RatioCard label="压缩率" value={data.signal.compression} help="(告警实例 − 事件) / 告警实例" />
              <RatioCard asPercent={false} label="每事件告警数" value={data.signal.alerts_per_occurrence} help="告警实例 / 事件" />
              <RatioCard label="创建时未映射" value={data.signal.creation_unmapped} help="未映射 / 已知创建分配" />
              <RatioCard label="确认 SLA 违约" value={data.signal.ack_sla_breach} help="超时 / 有 SLA 的事件" />
              <article className="analytics-card"><span>创建分配未知</span><strong>{data.signal.creation_assignment_unknown}</strong><small>旧记录不进入未映射分母</small></article>
              <article className="analytics-card"><span>当前仍未映射</span><strong>{data.signal.current_unmapped}</strong><small>所选范围事件当前状态</small></article>
              <article className="analytics-card"><span>抖动抑制启用</span><strong>{data.signal.flapping_activations}</strong><small>确定性生命周期事实</small></article>
              <article className="analytics-card"><span>风暴汇总启用</span><strong>{data.signal.storm_activations}</strong><small>确定性生命周期事实</small></article>
            </div>
          </section>

          <section className="analytics-section">
            <div className="analytics-section__head">
              <div><p className="eyebrow">Controlled egress</p><h2>通知投递</h2></div>
              <span>Fake 与外部真实调用不混算</span>
            </div>
            <div className="analytics-mode-grid">
              {Object.entries(data.notifications).map(([mode, item]) => (
                <article className="analytics-mode" key={mode}>
                  <header><h3>{MODE_LABELS[mode] ?? mode}</h3><code>{mode}</code></header>
                  <strong>{percent(item.success_rate)} 成功</strong>
                  <p>{item.succeeded} 成功 / {item.permanently_failed} 永久失败</p>
                  <dl>
                    <div><dt>待投递积压</dt><dd>{item.pending_backlog}</dd></div>
                    <div><dt>抑制噪声</dt><dd>{item.noise}</dd></div>
                    <div><dt>投递中位数</dt><dd>{duration(item.latency.median_ms)}</dd></div>
                    <div><dt>投递 P90</dt><dd>{duration(item.latency.p90_ms)}</dd></div>
                  </dl>
                </article>
              ))}
            </div>
          </section>

          <section className="analytics-section">
            <div className="analytics-section__head">
              <div><p className="eyebrow">Read-only investigator</p><h2>AI 调查</h2></div>
              <span>结构化调查报告，不代表自动处置</span>
            </div>
            <div className="analytics-mode-grid">
              {Object.entries(data.ai).map(([mode, item]) => (
                <article className="analytics-mode" key={mode}>
                  <header><h3>{MODE_LABELS[mode] ?? mode}</h3><code>{mode}</code></header>
                  <strong>{percent(item.p2_success)} 有效报告</strong>
                  <p>{item.p2_valid} 有效 / {item.model_started} 已发起模型</p>
                  <dl>
                    <div><dt>仅证据</dt><dd>{item.evidence_only}</dd></div>
                    <div><dt>取消</dt><dd>{item.canceled}</dd></div>
                    <div><dt>契约拒绝</dt><dd>{item.contract_rejected}</dd></div>
                    <div><dt>依赖失败</dt><dd>{item.dependency_failed}</dd></div>
                    <div><dt>进行中</dt><dd>{item.pending}</dd></div>
                    <div><dt>费用未知</dt><dd>{item.unknown_cost}</dd></div>
                    <div><dt>反馈响应</dt><dd>{percent(item.feedback_response)}</dd></div>
                    <div><dt>反馈采纳</dt><dd>{percent(item.feedback_adoption)}</dd></div>
                    <div><dt>证据快照中位数</dt><dd>{duration(item.p0_mtti.median_ms)}</dd></div>
                    <div><dt>首次只读工具中位数</dt><dd>{duration(item.p1_mtti.median_ms)}</dd></div>
                    <div><dt>报告中位数</dt><dd>{duration(item.p2_mtti.median_ms)}</dd></div>
                    <div><dt>报告 P90</dt><dd>{duration(item.p2_mtti.p90_ms)}</dd></div>
                  </dl>
                </article>
              ))}
            </div>
          </section>
        </>
      ) : null}
    </main>
  );
}
