import { describe, expect, it } from "vitest";
import {
  analystActionLabel,
  analystEvidenceGainMessage,
  analystEvidenceReferenceLabel,
  analystRecommendationLabel,
  analystVerdictLabel,
  investigationContinuationMessage,
  investigationFindingText,
  investigatorConfidenceAuditLabel,
  investigatorEvidenceReferenceLabel,
  investigatorMetricEvidencePresentation,
  investigatorVerdictLabel,
  metricObservationPresentation,
  uniqueGlobalMissingEvidence,
  INVESTIGATION_STAGE_TITLES,
} from "./investigationPresentation";

describe("investigation presentation", () => {
  it("presents V2 reports as uncalibrated model advice", () => {
    expect(investigatorVerdictLabel("LIKELY_INCIDENT")).toBe("模型判断：可能是事件");
    expect(investigatorVerdictLabel("INCIDENT_CONFIRMED")).toBe(
      "旧版模型判断：事件证据充分",
    );
    expect(investigatorConfidenceAuditLabel(0.95)).toBe("模型自评分（未校准）95%");
  });

  it("never labels a legacy empty attribution as evidence-backed", () => {
    expect(investigatorEvidenceReferenceLabel(["metric-1"])).toBe("有证据引用");
    expect(investigatorEvidenceReferenceLabel([])).toBe("无证据引用");
  });

  it("presents the persisted L1 point count and never exposes more than bounded L2", () => {
    const presentation = investigatorMetricEvidencePresentation({
      evidence_id: "metric-1",
      metric_id: "checkout_http_error_ratio",
      status: "DATA",
      summary: { latest: 0.08, point_count: 61 },
      sample: Array.from({ length: 25 }, (_, index) => ({
        timestamp: index + 1,
        value: String(index),
      })),
    });
    expect(presentation.pointCount).toBe("61 个点");
    expect(presentation.sample).toHaveLength(20);
    expect(presentation.sample.at(-1)).toEqual({ timestamp: 20, value: "19" });
  });

  it("translates Analyst protocol enums into operator language", () => {
    expect(analystVerdictLabel("SUPPORTED")).toBe("现有证据支持");
    expect(analystVerdictLabel("BLOCKED")).toBe("证据仍不足");
    expect(analystActionLabel("MANUAL_MITIGATION")).toBe("人工缓解建议");
    expect(analystRecommendationLabel(0, "NEXT_CHECK")).toBe("优先建议 · 继续核对");
    expect(analystRecommendationLabel(1, "NEXT_CHECK")).toBe("后续建议 · 继续核对");
  });

  it("labels alert and metric evidence by ownership instead of using a generic fallback", () => {
    expect(analystEvidenceReferenceLabel(
      "alert-1",
      [{ evidence_ref: "alert-1", alertname: "LocalHighErrorRate" }],
      [],
    )).toBe("告警事实：LocalHighErrorRate");
    expect(analystEvidenceReferenceLabel(
      "metric-1",
      [],
      [{ evidence_ref: "metric-1", metric_name: "checkout_http_error_ratio" }],
    )).toBe("指标证据：checkout_http_error_ratio");
  });

  it("makes zero evidence gain visible beside the conclusion", () => {
    expect(analystEvidenceGainMessage(0)).toBe(
      "本轮 AI 扩展未取得基础证据之外的新事实；结论仅基于已冻结的告警和基础证据。",
    );
    expect(analystEvidenceGainMessage(2)).toBeNull();
  });

  it("does not repeat hypothesis-level missing evidence in the global list", () => {
    expect(uniqueGlobalMissingEvidence(
      ["请求日志", "部署记录", "请求日志"],
      [["请求日志"], ["依赖指标"]],
    )).toEqual(["部署记录"]);
  });
  it("uses task language instead of internal stage codes", () => {
    expect(INVESTIGATION_STAGE_TITLES).toEqual({
      foundation: "基础证据",
      expansion: "AI 扩展调查",
      conclusion: "AI 调查结论",
    });
    expect(Object.values(INVESTIGATION_STAGE_TITLES).join(" ")).not.toMatch(/P[012]/);
  });

  it("translates stored legacy stage findings without rewriting audit data", () => {
    expect(
      investigationFindingText("P1 证据扩展已记录；当前批次尚未生成模型判断"),
    ).toBe("AI 扩展调查已记录；当前尚未生成调查结论");
    expect(
      investigationFindingText("P0 证据简报已保留；后续证据扩展未启动"),
    ).toBe("基础证据已保留；AI 扩展调查未启动");
  });

  it("explains why evidence-only did not continue and what to do next", () => {
    expect(
      investigationContinuationMessage({
        status: "EVIDENCE_ONLY",
        plannerStepCount: 0,
        degradations: [
          {
            domain: "metrics",
            code: "SOURCE_UNAVAILABLE",
            message: "指标源读取未完成",
            impact: "本次没有取得该指标源的新证据。",
            preserved: "告警范围仍保留。",
            next_step: "核对指标连接。",
          },
        ],
        terminationReason: "METRIC_SOURCE_UNAVAILABLE",
      }),
    ).toBe(
      "本次已完成基础证据，但指标源未配置或当前不可用，因此没有开始 AI 扩展调查。请在系统设置中核对该来源的指标连接后重新调查。",
    );
  });

  it("does not add a blocker notice once expansion steps exist", () => {
    expect(
      investigationContinuationMessage({
        status: "EVIDENCE_ONLY",
        plannerStepCount: 1,
        degradations: [],
        terminationReason: "PLANNER_FINISHED",
      }),
    ).toBeNull();
  });

  it("turns a typed metric observation into a readable result", () => {
    expect(metricObservationPresentation({
      evidence_ref: "metric-1",
      alert_ref: "alert-1",
      metric_name: "checkout_http_error_ratio",
      series_count: 1,
      point_count: 24,
      minimum: 0.012,
      maximum: 0.087,
      latest: 0.087,
      sample: [
        { timestamp: 1, value: "0.012" },
        { timestamp: 2, value: "0.087" },
      ],
    })).toEqual({
      latest: "0.087",
      range: "0.012 – 0.087",
      coverage: "24 个点 · 1 条序列",
      trend: "窗口内整体上升",
    });
  });

  it("does not call a small bounded fluctuation a meaningful rise", () => {
    expect(metricObservationPresentation({
      evidence_ref: "metric-2",
      alert_ref: "alert-1",
      metric_name: "checkout_http_request_rate",
      series_count: 1,
      point_count: 24,
      minimum: 118,
      maximum: 122,
      latest: 121,
      sample: [
        { timestamp: 1, value: "118" },
        { timestamp: 2, value: "121" },
      ],
    }).trend).toBe("窗口内整体稳定");
  });
});
