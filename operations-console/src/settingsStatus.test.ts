import { describe, expect, it } from "vitest";
import type { EventSource, Health, SourcePollHealth } from "./types";
import {
  explainSaveError,
  pollHealthSummary,
  sourceModeSummary,
  sourceStatus,
  sourceStatusText,
} from "./settingsStatus";

function source(patch: Partial<EventSource> = {}): EventSource {
  return {
    id: "src_1",
    name: "AM",
    lifecycle_state: "ENABLED",
    status: "ENABLED",
    ...patch,
  } as EventSource;
}

function health(patch: Partial<Health> = {}): Health {
  return {
    source_mode: "REGISTRY",
    legacy_source_id: null,
    last_poll_ok: true,
    last_poll_error: null,
    ...patch,
  } as Health;
}

describe("sourceStatus", () => {
  it("tells the user what to do next for every actionable state", () => {
    expect(sourceStatusText(source({ status: "CONNECTION_ERROR" })))
      .toBe("连接异常 · 检查地址");
    expect(sourceStatusText(source({ lifecycle_state: "DISABLED" })))
      .toBe("已停用 · 可重新启用");
  });

  it("offers no next step for settled states", () => {
    expect(sourceStatusText(source())).toBe("已启用");
    expect(sourceStatusText(source({ lifecycle_state: "ARCHIVED" }))).toBe("已归档");
  });

  it("has no vocabulary left for unapplied changes", () => {
    // F22 removed the state itself; the copy must not survive it.
    const labels = [
      sourceStatusText(source()),
      sourceStatusText(source({ lifecycle_state: "DISABLED" })),
      sourceStatusText(source({ lifecycle_state: "ARCHIVED" })),
      sourceStatusText(source({ status: "CONNECTION_ERROR" })),
    ].join(" ");
    for (const banned of ["未应用", "候选", "快照", "应用", "绑定", "范围模式"]) {
      expect(labels).not.toContain(banned);
    }
  });

  it("marks only real problems as degraded", () => {
    expect(sourceStatus(source()).tone).toBe("ok");
    expect(sourceStatus(source({ status: "CONNECTION_ERROR" })).tone).toBe("degraded");
    expect(sourceStatus(source({ lifecycle_state: "ARCHIVED" })).tone).toBe("neutral");
  });
});

describe("pollHealthSummary", () => {
  const fmt = (iso: string) => iso;

  function poll(patch: Partial<SourcePollHealth> = {}): SourcePollHealth {
    return {
      source_id: "src_1",
      health: "HEALTHY",
      endpoint_total: 2,
      endpoint_succeeded: 2,
      last_complete_success_at: "2026-07-26T10:00:00Z",
      last_any_success_at: "2026-07-26T10:00:00Z",
      ...patch,
    } as SourcePollHealth;
  }

  it("splits state from timestamps", () => {
    const summary = pollHealthSummary(poll(), fmt);
    expect(summary.primary).toBe("HEALTHY · 2/2 endpoint");
    expect(summary.secondary).toBe("完整成功 2026-07-26T10:00:00Z");
  });

  it("surfaces a partial success only when it is newer than the complete one", () => {
    const partial = pollHealthSummary(
      poll({
        health: "DEGRADED",
        endpoint_succeeded: 1,
        last_any_success_at: "2026-07-26T11:00:00Z",
      }),
      fmt,
    );
    expect(partial.primary).toBe("DEGRADED · 1/2 endpoint");
    expect(partial.secondary).toContain("部分成功 2026-07-26T11:00:00Z");

    // Equal timestamps mean the last poll was complete; no extra noise.
    expect(pollHealthSummary(poll(), fmt).secondary).not.toContain("部分成功");
  });

  it("handles a source that has never polled", () => {
    expect(pollHealthSummary(undefined).primary).toBe("暂无轮询记录");
  });

  it("reports a partial-only history without a complete success", () => {
    const summary = pollHealthSummary(
      poll({ last_complete_success_at: null, last_any_success_at: "2026-07-26T09:00:00Z" }),
      fmt,
    );
    expect(summary.secondary).toBe("尚无完整成功 · 部分成功 2026-07-26T09:00:00Z");
  });
});

describe("sourceModeSummary", () => {
  it("counts managed sources in registry mode", () => {
    const summary = sourceModeSummary(health({ source_mode: "REGISTRY" }), 2);
    expect(summary.title).toBe("由 2 个受管来源供数");
    expect(summary.tone).toBe("ok");
  });

  it("says nothing is configured and points at the next step", () => {
    const summary = sourceModeSummary(health({ source_mode: "UNCONFIGURED" }), 0);
    expect(summary.title).toContain("尚未配置");
    expect(summary.detail).toContain("不会轮询");
    expect(summary.tone).toBe("degraded");
  });

  it("does not claim a mode before health has loaded", () => {
    const summary = sourceModeSummary(null, 0);
    expect(summary.tone).toBe("neutral");
  });
});

describe("explainSaveError", () => {
  it("names the file the user has to fix", () => {
    // Nobody sets the key any more, so the message points at the file the
    // backend failed to read rather than at a variable to go and invent.
    const text = explainSaveError("MASTER_KEY_NOT_CONFIGURED");
    expect(text).toContain("backend/data/master.key");
    expect(text).not.toContain(".env");
  });

  it("distinguishes an unreadable secret from a missing key", () => {
    expect(explainSaveError("SECRET_UNAVAILABLE")).toContain("解密");
    expect(explainSaveError("SECRET_UNAVAILABLE")).not.toContain(".env");
  });

  it("passes anything else through untouched", () => {
    expect(explainSaveError("ENDPOINT_UNREACHABLE")).toBe("ENDPOINT_UNREACHABLE");
  });
});
