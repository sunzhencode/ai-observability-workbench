import { describe, expect, it } from "vitest";
import {
  investigationStartLabel,
  investigationStartSuccessMessage,
} from "./investigationStartPresentation";

describe("investigation start presentation", () => {
  it("distinguishes the first investigation from an explicit restart", () => {
    expect(investigationStartLabel({ hasInvestigation: false })).toBe("开始证据调查");
    expect(investigationStartLabel({ hasInvestigation: true })).toBe("重新调查当前事件");
  });

  it("keeps fast submission feedback visible after the request finishes", () => {
    expect(investigationStartLabel({ hasInvestigation: true, isPending: true })).toBe(
      "正在记录告警并读取证据…",
    );
    expect(investigationStartLabel({ hasInvestigation: true, isCoolingDown: true })).toBe(
      "基础证据已生成",
    );
    expect(
      investigationStartSuccessMessage("2026-08-26T01:08:33Z", () => "2026/8/26 09:08:33"),
    ).toBe("已显示生成于 2026/8/26 09:08:33 的基础证据；下方已切换到本次结果。");
  });
});
