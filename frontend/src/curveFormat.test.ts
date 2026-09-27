import { describe, expect, it } from "vitest";

import {
  EXPR_ORIGIN_TEXT,
  FAILURE_TEXT,
  METRIC_TYPE_TEXT,
  TIER_TEXT,
  WARNING_TEXT,
  dataAgeText,
  failureText,
  formatAxisTime,
  formatValue,
  seriesStats,
  seriesSummary,
  thresholdText,
  warningText,
} from "./curveFormat";

describe("failure wording", () => {
  it("gives every kind its own text", () => {
    // The split into many kinds only pays off if the words differ — otherwise
    // it is "failed to load" wearing more enums.
    const texts = Object.values(FAILURE_TEXT);
    expect(new Set(texts).size).toBe(texts.length);
  });

  it("distinguishes a missing metric from an unreachable store", () => {
    // These send the reader to completely different places.
    expect(FAILURE_TEXT.METRIC_NOT_FOUND).not.toBe(FAILURE_TEXT.THANOS_UNREACHABLE);
  });

  it("treats a never-satisfied condition as information, not a fault", () => {
    expect(FAILURE_TEXT.EXPRESSION_NO_RESULT).toContain("从未成立");
    expect(FAILURE_TEXT.EXPRESSION_NO_RESULT).not.toContain("失败");
  });

  it("never leaves an unknown kind unexplained", () => {
    expect(failureText("SOMETHING_NEW")).toContain("SOMETHING_NEW");
  });
});

describe("warning wording", () => {
  it("does not read like a failure", () => {
    // A curve that is on screen must not be captioned "unreachable".
    for (const text of Object.values(WARNING_TEXT)) {
      expect(text).not.toContain("失败");
      expect(text).not.toContain("无法");
    }
  });

  it("has no kind in common with failures", () => {
    const overlap = Object.keys(WARNING_TEXT).filter((kind) => kind in FAILURE_TEXT);
    expect(overlap).toEqual([]);
  });

  it("explains an unknown kind rather than dropping it", () => {
    expect(warningText("NEW_ONE")).toContain("NEW_ONE");
  });
});

describe("tier wording", () => {
  it("never claims the last tier is a boolean", () => {
    // PromQL's plain comparison filters series; it does not return 0/1.
    expect(TIER_TEXT.EXPRESSION_RESULT).not.toContain("成立");
    expect(TIER_TEXT.EXPRESSION_RESULT).not.toContain("是否");
  });

  it("covers every tier and origin", () => {
    expect(Object.keys(TIER_TEXT).sort()).toEqual([
      "EXPRESSION_RESULT",
      "METRIC",
      "THRESHOLD",
    ]);
    expect(Object.keys(EXPR_ORIGIN_TEXT).sort()).toEqual([
      "GENERATOR_URL",
      "RULES_API",
      "TEMPLATE",
    ]);
  });

  it("says out loud when a metric type was guessed", () => {
    expect(METRIC_TYPE_TEXT.UNKNOWN_SUFFIX_GUESS).toContain("推断");
  });
});

describe("thresholdText", () => {
  it("keeps the comparison direction", () => {
    // Without it the line is unreadable: above or below is the whole meaning.
    expect(thresholdText("<", 0.1)).toContain("<");
    expect(thresholdText(">", 90)).toContain(">");
  });

  it("is empty when there is no threshold", () => {
    expect(thresholdText("<", null)).toBe("");
  });

  it("still renders without an operator", () => {
    expect(thresholdText(null, 5)).toContain("5");
  });
});

describe("formatValue", () => {
  it("never rounds a small non-zero value down to zero", () => {
    // "0.00" would say nothing is happening when something is.
    expect(formatValue(0.0001)).not.toBe("0.00");
    expect(formatValue(0.0001)).not.toBe("0");
  });

  it("keeps a real zero as zero", () => {
    expect(formatValue(0)).toBe("0");
  });

  it("compacts large numbers", () => {
    expect(formatValue(1_500_000)).toBe("1.50M");
    expect(formatValue(2500)).toBe("2.50k");
  });

  it("appends a unit when there is one", () => {
    expect(formatValue(1, "bytes")).toBe("1.00 bytes");
    expect(formatValue(1)).toBe("1.00");
  });

  it("does not crash on non-finite values", () => {
    expect(formatValue(Number.NaN)).toBe("—");
    expect(formatValue(Number.POSITIVE_INFINITY)).toBe("—");
  });
});

describe("dataAgeText", () => {
  const now = Date.parse("2026-07-30T12:00:00Z");

  it("says how stale the chart is", () => {
    expect(dataAgeText("2026-07-30T11:55:00Z", now)).toContain("5 分钟");
    expect(dataAgeText("2026-07-30T11:59:50Z", now)).toContain("刚刚");
    expect(dataAgeText("2026-07-30T09:00:00Z", now)).toContain("小时");
  });

  it("is empty rather than wrong when the timestamp is unusable", () => {
    expect(dataAgeText(null, now)).toBe("");
    expect(dataAgeText("not a date", now)).toBe("");
  });
});

describe("seriesSummary", () => {
  it("reads the chart out without seeing it", () => {
    const text = seriesSummary([
      [0, 1],
      [1, 5],
      [2, 3],
    ]);
    expect(text).toContain("当前");
    expect(text).toContain("最小");
    expect(text).toContain("最大");
  });

  it("says so when there is nothing to summarise", () => {
    expect(seriesSummary([])).toContain("没有数据点");
  });
});

describe("formatAxisTime", () => {
  const at = Date.parse("2026-07-30T14:05:00Z") / 1000;

  it("shows only the time within a day", () => {
    expect(formatAxisTime(at, 6 * 3600)).not.toMatch(/\d\d[-/]\d\d/);
  });

  it("adds the date once the window spans more than a day", () => {
    // "08:00" appears three times on a three-day window and says nothing about
    // which day you are looking at.
    expect(formatAxisTime(at, 3 * 24 * 3600)).toMatch(/\d\d[-/]\d\d/);
  });
});

describe("seriesStats", () => {
  it("reports last, min and max", () => {
    const stats = seriesStats([
      [0, 1],
      [1, 5],
      [2, 3],
    ]);
    expect(stats).toEqual({ first: 1, last: 3, min: 1, max: 5 });
  });

  it("ignores non-finite values rather than poisoning the range", () => {
    const stats = seriesStats([
      [0, 1],
      [1, Number.NaN],
      [2, 3],
    ]);
    expect(stats).toEqual({ first: 1, last: 3, min: 1, max: 3 });
  });

  it("is null when there is nothing to report", () => {
    expect(seriesStats([])).toBeNull();
  });
});
