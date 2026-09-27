import { afterEach, describe, expect, it, vi } from "vitest";
import { ageFrom, cardClass, sevPill, statePill } from "./util";

afterEach(() => {
  vi.useRealTimers();
});

function at(nowIso: string) {
  vi.useFakeTimers();
  vi.setSystemTime(new Date(nowIso));
}

describe("ageFrom", () => {
  it("reads a Z-suffixed timestamp as UTC", () => {
    at("2026-07-26T10:00:30Z");
    expect(ageFrom("2026-07-26T10:00:00Z")).toBe("30s");
  });

  it("steps through s/m/h/d units", () => {
    at("2026-07-26T12:00:00Z");
    expect(ageFrom("2026-07-26T11:59:01Z")).toBe("59s");
    expect(ageFrom("2026-07-26T11:59:00Z")).toBe("1m");
    expect(ageFrom("2026-07-26T11:01:00Z")).toBe("59m");
    expect(ageFrom("2026-07-26T11:00:00Z")).toBe("1h");
    expect(ageFrom("2026-07-25T13:00:00Z")).toBe("23h");
    expect(ageFrom("2026-07-25T12:00:00Z")).toBe("1d");
    expect(ageFrom("2026-07-24T11:00:00Z")).toBe("2d");
  });

  it("never reports a negative age for a future timestamp", () => {
    at("2026-07-26T10:00:00Z");
    expect(ageFrom("2026-07-26T10:05:00Z")).toBe("0s");
  });

  it("returns an em dash for null and unparseable input", () => {
    expect(ageFrom(null)).toBe("—");
    expect(ageFrom("not-a-date")).toBe("—");
  });

  it("documents why the backend must emit Z: a naive string is read as local", () => {
    // schemas.py serializes naive datetimes as UTC with an explicit Z. If that
    // ever regresses, the same instant shifts by the viewer's UTC offset --
    // 8 hours in UTC+8 -- and every age on the page is wrong.
    const naive = Date.parse("2026-07-26T10:00:00");
    const utc = Date.parse("2026-07-26T10:00:00Z");
    const offsetMs = new Date("2026-07-26T10:00:00").getTimezoneOffset() * 60_000;
    expect(naive).toBe(utc + offsetMs);
  });
});

describe("class name helpers", () => {
  it("adds the firing modifier only while firing", () => {
    expect(cardClass("critical", "firing")).toBe("card card--critical card--firing");
    expect(cardClass("critical", "recovered")).toBe("card card--critical");
  });

  it("builds severity and state pills", () => {
    expect(sevPill("warning")).toBe("pill pill--warning");
    expect(statePill("unknown")).toBe("pill pill--unknown");
  });
});
