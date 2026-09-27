/**
 * One metric curve, drawn the way the reader already knows how to read.
 *
 * Recharts rather than hand-drawn SVG, and the reason is stage 1's whole purpose
 * (design D2/D41): the user has to confirm with their own eyes that these curves
 * are *correct* before any model is allowed to read them. A picture with no axis
 * ticks and no readable values cannot support that.
 *
 * The visual language is Grafana's on purpose — gradient area under the line,
 * faint grid, crosshair, dark tooltip, a legend that carries last/min/max. The
 * person looking at this has been reading Grafana all day; familiar beats novel.
 *
 * Everything decidable lives in `curveGeometry.ts` / `curveFormat.ts`, which run
 * under vitest; this file is only the drawing, which the browser acceptance run
 * covers.
 */
import { useMemo, useState } from "react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import {
  DEFAULT_VISIBLE_SERIES,
  rankSeriesByAlertLabels,
  seriesColor,
  seriesDisplayName,
  seriesKey,
  toChartRows,
  valueDomain,
} from "../../curveGeometry";
import {
  EXPR_ORIGIN_TEXT,
  METRIC_TYPE_TEXT,
  TIER_TEXT,
  dataAgeText,
  formatAxisTime,
  formatValue,
  seriesStats,
  thresholdText,
} from "../../curveFormat";
import type { MetricCurve as Curve } from "../../types";

interface Props {
  curve: Curve;
  alertLabels: Record<string, string>;
  alertStartsAt: string | null;
}

interface TooltipPayloadEntry {
  dataKey?: string | number;
  value?: number | string | null;
  color?: string;
}

export function MetricCurve({ curve, alertLabels, alertStartsAt }: Props) {
  const [showAll, setShowAll] = useState(false);

  const series = useMemo(
    () =>
      curve.series.map((item) => ({
        labels: item.labels,
        points: item.points.map(([x, y]) => [x, y] as const),
      })),
    [curve.series],
  );

  const ranked = useMemo(
    () => rankSeriesByAlertLabels(series, alertLabels),
    [series, alertLabels],
  );
  const visibleIndexes = showAll ? ranked : ranked.slice(0, DEFAULT_VISIBLE_SERIES);
  const hiddenCount = ranked.length - visibleIndexes.length;

  const visible = useMemo(
    () => visibleIndexes.map((index) => series[index]),
    [visibleIndexes, series],
  );
  const { rows, keys } = useMemo(
    () => toChartRows(visible, curve.step_seconds),
    [visible, curve.step_seconds],
  );
  const domain = useMemo(
    () => valueDomain(visible, curve.threshold),
    [visible, curve.threshold],
  );

  const spanSeconds = useMemo(() => {
    if (rows.length < 2) return curve.step_seconds;
    return rows[rows.length - 1].x - rows[0].x;
  }, [rows, curve.step_seconds]);

  const alertAt = alertStartsAt ? Date.parse(alertStartsAt) / 1000 : null;
  const unit = curve.display_unit;
  const gradientId = `curve-fill-${curve.curve_id}`;

  return (
    <section className="card metric-curve" aria-label={curve.title}>
      <header className="metric-curve__head">
        <h4>{curve.title}</h4>
        {/* Text, never colour alone (CAP-08). */}
        <p className="dim metric-curve__meta">
          <span>{EXPR_ORIGIN_TEXT[curve.expr_origin]}</span>
          {curve.tier ? <span> · {TIER_TEXT[curve.tier]}</span> : null}
          <span> · {METRIC_TYPE_TEXT[curve.metric_type]}</span>
          {curve.queried_at ? <span> · {dataAgeText(curve.queried_at)}</span> : null}
        </p>
        <details className="metric-curve__query">
          <summary className="dim">查询语句</summary>
          <code>{curve.query}</code>
        </details>
      </header>

      <div className="metric-curve__chart">
        <ResponsiveContainer width="100%" height={216}>
          <AreaChart data={rows} margin={{ top: 10, right: 16, bottom: 0, left: 0 }}>
            <defs>
              {keys.map((key, index) => (
                <linearGradient
                  key={key}
                  id={`${gradientId}-${index}`}
                  x1="0"
                  y1="0"
                  x2="0"
                  y2="1"
                >
                  <stop offset="0%" stopColor={seriesColor(index)} stopOpacity={0.26} />
                  <stop offset="95%" stopColor={seriesColor(index)} stopOpacity={0.02} />
                </linearGradient>
              ))}
            </defs>
            {/* Horizontal only, and faint: the grid is a reading aid, not content. */}
            <CartesianGrid vertical={false} stroke="#e9edf3" strokeDasharray="0" />
            <XAxis
              dataKey="x"
              type="number"
              domain={["dataMin", "dataMax"]}
              tickFormatter={(value: number) => formatAxisTime(value, spanSeconds)}
              tick={{ fontSize: 11, fill: "#8792a2" }}
              tickLine={false}
              axisLine={{ stroke: "#e9edf3" }}
              minTickGap={48}
            />
            <YAxis
              domain={domain}
              tickFormatter={(value: number) => formatValue(value, unit)}
              tick={{ fontSize: 11, fill: "#8792a2" }}
              tickLine={false}
              axisLine={false}
              width={68}
            />
            <Tooltip
              cursor={{ stroke: "#98a2b3", strokeWidth: 1, strokeDasharray: "3 3" }}
              isAnimationActive={false}
              content={({ active, payload, label }) => {
                if (!active || !payload || payload.length === 0) return null;
                return (
                  <div className="metric-tooltip">
                    <div className="metric-tooltip__time">
                      {typeof label === "number" ? formatAxisTime(label, 0) : ""}
                    </div>
                    {(payload as readonly unknown[] as readonly TooltipPayloadEntry[]).map((entry, index) => (
                      <div className="metric-tooltip__row" key={String(entry.dataKey)}>
                        <span
                          className="metric-tooltip__dot"
                          style={{ background: entry.color ?? seriesColor(index) }}
                        />
                        <span className="metric-tooltip__name">
                          {seriesDisplayName(visible, index, String(entry.dataKey))}
                        </span>
                        <span className="metric-tooltip__value">
                          {typeof entry.value === "number"
                            ? formatValue(entry.value, unit)
                            : "无数据"}
                        </span>
                      </div>
                    ))}
                  </div>
                );
              }}
            />
            {alertAt !== null ? (
              <ReferenceLine
                x={alertAt}
                stroke="#e5484d"
                strokeDasharray="4 3"
                label={{
                  value: "告警触发",
                  fontSize: 11,
                  fill: "#c5313a",
                  position: "insideTopRight",
                }}
              />
            ) : null}
            {curve.threshold !== null ? (
              <ReferenceLine
                y={curve.threshold}
                stroke="#e08600"
                strokeDasharray="6 4"
                label={{
                  value: thresholdText(curve.threshold_operator, curve.threshold),
                  fontSize: 11,
                  fill: "#b96600",
                  position: "insideBottomRight",
                }}
              />
            ) : null}
            {keys.map((key, index) => (
              <Area
                key={key}
                type="monotone"
                dataKey={key}
                stroke={seriesColor(index)}
                strokeWidth={1.6}
                fill={`url(#${gradientId}-${index})`}
                dot={false}
                activeDot={{ r: 3, strokeWidth: 0 }}
                // Gaps stay gaps: joining across missing samples would invent a
                // trend that was never measured.
                connectNulls={false}
                isAnimationActive={false}
              />
            ))}
          </AreaChart>
        </ResponsiveContainer>
      </div>

      {/* Grafana's legend-as-table: the numbers are why anyone reads a legend.
          Also the accessible path — focusable rows carry the same values the
          chart does, for keyboard and screen readers. */}
      <table className="metric-legend">
        <thead>
          <tr>
            <th scope="col">序列</th>
            <th scope="col">当前</th>
            <th scope="col">最小</th>
            <th scope="col">最大</th>
          </tr>
        </thead>
        <tbody>
          {visible.map((item, index) => {
            const stats = seriesStats(item.points);
            return (
              <tr key={seriesKey(item.labels, index)} tabIndex={0}>
                <th scope="row">
                  <span
                    className="metric-legend__swatch"
                    style={{ background: seriesColor(index) }}
                  />
                  {seriesDisplayName(visible, index, `序列 ${index + 1}`)}
                </th>
                <td>{stats ? formatValue(stats.last, unit) : "—"}</td>
                <td>{stats ? formatValue(stats.min, unit) : "—"}</td>
                <td>{stats ? formatValue(stats.max, unit) : "—"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>

      {hiddenCount > 0 ? (
        <button type="button" className="link" onClick={() => setShowAll(true)}>
          还有 {hiddenCount} 条曲线未显示，展开
        </button>
      ) : null}
      {showAll && ranked.length > DEFAULT_VISIBLE_SERIES ? (
        <button type="button" className="link" onClick={() => setShowAll(false)}>
          只看最相关的 {DEFAULT_VISIBLE_SERIES} 条
        </button>
      ) : null}
    </section>
  );
}
