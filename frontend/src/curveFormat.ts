/**
 * Wording for everything a curve carries, in one testable place.
 *
 * The seven-plus failure kinds and the warnings exist so that each one sends the
 * reader somewhere different (CAP-12.7a). That only holds if the text actually
 * differs, so the mapping is data here and is asserted rather than left to
 * whoever writes the JSX.
 */

export type ExprOrigin = "RULES_API" | "GENERATOR_URL" | "TEMPLATE";
export type CurveTier = "THRESHOLD" | "METRIC" | "EXPRESSION_RESULT";
export type MetricTypeName =
  | "COUNTER"
  | "GAUGE"
  | "UNKNOWN_SUFFIX_GUESS"
  | "UNKNOWN";

/** Where the expression came from. Shown because it changes how far to trust it. */
export const EXPR_ORIGIN_TEXT: Record<ExprOrigin, string> = {
  RULES_API: "来自告警规则定义",
  GENERATOR_URL: "从告警链接推断",
  TEMPLATE: "来自你配置的指标模板",
};

/**
 * What the chart is actually showing.
 *
 * `EXPRESSION_RESULT` is deliberately **not** described as "condition true/false":
 * a plain PromQL comparison filters series rather than returning 0/1, so that
 * wording would describe a chart nobody is looking at.
 */
export const TIER_TEXT: Record<CurveTier, string> = {
  THRESHOLD: "告警表达式的取值",
  METRIC: "告警涉及的指标",
  EXPRESSION_RESULT: "告警表达式的返回结果",
};

export const METRIC_TYPE_TEXT: Record<MetricTypeName, string> = {
  COUNTER: "累计值，已按速率展示",
  GAUGE: "瞬时值",
  UNKNOWN_SUFFIX_GUESS: "类型按名称推断为累计值",
  UNKNOWN: "类型未知",
};

/** Each entry answers "what do I do now?" — that is the whole point of the split. */
export const FAILURE_TEXT: Record<string, string> = {
  THANOS_NOT_CONFIGURED: "这个来源还没有配置历史数据地址，去系统设置里补上",
  THANOS_UNREACHABLE: "读取历史数据失败，检查地址与网络（右侧代码说明是哪一种）",
  BUDGET_EXCEEDED: "查询超出了本地预算，没有发出",
  QUERY_SCOPE_UNSAFE: "这条表达式要扫描的时间范围过大，已拒绝执行",
  EXPR_UNAVAILABLE: "拿不到这条告警的表达式，画不出主曲线",
  EXPR_UNPARSEABLE: "表达式里的占位符在这条告警上没有对应标签",
  EXPR_AMBIGUOUS: "同名告警规则有多条，无法确定用哪一条",
  SOURCE_CONFIG_CHANGED: "读取期间来源配置变了，结果已丢弃，请重试",
  SERIES_LIMIT_EXCEEDED: "命中的曲线条数超过上限，已整体拒绝而不是只画一部分",
  METRIC_NOT_FOUND: "历史库里没有这个指标，检查指标名",
  LABEL_SET_NOT_FOUND: "指标存在，但这组标签下没有数据",
  NO_SAMPLES_IN_WINDOW: "这段时间窗内没有采样点，换个时间范围看看",
  EXPRESSION_NO_RESULT: "这段时间内告警条件从未成立",
};

/** Warnings mean the curve **is** there. They must not read like failures. */
export const WARNING_TEXT: Record<string, string> = {
  EXPR_FROM_GENERATOR_URL: "表达式取自告警链接而非规则定义",
  RULES_ENDPOINT_UNAVAILABLE: "读不到规则定义，已回退到告警链接",
  METRIC_TYPE_UNKNOWN: "指标类型未确认，按名称推断",
  LABEL_SCHEMA_GUESSED: "读不到该指标的标签集合，用了通用标签",
  COUNTER_AS_AUTHORED: "规则直接比较累计值，按原样执行未改写",
  SERIES_TRUNCATED_FOR_DISPLAY: "命中的内容多于可展示的数量",
  // Reads as "not applicable", not as "broken": the template matched on a label
  // *name* the alert happens to carry, and turned out not to be about this alert.
  AUXILIARY_NO_DATA: "这条指标模板对这条告警没有数据，已跳过",
};

export function failureText(kind: string): string {
  return FAILURE_TEXT[kind] ?? `无法取得这条曲线（${kind}）`;
}

export function warningText(kind: string): string {
  return WARNING_TEXT[kind] ?? `这条曲线有降级（${kind}）`;
}

/** Comparison direction, spelled out so a threshold line can be read. */
export function thresholdText(operator: string | null, value: number | null): string {
  if (value === null || value === undefined) return "";
  const symbol = operator ?? "";
  return symbol ? `阈值 ${symbol} ${formatValue(value)}` : `阈值 ${formatValue(value)}`;
}

/** Compact but honest: never rounds a small non-zero value to `0`. */
export function formatValue(value: number, unit = ""): string {
  if (!Number.isFinite(value)) return "—";
  const magnitude = Math.abs(value);
  let text: string;
  if (magnitude === 0) text = "0";
  else if (magnitude >= 1_000_000) text = `${(value / 1_000_000).toFixed(2)}M`;
  else if (magnitude >= 1000) text = `${(value / 1000).toFixed(2)}k`;
  else if (magnitude >= 1) text = value.toFixed(2);
  else if (magnitude >= 0.01) text = value.toFixed(3);
  // Below 0.01 a fixed rendering would show 0.00 and lose the fact that
  // something is happening at all.
  else text = value.toExponential(2);
  return unit ? `${text} ${unit}` : text;
}

export function formatTime(seconds: number): string {
  const date = new Date(seconds * 1000);
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/**
 * Axis tick text, scaled to the window like Grafana's.
 *
 * `08:00` on a same-day window is unambiguous; on a three-day window it appears
 * three times and tells you nothing about which day you are looking at.
 */
export function formatAxisTime(seconds: number, spanSeconds: number): string {
  const date = new Date(seconds * 1000);
  const time = date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (spanSeconds <= 24 * 3600) return time;
  const day = date.toLocaleDateString([], { month: "2-digit", day: "2-digit" });
  return `${day} ${time}`;
}

export interface SeriesStats {
  first: number;
  last: number;
  min: number;
  max: number;
}

/** last / min / max for a legend row. Null when there is nothing to report. */
export function seriesStats(
  points: readonly (readonly [number, number])[],
): SeriesStats | null {
  const values = points.map(([, y]) => y).filter((y) => Number.isFinite(y));
  if (values.length === 0) return null;
  return {
    first: values[0],
    last: values[values.length - 1],
    min: Math.min(...values),
    max: Math.max(...values),
  };
}

/** "3 分钟前" — how stale the reader is looking at, without doing the maths. */
export function dataAgeText(queriedAt: string | null | undefined, now = Date.now()): string {
  if (!queriedAt) return "";
  const at = Date.parse(queriedAt);
  if (Number.isNaN(at)) return "";
  const seconds = Math.max(0, Math.round((now - at) / 1000));
  if (seconds < 60) return "刚刚取数";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} 分钟前取数`;
  return `${Math.round(minutes / 60)} 小时前取数`;
}

/** A one-line summary readable without seeing the chart (keyboard, screen reader). */
export function seriesSummary(
  points: readonly (readonly [number, number])[],
  unit = "",
): string {
  const values = points.map(([, y]) => y).filter((y) => Number.isFinite(y));
  if (values.length === 0) return "这条曲线没有数据点";
  const min = Math.min(...values);
  const max = Math.max(...values);
  const last = values[values.length - 1];
  return `当前 ${formatValue(last, unit)}，最小 ${formatValue(min, unit)}，最大 ${formatValue(max, unit)}`;
}
