import { describe, expect, it } from "vitest";
import type { HandlingState } from "./types";
import {
  defaultHandlingReason,
  handlingActionLabel,
  handlingActions,
  handlingLabel,
  isHandlingSettled,
  resolveHandlingReason,
  showsHandlingPill,
} from "./handling";

const ALL: HandlingState[] = ["NEW", "IN_PROGRESS", "CLOSED", "FALSE_POSITIVE"];

describe("labels", () => {
  it("never leaks an enum value into the UI", () => {
    for (const state of ALL) {
      expect(handlingLabel(state)).not.toBe(state);
      expect(handlingActionLabel(state)).not.toBe(state);
      expect(handlingLabel(state)).not.toMatch(/[A-Z_]{3,}/);
    }
  });

  it("uses distinct labels so states cannot be confused", () => {
    expect(new Set(ALL.map(handlingLabel)).size).toBe(ALL.length);
  });
});

describe("transitions", () => {
  it("offers the three initial moves from 未处理", () => {
    expect(handlingActions("NEW")).toEqual(["IN_PROGRESS", "CLOSED", "FALSE_POSITIVE"]);
  });

  it("drops 开始处理 once already in progress", () => {
    expect(handlingActions("IN_PROGRESS")).toEqual(["CLOSED", "FALSE_POSITIVE"]);
  });

  it("treats终态 as terminal", () => {
    expect(handlingActions("CLOSED")).toEqual([]);
    expect(handlingActions("FALSE_POSITIVE")).toEqual([]);
  });

  it("never offers a transition back to the state it is already in", () => {
    for (const state of ALL) {
      expect(handlingActions(state)).not.toContain(state);
    }
  });
});

describe("showsHandlingPill", () => {
  it("stays quiet for untouched incidents", () => {
    expect(showsHandlingPill("NEW")).toBe(false);
  });

  it("marks every state the user actually chose", () => {
    for (const state of ALL.filter((item) => item !== "NEW")) {
      expect(showsHandlingPill(state)).toBe(true);
    }
  });
});

describe("isHandlingSettled", () => {
  it("counts only the states a human chose as a conclusion", () => {
    expect(isHandlingSettled("CLOSED")).toBe(true);
    expect(isHandlingSettled("FALSE_POSITIVE")).toBe(true);
    expect(isHandlingSettled("NEW")).toBe(false);
    expect(isHandlingSettled("IN_PROGRESS")).toBe(false);
  });

  it("agrees with the transition table: settled means nothing left to choose", () => {
    for (const state of ALL) {
      expect(isHandlingSettled(state)).toBe(handlingActions(state).length === 0);
    }
  });
});

describe("resolveHandlingReason", () => {
  it("records a deterministic default so one-click marking stays auditable", () => {
    expect(resolveHandlingReason("", "CLOSED")).toBe("本地标记为已关闭");
    expect(resolveHandlingReason("   ", "FALSE_POSITIVE")).toBe("本地标记为误报");
  });

  it("prefers what the user typed, trimmed", () => {
    expect(resolveHandlingReason("  上游已修复  ", "CLOSED")).toBe("上游已修复");
  });

  it("never returns an empty string, which the backend rejects", () => {
    for (const state of ALL) {
      expect(resolveHandlingReason("", state).trim()).not.toBe("");
      expect(defaultHandlingReason(state).trim()).not.toBe("");
    }
  });
});
