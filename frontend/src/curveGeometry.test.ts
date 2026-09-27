import { describe, expect, it } from "vitest";

import {
  DEFAULT_VISIBLE_SERIES,
  GAP_STEP_FACTOR,
  SERIES_COLORS,
  rankSeriesByAlertLabels,
  distinguishingLabels,
  niceDomain,
  seriesColor,
  seriesDisplayName,
  seriesKey,
  toChartRows,
  valueDomain,
} from "./curveGeometry";

const series = (labels: Record<string, string>, points: [number, number][]) => ({
  labels,
  points,
});

describe("seriesKey", () => {
  it("is stable regardless of label order", () => {
    expect(seriesKey({ b: "2", a: "1" }, 0)).toBe(seriesKey({ a: "1", b: "2" }, 0));
  });

  it("drops __name__, which every series carries and none is identified by", () => {
    expect(seriesKey({ __name__: "up", pod: "a" }, 0)).toBe("pod=a");
  });

  it("falls back to a position when a series has no labels", () => {
    expect(seriesKey({}, 2)).toBe("series 3");
  });
});

describe("toChartRows", () => {
  it("merges series onto one time axis", () => {
    const { rows, keys } = toChartRows(
      [series({ pod: "a" }, [[10, 1]]), series({ pod: "b" }, [[10, 2]])],
      10,
    );
    expect(keys).toEqual(["pod=a", "pod=b"]);
    expect(rows).toEqual([{ x: 10, "pod=a": 1, "pod=b": 2 }]);
  });

  it("keeps a missing sample missing instead of turning it into zero", () => {
    // A fabricated zero is indistinguishable from a real one — the single most
    // misleading thing a monitoring chart can do.
    const { rows } = toChartRows([series({}, [[0, 5], [100, 7]])], 10);
    const values = rows.map((row) => row["series 1"]);
    expect(values).toContain(null);
    expect(values).not.toContain(0);
  });

  it("breaks the line across a gap rather than joining through it", () => {
    const { rows } = toChartRows([series({}, [[0, 1], [1000, 2]])], 10);
    const gapRow = rows.find((row) => row["series 1"] === null);
    expect(gapRow).toBeDefined();
    expect(gapRow!.x).toBeGreaterThan(0);
    expect(gapRow!.x).toBeLessThan(1000);
  });

  it("tolerates ordinary scrape jitter without inventing a gap", () => {
    const step = 10;
    const jitter = step * (GAP_STEP_FACTOR - 0.3);
    const { rows } = toChartRows([series({}, [[0, 1], [jitter, 2]])], step);
    expect(rows.every((row) => row["series 1"] !== null)).toBe(true);
  });

  it("marks a series absent from a timestamp as null, not undefined", () => {
    // `undefined` lets the chart connect straight through the missing point.
    const { rows } = toChartRows(
      [series({ pod: "a" }, [[10, 1]]), series({ pod: "b" }, [[20, 2]])],
      10,
    );
    const first = rows.find((row) => row.x === 10)!;
    expect(first["pod=b"]).toBeNull();
    expect("pod=b" in first).toBe(true);
  });

  it("sorts rows by time", () => {
    const { rows } = toChartRows([series({}, [[30, 1], [10, 2], [20, 3]])], 10);
    expect(rows.map((row) => row.x)).toEqual([10, 20, 30]);
  });

  it("handles an empty series list", () => {
    expect(toChartRows([], 10)).toEqual({ rows: [], keys: [] });
  });
});

describe("valueDomain", () => {
  it("keeps the threshold inside the range", () => {
    // A threshold line outside the domain silently vanishes, and the curve then
    // looks unremarkable.
    const [min, max] = valueDomain([series({}, [[0, 1], [1, 2]])], 100);
    expect(max).toBeGreaterThanOrEqual(100);
    expect(min).toBeLessThanOrEqual(1);
  });

  it("gives a flat line room to be seen", () => {
    const [min, max] = valueDomain([series({}, [[0, 5], [1, 5]])], null);
    expect(min).toBeLessThan(5);
    expect(max).toBeGreaterThan(5);
  });

  it("falls back to a usable range with no data", () => {
    expect(valueDomain([], null)).toEqual([0, 1]);
  });

  it("ignores a non-finite threshold", () => {
    const [, max] = valueDomain([series({}, [[0, 1]])], Number.NaN);
    expect(Number.isFinite(max)).toBe(true);
  });
});

describe("seriesColor", () => {
  it("is deterministic and wraps", () => {
    expect(seriesColor(0)).toBe(seriesColor(SERIES_COLORS.length));
    expect(seriesColor(1)).not.toBe(seriesColor(0));
  });

  it("has no repeated colour, so two series are never the same shade", () => {
    expect(new Set(SERIES_COLORS).size).toBe(SERIES_COLORS.length);
  });
});

describe("rankSeriesByAlertLabels", () => {
  it("puts the series matching the alert first", () => {
    const order = rankSeriesByAlertLabels(
      [series({ pod: "other" }, []), series({ pod: "web-0" }, [])],
      { pod: "web-0" },
    );
    expect(order[0]).toBe(1);
  });

  it("breaks ties deterministically rather than by map order", () => {
    const input = [series({ pod: "b" }, []), series({ pod: "a" }, [])];
    expect(rankSeriesByAlertLabels(input, {})).toEqual(
      rankSeriesByAlertLabels(input, {}),
    );
    expect(rankSeriesByAlertLabels(input, {})[0]).toBe(1); // "pod=a" sorts first
  });

  it("returns every index exactly once", () => {
    const input = [series({}, []), series({}, []), series({}, [])];
    expect([...rankSeriesByAlertLabels(input, {})].sort()).toEqual([0, 1, 2]);
  });

  it("leaves room to show a few by default without hiding the rest", () => {
    expect(DEFAULT_VISIBLE_SERIES).toBeGreaterThan(0);
  });
});

describe("legend naming", () => {
  it("prints only what tells the series apart", () => {
    // The full label set is identical on every row and wraps to three lines —
    // complete, and useless.
    const input = [
      series({ cluster: "c", pod: "a", job: "j" }, []),
      series({ cluster: "c", pod: "b", job: "j" }, []),
    ];
    expect(distinguishingLabels(input)).toEqual(["pod"]);
    expect(seriesDisplayName(input, 0)).toBe("pod=a");
    expect(seriesDisplayName(input, 1)).toBe("pod=b");
  });

  it("uses a short identity when there is only one series", () => {
    const input = [
      series({ cluster: "uat", pod: "web-0", color: "yellow", job: "j" }, []),
    ];
    const name = seriesDisplayName(input, 0);
    expect(name).toContain("pod=web-0");
    expect(name).not.toContain("job=");
    expect(name.split(" ").length).toBeLessThanOrEqual(2);
  });

  it("treats a label present on only some series as distinguishing", () => {
    const input = [series({ pod: "a" }, []), series({ pod: "a", extra: "x" }, [])];
    expect(distinguishingLabels(input)).toContain("extra");
  });

  it("never shows __name__, which every series carries", () => {
    const input = [
      series({ __name__: "m", pod: "a" }, []),
      series({ __name__: "m", pod: "b" }, []),
    ];
    expect(distinguishingLabels(input)).toEqual(["pod"]);
  });

  it("falls back rather than returning nothing", () => {
    expect(seriesDisplayName([series({}, [])], 0, "fallback")).toBe("fallback");
    expect(seriesDisplayName([], 0, "fallback")).toBe("fallback");
  });
});

describe("niceDomain", () => {
  it("turns a padded 0/1 range into round numbers", () => {
    // The 8% padding produced -0.08 .. 1.08, and the axis then read
    // "-0.080 / 0.220 / 0.520 / 0.820 / 1.08" — decodable, but nobody should
    // have to.
    expect(niceDomain(-0.08, 1.08)).toEqual([-0.5, 1.5]);
  });

  it("never leaves floating point noise in a bound", () => {
    for (const [lo, hi] of [
      [0.1, 0.3],
      [-2.7, 8.1],
      [0, 0.007],
      [1000, 100000],
    ] as const) {
      const [low, high] = niceDomain(lo, hi);
      expect(String(low)).not.toMatch(/\d{8,}/);
      expect(String(high)).not.toMatch(/\d{8,}/);
    }
  });

  it("always contains the input range", () => {
    for (const [lo, hi] of [
      [0.1, 0.3],
      [-5, 5],
      [12, 13],
      [0, 1],
    ] as const) {
      const [low, high] = niceDomain(lo, hi);
      expect(low).toBeLessThanOrEqual(lo);
      expect(high).toBeGreaterThanOrEqual(hi);
    }
  });

  it("falls back rather than producing an unusable axis", () => {
    expect(niceDomain(5, 5)).toEqual([0, 1]);
    expect(niceDomain(Number.NaN, 1)).toEqual([0, 1]);
  });

  it("is what valueDomain returns", () => {
    const [low, high] = valueDomain([series({}, [[0, 0], [1, 1]])], null);
    expect(Number.isInteger(low * 100)).toBe(true);
    expect(Number.isInteger(high * 100)).toBe(true);
  });
});
