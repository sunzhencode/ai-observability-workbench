import { describe, expect, it } from "vitest";
import { occurrenceDetailState } from "./occurrenceDetailState";

describe("occurrence detail read state", () => {
  it("keeps cached detail visible when a background refresh fails", () => {
    expect(occurrenceDetailState({
      hasData: true,
      isError: true,
      isPending: false,
      errorStatus: null,
    })).toBe("STALE");
  });

  it("only calls an occurrence missing after an initial 404", () => {
    expect(occurrenceDetailState({
      hasData: false,
      isError: true,
      isPending: false,
      errorStatus: 404,
    })).toBe("MISSING");
  });

  it("distinguishes an initial transport failure from a missing occurrence", () => {
    expect(occurrenceDetailState({
      hasData: false,
      isError: true,
      isPending: false,
      errorStatus: null,
    })).toBe("UNAVAILABLE");
  });
});
