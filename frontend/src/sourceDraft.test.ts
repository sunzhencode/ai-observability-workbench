import { describe, expect, it } from "vitest";
import type { EventSource, EventSourceCandidate } from "./types";
import {
  advancedSummary,
  authSecret,
  authSummary,
  backupSummary,
  canSave,
  draftFromSource,
  emptyEndpoint,
  emptySourceDraft,
  emptyThanos,
  endpointDraftFromOut,
  withAssignedPositions,
  historySummary,
} from "./sourceDraft";

function withUrl(url = "https://am.test"): EventSourceCandidate {
  const draft = emptySourceDraft();
  return { ...draft, name: "AM", endpoints: [{ ...draft.endpoints[0], url }] };
}

describe("emptySourceDraft", () => {
  it("needs only a name and an address to be saveable", () => {
    expect(canSave(emptySourceDraft())).toBe(false);
    expect(canSave(withUrl())).toBe(true);
  });

  it("leaves every optional section at a value the user never has to open", () => {
    const draft = emptySourceDraft();

    expect(draft.endpoints).toHaveLength(1);
    expect(draft.thanos?.url).toBe("");
    expect(draft.poll_interval_seconds).toBe(30);
    expect(draft.watchdog_enabled).toBe(false);
  });

  it("refuses a blank name or a blank address", () => {
    expect(canSave({ ...withUrl(), name: "   " })).toBe(false);
    expect(canSave(withUrl("  "))).toBe(false);
  });
});

describe("collapsed section summaries", () => {
  it("says what is inside without opening it", () => {
    const draft = withUrl();

    expect(backupSummary(draft)).toBe("未配置");
    expect(authSummary(draft)).toBe("无认证");
    expect(historySummary(draft)).toBe("未配置");
    expect(advancedSummary(draft)).toContain("轮询 30s");
  });

  it("reports what has actually been configured", () => {
    const draft = withUrl();
    const configured: EventSourceCandidate = {
      ...draft,
      endpoints: [
        { ...draft.endpoints[0], auth_type: "BEARER" },
        { ...draft.endpoints[0], url: "https://am-2.test" },
      ],
      thanos: { ...draft.thanos!, url: "https://thanos.test" },
      watchdog_enabled: true,
    };

    expect(backupSummary(configured)).toBe("1 个");
    expect(authSummary(configured)).toBe("BEARER");
    expect(historySummary(configured)).toBe("https://thanos.test");
    expect(advancedSummary(configured)).toContain("Watchdog 开");
  });
});

describe("authSecret", () => {
  it("keeps a stored credential and never invents a value to show", () => {
    expect(authSecret("NONE", true)).toEqual({ action: "CLEAR" });
    expect(authSecret("BEARER", true)).toEqual({ action: "KEEP" });
    expect(authSecret("BEARER", false)).toEqual({ action: "REPLACE", value: "" });
  });
});

describe("draftFromSource", () => {
  it("round-trips the one current configuration", () => {
    const source = {
      id: "src_1",
      name: "Prod AM",
      lifecycle_state: "ENABLED",
      status: "ENABLED",
      version: 3,
      config: {
        poll_interval_seconds: 15,
        resolution_grace_seconds: 0,
        max_parallel_endpoints: 2,
        watchdog_enabled: true,
        watchdog_alertname: "Watchdog",
        watchdog_identity_label: "cluster",
        watchdog_missing_after_seconds: 60,
        last_test_status: null,
        tested_at: null,
        safe_error_code: null,
        endpoints: [
          {
            position: 0,
            url: "https://am.test",
            enabled: true,
            auth_type: "BEARER",
            username: "",
            secret_configured: true,
            last_test: null,
          },
        ],
        thanos: {
          url: "https://thanos.test",
          auth_type: "NONE",
          username: "",
          secret_configured: false,
          timeout_seconds: 20,
          last_test_status: null,
          tested_at: null,
          safe_error_code: null,
        },
      },
    } as unknown as EventSource;

    const draft = draftFromSource(source);

    expect(draft.name).toBe("Prod AM");
    expect(draft.endpoints[0].url).toBe("https://am.test");
    expect(draft.endpoints[0].secret).toEqual({ action: "KEEP" });
    expect(draft.thanos?.timeout_seconds).toBe(20);
    expect(draft.poll_interval_seconds).toBe(15);
  });

  it("falls back to defaults for a source with no configuration yet", () => {
    const draft = draftFromSource({
      id: "src_empty",
      name: "Empty",
      config: null,
    } as unknown as EventSource);

    expect(draft.name).toBe("Empty");
    expect(draft.endpoints[0].url).toBe("");
  });
});

describe("withAssignedPositions", () => {
  function loaded(): EventSourceCandidate {
    const draft = emptySourceDraft();
    return {
      ...draft,
      name: "HA",
      endpoints: [
        { ...draft.endpoints[0], position: 0, url: "https://am-0.test" },
        { ...draft.endpoints[0], position: 1, url: "https://am-1.test" },
        { ...draft.endpoints[0], position: 2, url: "https://am-2.test" },
      ],
    };
  }

  it("keeps each surviving endpoint on the slot it came from", () => {
    // 「移除」 drops the middle row; the last one must not slide into slot 1,
    // because slot 1 owns the removed endpoint's stored credential.
    const afterRemoval = {
      ...loaded(),
      endpoints: loaded().endpoints.filter((_, index) => index !== 1),
    };

    const payload = withAssignedPositions(afterRemoval);

    expect(payload.endpoints.map((item) => [item.url, item.position])).toEqual([
      ["https://am-0.test", 0],
      ["https://am-2.test", 2],
    ]);
  });

  it("gives a newly added row the lowest free slot", () => {
    const draft = loaded();
    const withGap = {
      ...draft,
      endpoints: [
        draft.endpoints[0],
        draft.endpoints[2],
        { ...emptySourceDraft().endpoints[0], url: "https://am-new.test" },
      ],
    };

    const payload = withAssignedPositions(withGap);

    expect(payload.endpoints.map((item) => item.position)).toEqual([0, 2, 1]);
  });

  it("numbers a brand-new source from zero", () => {
    const draft = emptySourceDraft();
    const payload = withAssignedPositions({
      ...draft,
      endpoints: [draft.endpoints[0], { ...draft.endpoints[0] }],
    });
    expect(payload.endpoints.map((item) => item.position)).toEqual([0, 1]);
  });
});

describe("endpointDraftFromOut", () => {
  it("carries the stored slot into the draft", () => {
    expect(
      endpointDraftFromOut({
        position: 3,
        url: "https://am.test",
        enabled: true,
        auth_type: "NONE",
        username: "",
        secret_configured: false,
        last_test: null,
      }),
    ).toMatchObject({ position: 3, url: "https://am.test" });
  });
});

describe("withAssignedPositions strips form-only fields", () => {
  it("never sends the secret box state to the API", () => {
    // `secretField` exists so the box can say "已配置 · 留空表示不修改". The API
    // has no such field and rejects extras.
    const draft = {
      ...emptySourceDraft(),
      endpoints: [
        {
          ...emptyEndpoint(),
          url: "https://a.example",
          secretField: { configured: true, input: "", cleared: false },
        },
      ],
      thanos: {
        ...emptyThanos(),
        secretField: { configured: false, input: "x", cleared: false },
      },
    };

    const payload = withAssignedPositions(draft);

    expect(payload.endpoints[0]).not.toHaveProperty("secretField");
    expect(payload.thanos).not.toHaveProperty("secretField");
    // The wire value survives.
    expect(payload.endpoints[0].secret).toBeDefined();
  });
});
