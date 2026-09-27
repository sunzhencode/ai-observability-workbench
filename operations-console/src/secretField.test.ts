import { describe, expect, it } from "vitest";
import {
  emptySecretField,
  secretFieldSatisfied,
  secretPlaceholder,
  secretUpdateFor,
} from "./secretField";

describe("secretUpdateFor", () => {
  it("keeps a stored value when the user types nothing", () => {
    // The ordinary case: open a saved channel, change something else, save.
    expect(secretUpdateFor({ configured: true, input: "", cleared: false })).toEqual({
      action: "KEEP",
    });
  });

  it("replaces when the user types", () => {
    expect(secretUpdateFor({ configured: true, input: "new", cleared: false })).toEqual({
      action: "REPLACE",
      value: "new",
    });
    expect(secretUpdateFor({ configured: false, input: "new", cleared: false })).toEqual({
      action: "REPLACE",
      value: "new",
    });
  });

  it("clears when asked, whatever was typed before", () => {
    expect(secretUpdateFor({ configured: true, input: "x", cleared: true })).toEqual({
      action: "CLEAR",
    });
  });

  it("sends nothing for an untouched optional field on a new object", () => {
    expect(secretUpdateFor(emptySecretField())).toEqual({ action: "CLEAR" });
  });

  it("never sends a value with KEEP or CLEAR, which the API rejects", () => {
    for (const state of [
      { configured: true, input: "", cleared: false },
      { configured: true, input: "typed", cleared: true },
      { configured: false, input: "", cleared: false },
    ]) {
      const update = secretUpdateFor(state);
      if (update.action !== "REPLACE") expect(update.value).toBeUndefined();
    }
  });
});

describe("secretPlaceholder", () => {
  it("says the box may be left alone once something is stored", () => {
    expect(secretPlaceholder({ configured: true, input: "", cleared: false })).toContain(
      "留空表示不修改",
    );
  });

  it("says what will happen when clearing", () => {
    expect(secretPlaceholder({ configured: true, input: "", cleared: true })).toBe(
      "将被清除",
    );
  });

  it("uses the caller's hint for a fresh field", () => {
    expect(secretPlaceholder(emptySecretField(), "粘贴完整地址")).toBe("粘贴完整地址");
  });
});

describe("secretFieldSatisfied", () => {
  it("accepts a stored value without retyping", () => {
    expect(secretFieldSatisfied({ configured: true, input: "", cleared: false }, true)).toBe(
      true,
    );
  });

  it("refuses an empty required field on a new object", () => {
    expect(secretFieldSatisfied(emptySecretField(), true)).toBe(false);
  });

  it("refuses a required field the user just cleared", () => {
    expect(
      secretFieldSatisfied({ configured: true, input: "", cleared: true }, true),
    ).toBe(false);
  });

  it("is always happy when the field is optional", () => {
    expect(secretFieldSatisfied(emptySecretField(), false)).toBe(true);
  });
});
