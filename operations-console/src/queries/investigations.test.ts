import { describe, expect, it } from "vitest";
import { investigationRefetchInterval } from "./investigations";

describe("investigation refresh interval", () => {
  it("polls active investigations quickly", () => {
    expect(investigationRefetchInterval([{ status: "QUEUED" }], false)).toBe(2_000);
  });

  it("retries a cached terminal history after a refresh error", () => {
    expect(investigationRefetchInterval([{ status: "COMPLETED" }], true)).toBe(15_000);
  });

  it("does not poll a healthy terminal history", () => {
    expect(investigationRefetchInterval([{ status: "COMPLETED" }], false)).toBe(false);
  });
});
