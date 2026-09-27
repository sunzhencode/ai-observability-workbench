import { describe, expect, it } from "vitest";

import { modelFailureText } from "./modelFailure";

describe("modelFailureText", () => {
  it("turns the case that started this into a fix, not a dead end", () => {
    // A mistyped model name is a 404. Reported as a bare SERVICE_ERROR it sends
    // the reader to check their network; the fix is a dropdown two fields up.
    const text = modelFailureText("SERVICE_ERROR", "HTTP_404");
    expect(text).toContain("找不到这个模型");
    expect(text).toContain("拉取可用模型");
  });

  it("tells a wrong key apart from a wrong model", () => {
    const badKey = modelFailureText("AUTH_FAILED", "HTTP_401");
    const badModel = modelFailureText("SERVICE_ERROR", "HTTP_404");
    expect(badKey).not.toBe(badModel);
    expect(badKey).toContain("key");
  });

  it("keeps the raw codes so a bug report can quote something exact", () => {
    expect(modelFailureText("SERVICE_ERROR", "HTTP_502")).toContain("HTTP_502");
  });

  it("explains an egress rejection by its specific reason", () => {
    expect(modelFailureText("EGRESS_REJECTED", "EGRESS_PRIVATE_ADDRESS")).toContain(
      "内网",
    );
    expect(modelFailureText("EGRESS_REJECTED", "EGRESS_SCHEME")).toContain("https");
  });

  it("still says something useful with no sub-code", () => {
    expect(modelFailureText("TIMEOUT")).toBe("服务超时");
    expect(modelFailureText("WHAT_IS_THIS")).toBe("WHAT_IS_THIS");
  });

  it("never gives two codes the same sentence", () => {
    // The taxonomy is only worth having if each entry names a different action.
    const cases: [string, string][] = [
      ["AUTH_FAILED", "HTTP_401"],
      ["SERVICE_ERROR", "HTTP_404"],
      ["SERVICE_ERROR", "HTTP_429"],
      ["EGRESS_REJECTED", "EGRESS_PRIVATE_ADDRESS"],
      ["UNREACHABLE", ""],
      ["TIMEOUT", ""],
      ["CONTRACT_INVALID", ""],
      ["MODEL_PLANNER_CONTRACT_INVALID", ""],
      ["MODEL_ANALYST_CONTRACT_INVALID", ""],
    ];
    const rendered = cases.map(([kind, detail]) => modelFailureText(kind, detail));
    expect(new Set(rendered).size).toBe(rendered.length);
  });

  it("separates the two model protocols so the next action is specific", () => {
    expect(modelFailureText("MODEL_PLANNER_CONTRACT_INVALID")).toContain("工具调用");
    expect(modelFailureText("MODEL_ANALYST_CONTRACT_INVALID")).toContain("结构化输出");
  });
});
