import { describe, expect, it } from "vitest";
import { queueSourceText } from "./incidentPresentation";

describe("incident queue presentation", () => {
  it("identifies the source directly on each queue card", () => {
    expect(queueSourceText("source-a")).toBe("来源：source-a");
  });
});
