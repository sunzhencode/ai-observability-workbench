import { describe, expect, it } from "vitest";
import { backgroundReadState } from "./backgroundReadState";

describe("background read state", () => {
  it("treats a refresh error with cached data as stale, not unavailable", () => {
    expect(backgroundReadState({
      hasData: true,
      isError: true,
      isPending: false,
    })).toBe("STALE");
  });

  it("treats an initial read error without cached data as unavailable", () => {
    expect(backgroundReadState({
      hasData: false,
      isError: true,
      isPending: false,
    })).toBe("UNAVAILABLE");
  });

  it("keeps an empty but successfully read collection ready", () => {
    expect(backgroundReadState({
      hasData: true,
      isError: false,
      isPending: false,
    })).toBe("READY");
  });
});
