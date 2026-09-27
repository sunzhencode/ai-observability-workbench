/**
 * Pure geometry and formatting for metric curves.
 *
 * Everything a chart needs that is *not* rendering lives here, because this
 * repository's frontend runs vitest without a DOM (SYSTEM_SPEC §15.7): whatever
 * stays inside a component is only ever covered by the browser acceptance run.
 * Recharts draws; every decision it draws from is decided and tested in here.
 */

export type CurvePoint = readonly [seconds: number, value: number];

/** One chart row: `x` is unix seconds, `y` is null across a gap. */
export interface ChartRow {
  x: number;
  [seriesKey: string]: number | null;
}

export interface SeriesInput {
  readonly labels: Readonly<Record<string, string>>;
  readonly points: readonly CurvePoint[];
}

/**
 * A sample is "missing" once the distance to the previous one exceeds this many
 * steps. One step of slack absorbs ordinary scrape jitter; beyond that the store
 * genuinely had nothing, and joining across it would draw a line through data
 * that does not exist.
 */
export const GAP_STEP_FACTOR = 1.8;

/**
 * Label names whose values differ across the series on one chart.
 *
 * These are the only ones worth printing in a legend: everything the series have
 * in common is context the chart header already carries. Printing the full label
 * set instead produces the three-line wrapped legend this replaced — technically
 * complete, unreadable in practice, and identical on every row.
 */
export function distinguishingLabels(series: readonly SeriesInput[]): string[] {
  if (series.length <= 1) return [];
  // Distinct *values* and how many series carry the label are two different
  // counts. Conflating them marks a label every series shares at one value as
  // distinguishing, which puts the whole common prefix back in the legend.
  const seen = new Map<string, { values: Set<string>; carriers: number }>();
  for (const item of series) {
    for (const [name, value] of Object.entries(item.labels)) {
      if (name === "__name__") continue;
      const entry = seen.get(name) ?? { values: new Set<string>(), carriers: 0 };
      entry.values.add(value);
      entry.carriers += 1;
      seen.set(name, entry);
    }
  }
  return [...seen.entries()]
    .filter(([, entry]) => entry.values.size > 1 || entry.carriers < series.length)
    .map(([name]) => name)
    .sort();
}

/**
 * What to print for one series in a legend.
 *
 * With several series, only what tells them apart. With one, a short identity
 * rather than the whole label set — there is nothing to tell it apart *from*.
 */
export function seriesDisplayName(
  series: readonly SeriesInput[],
  index: number,
  fallback = "",
): string {
  const item = series[index];
  if (item === undefined) return fallback;
  const names = distinguishingLabels(series);
  if (names.length > 0) {
    const parts = names
      .filter((name) => item.labels[name] !== undefined)
      .map((name) => `${name}=${item.labels[name]}`);
    if (parts.length > 0) return parts.join(" ");
  }
  // Single series (or all identical): prefer the labels a human identifies an
  // object by, in a fixed order, and stop at two so the legend stays one line.
  const preferred = ["pod", "instance", "node", "container", "namespace", "cluster", "job"];
  const parts = preferred
    .filter((name) => item.labels[name])
    .slice(0, 2)
    .map((name) => `${name}=${item.labels[name]}`);
  if (parts.length > 0) return parts.join(" ");
  return fallback || seriesKey(item.labels, index);
}

/** Chart-friendly key for one series, stable across renders. */
export function seriesKey(labels: Readonly<Record<string, string>>, index: number): string {
  const parts = Object.keys(labels)
    .filter((name) => name !== "__name__")
    .sort()
    .map((name) => `${name}=${labels[name]}`);
  return parts.length > 0 ? parts.join(",") : `series ${index + 1}`;
}

/**
 * Merge series onto a shared time axis, inserting explicit gaps.
 *
 * **Missing samples stay missing.** Turning them into zeros would be
 * indistinguishable from a real zero — the single most misleading thing a
 * monitoring chart can do — and joining across them invents a trend that was
 * never measured.
 */
export function toChartRows(
  series: readonly SeriesInput[],
  stepSeconds: number,
): { rows: ChartRow[]; keys: string[] } {
  const keys = series.map((item, index) => seriesKey(item.labels, index));
  const byX = new Map<number, ChartRow>();

  series.forEach((item, index) => {
    const key = keys[index];
    let previousX: number | null = null;
    for (const [x, y] of item.points) {
      if (previousX !== null && stepSeconds > 0) {
        const gap = x - previousX;
        if (gap > stepSeconds * GAP_STEP_FACTOR) {
          // One explicit null immediately after the last real sample is what
          // makes Recharts break the line instead of bridging it.
          const marker = previousX + stepSeconds;
          const row = byX.get(marker) ?? { x: marker };
          row[key] = null;
          byX.set(marker, row);
        }
      }
      const row = byX.get(x) ?? { x };
      row[key] = y;
      byX.set(x, row);
      previousX = x;
    }
  });

  const rows = [...byX.values()].sort((a, b) => a.x - b.x);
  // A series absent from a row must be null, not undefined: undefined lets the
  // chart connect straight through it.
  for (const row of rows) {
    for (const key of keys) {
      if (!(key in row)) row[key] = null;
    }
  }
  return { rows, keys };
}

/**
 * Y range covering the data *and* the threshold, padded slightly.
 *
 * The threshold has to be inside the domain or the line silently falls off the
 * chart — and a threshold you cannot see is worse than none, because the curve
 * then looks unremarkable.
 */
export function valueDomain(
  series: readonly SeriesInput[],
  threshold: number | null,
): [number, number] {
  const values: number[] = [];
  for (const item of series) for (const [, y] of item.points) values.push(y);
  if (threshold !== null && Number.isFinite(threshold)) values.push(threshold);
  if (values.length === 0) return [0, 1];

  let min = Math.min(...values);
  let max = Math.max(...values);
  if (min === max) {
    // A flat line drawn edge-to-edge reads as "no data". Give it room.
    const magnitude = Math.abs(min) || 1;
    min -= magnitude * 0.1;
    max += magnitude * 0.1;
  } else {
    const padding = (max - min) * 0.08;
    min -= padding;
    max += padding;
  }
  if (min > 0 && min < (max - min) * 0.5) min = 0; // keep zero in view when close
  return niceDomain(min, max);
}

/**
 * Round a range outwards to values a person reads without effort.
 *
 * Grafana never shows an axis labelled `-0.080 / 0.220 / 0.520 / 0.820 / 1.08`,
 * and neither should this: padding the data by a percentage produces exactly
 * that, and the reader then spends attention decoding tick labels instead of
 * looking at the line.
 */
export function niceDomain(min: number, max: number): [number, number] {
  if (!Number.isFinite(min) || !Number.isFinite(max) || max <= min) return [0, 1];
  const step = niceStep((max - min) / 4);
  const low = Math.floor(min / step) * step;
  const high = Math.ceil(max / step) * step;
  // Floating point leaves 0.30000000000000004 behind; the axis must not show it.
  const decimals = Math.max(0, Math.ceil(-Math.log10(step)) + 1);
  return [round(low, decimals), round(high, decimals)];
}

/** 1, 2, 2.5 or 5 times a power of ten — the steps people count in. */
function niceStep(raw: number): number {
  if (!(raw > 0)) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  const normalised = raw / magnitude;
  const rung = normalised <= 1 ? 1 : normalised <= 2 ? 2 : normalised <= 2.5 ? 2.5 : normalised <= 5 ? 5 : 10;
  return rung * magnitude;
}

function round(value: number, decimals: number): number {
  const factor = 10 ** decimals;
  return Math.round(value * factor) / factor;
}

/**
 * Grafana's classic palette, in its own order.
 *
 * Deterministic by index so a redraw never reshuffles which series is which
 * colour — and familiar, because the person reading this has been reading
 * Grafana charts all day.
 */
export const SERIES_COLORS = [
  "#7EB26D",
  "#EAB839",
  "#6ED0E0",
  "#EF843C",
  "#E24D42",
  "#1F78C1",
  "#BA43A9",
  "#705DA0",
] as const;

export function seriesColor(index: number): string {
  return SERIES_COLORS[index % SERIES_COLORS.length];
}

/**
 * The series most likely to be the one the alert is about.
 *
 * Ranked by how many of the alert's own labels they carry, ties broken by label
 * text so the order never depends on map iteration. Used to decide which few to
 * show by default — the rest stay one click away rather than disappearing.
 */
export function rankSeriesByAlertLabels(
  series: readonly SeriesInput[],
  alertLabels: Readonly<Record<string, string>>,
): number[] {
  return series
    .map((item, index) => {
      let score = 0;
      for (const [name, value] of Object.entries(alertLabels)) {
        if (item.labels[name] === value) score += 1;
      }
      return { index, score, text: seriesKey(item.labels, index) };
    })
    .sort((a, b) => b.score - a.score || a.text.localeCompare(b.text))
    .map((entry) => entry.index);
}

export const DEFAULT_VISIBLE_SERIES = 5;
