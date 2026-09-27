import { describe, expect, it } from "vitest";
import {
  availableResolutionCodes,
  RESOLUTION_LABELS,
  resolutionHelp,
  resolutionReasonRequired,
} from "./responseActions";

describe("three-state response actions", () => {
  it("only offers recovery claims after the upstream signal recovered", () => {
    expect(availableResolutionCodes("FIRING")).toEqual([
      "FALSE_POSITIVE",
      "DUPLICATE",
      "NO_ACTION",
    ]);
    expect(availableResolutionCodes("RECOVERED")).toContain("FIXED");
    expect(availableResolutionCodes("RECOVERED")).toContain("SELF_RECOVERED");
  });

  it("uses product language and requires an explanation without recovery", () => {
    expect(RESOLUTION_LABELS.FIXED).toBe("人工处理后恢复");
    expect(resolutionReasonRequired("FIRING")).toBe(true);
    expect(resolutionReasonRequired("RECOVERED")).toBe(false);
    expect(resolutionHelp("FIRING")).toContain("尚未确认恢复");
  });
});
