import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "./client";

interface Call {
  url: string;
  init?: RequestInit;
}

function stubFetch(
  responder: (call: Call) => { status?: number; json?: unknown; text?: string },
) {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    const { status = 200, json, text } = responder({ url, init });
    return {
      ok: status >= 200 && status < 300,
      status,
      headers: new Headers(),
      json: async () => json,
      text: async () => text ?? "",
    } as Response;
  });
  return calls;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("GET requests", () => {
  it("sends notification incident and page filters to the server", async () => {
    const calls = stubFetch(() => ({ json: [] }));
    await api.notificationDeliveries({ incidentId: 42, beforeId: 201, channelId: "channel-a" });
    const query = new URL(calls[0].url, "http://localhost").searchParams;
    expect(query.get("incident_id")).toBe("42");
    expect(query.get("before_id")).toBe("201");
    expect(query.get("channel_id")).toBe("channel-a");
  });

  it("reads platform health as one server snapshot and preserves partial failure", async () => {
    const snapshot = {
      status: "ok",
      source_mode: "REGISTRY",
      configuration_status: "configured",
      configuration_errors: [],
      last_poll_at: "2026-09-09T00:00:02Z",
      last_poll_ok: false,
      last_poll_error: "ENDPOINT_TIMEOUT",
      incident_count: 1,
      last_backfill_at: null,
      last_backfill_ok: null,
      last_backfill_error: null,
      backfill_skipped: true,
      backfill_effective_hours: 0,
      backfill_truncated_reason: null,
      alerts_reconstructed: 0,
      sources: [{ health: "DEGRADED" }],
      watchdog: { overall_status: "healthy", sources: [] },
      notifications: { status: "disabled" },
      readiness: { status: "ready", checked_at: "2026-09-09T00:00:03Z", checks: [] },
      jobs: { scheduler_running: true, runner_running: true },
    };
    const calls = stubFetch(() => ({ json: snapshot }));

    const health = await api.health();

    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe("/api/v1/platform-health");
    expect(health.last_poll_ok).toBe(false);
    expect(health.sources[0].health).toBe("DEGRADED");
    expect(health.readiness.status).toBe("ready");
  });

  it("reads the single V2 investigator resource without legacy P0/P1 projection", async () => {
    const calls = stubFetch(() => ({
      json: [{
        schema_version: "V2",
        id: "inv-1",
        occurrence_id: 3,
        status: "COMPLETED",
        job_id: "job-1",
        provider_profile_id: "profile-1",
        model_channel_id: "model-1",
        model_revision: 2,
        alert_evidence: [{ evidence_id: "alert-1", alertname: "HighErrors" }],
        metric_evidence: [{
          evidence_id: "metric-1",
          metric_id: "error_ratio",
          status: "DATA",
          summary: { latest: 0.08 },
          sample: [],
        }],
        degraded_domains: [],
        available_metric_count: 2,
        activities: [],
        report: null,
        request_count: 1,
        tool_call_count: 0,
        input_tokens: 10,
        output_tokens: 5,
        safe_error_code: null,
        created_at: "2026-09-04T00:00:00Z",
        updated_at: "2026-09-04T00:00:01Z",
      }],
    }));

    const investigations = await api.occurrenceInvestigations(3);

    expect(calls[0].url).toBe("/api/v1/occurrences/3/investigator-runs");
    expect(investigations[0].schema_version).toBe("V2");
    expect(investigations[0].metric_evidence[0].evidence_id).toBe("metric-1");
  });

  it("maps the monitoring test result instead of its activation state", async () => {
    stubFetch(({ url }) => url.includes("/monitoring")
      ? {
          json: [{
            source_id: "source-a",
            kind: "GRAFANA",
            base_url: "http://grafana.invalid",
            state: "ACTIVE",
            secret_configured: false,
            tested_at: "2026-08-20T00:00:00Z",
            last_test_code: "OK",
            version: 1,
          }],
        }
      : {
          json: [{
            id: "source-a",
            name: "Primary",
            state: "ENABLED",
            version: 1,
            endpoints: [],
            poll_interval_seconds: 30,
            resolution_grace_seconds: 60,
            max_parallel_endpoints: 2,
            watchdog_enabled: false,
            watchdog_alertname: "Watchdog",
            watchdog_identity_label: "cluster",
            watchdog_missing_after_seconds: 300,
            last_poll_at: null,
            last_poll_completeness: null,
            last_poll_safe_error_codes: [],
            created_at: "2026-08-20T00:00:00Z",
            updated_at: "2026-08-20T00:00:00Z",
          }],
        });

    const sources = await api.eventSources();

    expect(sources[0].config?.grafana?.last_test_status).toBe("OK");
  });

  it("keeps model usage and latest test result as independent states", async () => {
    stubFetch(() => ({
      json: [{
        id: "model-primary",
        name: "Primary model",
        kind: "OPENAI_COMPATIBLE",
        enabled: true,
        revision_no: 2,
        state: "ACTIVE",
        base_url: "https://models.example.invalid/v1",
        model: "model-a",
        api_key_configured: true,
        tested_at: "2026-08-24T12:00:00Z",
        last_test_code: "MODEL_TIMEOUT",
      }],
    }));

    const channels = await api.modelChannels();

    expect(channels[0].enabled).toBe(true);
    expect(channels[0].active_revision_id).toBe("model-primary");
    expect(channels[0].revisions[0].last_test_code).toBe("MODEL_TIMEOUT");
    expect(channels[0].revisions[0].tested_ok_at).toBeNull();
  });

  it("preserves notification config and per-field secret state for editing", async () => {
    stubFetch(() => ({
      json: [{
        id: "webhook-primary",
        name: "Primary webhook",
        provider: "GENERIC_WEBHOOK",
        enabled: true,
        revision_no: 3,
        revision_state: "ACTIVE",
        config: { url: "https://hooks.example.com/workbench", timeout_seconds: 12 },
        secret_configured: { headers: true, signing_secret: false },
        tested_at: "2026-09-13T00:00:00Z",
        last_test_code: "OK",
      }],
    }));

    const channels = await api.notificationChannels();
    const revision = channels[0].revisions[0];

    expect(revision.config).toEqual({
      url: "https://hooks.example.com/workbench",
      timeout_seconds: 12,
    });
    expect(revision.secret_configured).toEqual({
      headers: true,
      signing_secret: false,
    });
  });

  it("filters incidents locally because the candidate endpoint returns the full queue", async () => {
    const calls = stubFetch(() => ({ json: [] }));
    await api.incidents({ sourceIds: ["source-a", "source-b"] });
    expect(calls[0].url).toBe("/api/v1/incidents");
  });

  it("uses the same stable endpoint when no source is selected", async () => {
    const calls = stubFetch(() => ({ json: [] }));
    await api.incidents();
    expect(calls[0].url).toBe("/api/v1/incidents");
    await api.incidents({ sourceIds: [] });
    expect(calls[1].url).toBe("/api/v1/incidents");
  });

  it("encodes Incident Queue system view and URL filters", async () => {
    const calls = stubFetch(() => ({
      json: { items: [], next_cursor: null, evaluated_at: "2026-08-13T00:00:00Z" },
    }));
    await api.operationalOccurrences({
      view: "SLA_AT_RISK",
      sourceIds: ["source-a", "source-b"],
      signalStates: ["FIRING"],
      limit: 50,
    });
    expect(calls[0].url).toBe(
      "/api/v1/occurrences?view=SLA_AT_RISK&source_ids=source-a%2Csource-b&signal_states=FIRING&limit=50",
    );
  });

  it("reads deterministic similar history from the occurrence resource", async () => {
    const calls = stubFetch(() => ({ json: [] }));
    await api.similarOccurrenceHistory(18);
    expect(calls[0].url).toBe("/api/v1/occurrences/18/similar");
    expect(calls[0].init?.method ?? "GET").toBe("GET");
  });

  it("reads Analytics with the exact URL-backed filters", async () => {
    const calls = stubFetch(() => ({
      json: {
        freshness: "ROLLUP_PENDING",
        generated_at: null,
        range: "30d",
        from_utc: "2026-08-10T00:00:00Z",
        to_utc: "2026-09-09T00:00:00Z",
        source_id: "source/a",
        service_id: 7,
        signal_severity: "warning",
        response: {},
        signal: {},
        notifications: {},
        ai: {},
      },
    }));

    await api.analyticsOverview({
      range: "30d",
      sourceId: "source/a",
      serviceId: 7,
      signalSeverity: "warning",
    });

    expect(calls[0].url).toBe(
      "/api/v1/analytics/overview?range=30d&source_id=source%2Fa&service_id=7&signal_severity=warning",
    );
    expect(calls[0].init?.method ?? "GET").toBe("GET");
  });
});

describe("write requests", () => {
  it("starts and cancels the V2 investigator through idempotent commands", async () => {
    vi.stubGlobal("crypto", { randomUUID: () => "00000000-0000-4000-8000-000000000002" });
    const calls = stubFetch(() => ({
      json: {
        schema_version: "V2",
        id: "inv/1",
        occurrence_id: 18,
        status: "QUEUED",
        alert_evidence: [],
        metric_evidence: [],
        degraded_domains: [],
        available_metric_count: 0,
        activities: [],
        report: null,
        request_count: 0,
        tool_call_count: 0,
        input_tokens: 0,
        output_tokens: 0,
        created_at: "2026-09-04T00:00:00Z",
        updated_at: "2026-09-04T00:00:00Z",
      },
    }));

    await api.startOccurrenceInvestigation(18);
    await api.cancelInvestigation("inv/1");

    expect(calls[0].url).toBe("/api/v1/occurrences/18/investigator-runs");
    expect(calls[1].url).toBe("/api/v1/investigator-runs/inv%2F1/cancel");
    expect(calls.map((call) => call.init?.method)).toEqual(["POST", "POST"]);
    expect(calls.map((call) => new Headers(call.init?.headers).get("Idempotency-Key"))).toEqual([
      "operator-00000000-0000-4000-8000-000000000002",
      "operator-00000000-0000-4000-8000-000000000002",
    ]);
  });

  it("records investigation feedback without changing investigation state", async () => {
    const calls = stubFetch(() => ({
      status: 201,
      json: { sequence: 1, rating: "ADOPTED", created_at: "2026-08-29T00:00:00Z" },
    }));

    await api.recordInvestigationFeedback("inv/1", "ADOPTED");

    expect(calls[0].url).toBe("/api/v1/investigator-runs/inv%2F1/feedback");
    expect(calls[0].init?.method).toBe("POST");
    expect(String(calls[0].init?.body)).toContain('"rating":"ADOPTED"');
  });
  it("uses the single start-handling command instead of internal response transitions", async () => {
    const calls = stubFetch(() => ({
      json: {
        occurrence_id: 3,
        previous_state: "UNACKNOWLEDGED",
        current_state: "IN_PROGRESS",
        resolution_code: null,
        version: 2,
        timeline: [],
        replayed: false,
      },
    }));

    await api.startHandlingOccurrence(3, 1, "已接手核对");

    expect(calls[0].url).toBe("/api/v1/occurrences/3/start-handling");
    expect(String(calls[0].init?.body)).toContain('"expected_version":1');
    expect(String(calls[0].init?.body)).toContain("已接手核对");
  });

  it("ends handling with an explicit resolution category and no priority field", async () => {
    const calls = stubFetch(() => ({
      json: {
        occurrence_id: 3,
        previous_state: "IN_PROGRESS",
        current_state: "RESOLVED",
        resolution_code: "FALSE_POSITIVE",
        version: 3,
        timeline: [],
        replayed: false,
      },
    }));

    await api.resolveOccurrence(3, 2, "FALSE_POSITIVE", null, "规则条件不适用于该服务");

    expect(calls[0].url).toBe("/api/v1/occurrences/3/resolve");
    const body = String(calls[0].init?.body);
    expect(body).toContain('"resolution_code":"FALSE_POSITIVE"');
    expect(body).not.toContain("priority");
  });

  it("records an HTTPS Runbook link without fetching or executing the target", async () => {
    const calls = stubFetch(() => ({
      status: 201,
      json: {
        task: {
          id: 7,
          occurrence_id: 3,
          title: "Verify replication",
          description: null,
          due_at: null,
          runbook_link: "https://runbooks.internal.example/db",
          status: "TODO",
          result: null,
          version: 1,
          created_at: "2026-08-20T00:00:00Z",
          updated_at: "2026-08-20T00:00:00Z",
        },
        timeline: {
          id: 1,
          occurrence_id: 3,
          sequence: 1,
          actor_type: "INTERACTIVE_OPERATOR",
          event_type: "TASK_CREATED",
          summary: "task created",
          detail: {},
          request_id: "req-1",
          source_ip: "127.0.0.1",
          created_at: "2026-08-20T00:00:00Z",
        },
        replayed: false,
      },
    }));

    await api.createOccurrenceTask(3, {
      title: "Verify replication",
      description: null,
      due_at: null,
      runbook_link: "https://runbooks.internal.example/db",
    });

    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe("/api/v1/occurrences/3/tasks");
    expect(calls[0].init?.method).toBe("POST");
    expect(String(calls[0].init?.body)).toContain("https://runbooks.internal.example/db");
  });

  it("adds a fresh stable idempotency key to resource creation", async () => {
    vi.stubGlobal("crypto", { randomUUID: () => "00000000-0000-4000-8000-000000000001" });
    const calls = stubFetch(() => ({
      status: 201,
      json: {
        id: 1,
        name: "Warnings",
        priority: 10,
        enabled: true,
        version: 1,
        matchers: [],
        group_by_labels: ["alertname"],
        source_ids: [],
        created_at: "2026-08-13T00:00:00Z",
        updated_at: "2026-08-13T00:00:00Z",
      },
    }));

    await api.createAggregationRule({
      name: "Warnings",
      priority: 10,
      enabled: true,
      matchers: [],
      group_by_labels: ["alertname"],
    });

    expect(new Headers(calls[0].init?.headers).get("Idempotency-Key")).toBe(
      "operator-00000000-0000-4000-8000-000000000001",
    );
    expect(String(calls[0].init?.body)).toContain('"grouping_window_seconds":30');
  });

  it("uses the unique noise configuration and occurrence suppression endpoints", async () => {
    const calls = stubFetch(({ url }) => {
      if (url.endsWith("/noise-controls")) return { json: {
        source_id: "source-a",
        flapping_enabled: true,
        storm_enabled: true,
        storm_alert_threshold: 100,
        storm_occurrence_threshold: 20,
        storm_active: false,
        storm_started_at: null,
        version: 2,
      } };
      if (url.endsWith("/suppression/end")) return { json: {
        id: 8,
        occurrence_id: 3,
        starts_at: "2026-08-24T00:00:00Z",
        ends_at: "2026-08-24T01:00:00Z",
        reason: "planned rollout",
        status: "ENDED",
        ended_at: "2026-08-24T00:10:00Z",
        version: 2,
        created_at: "2026-08-24T00:00:00Z",
      } };
      return { status: 201, json: {
        id: 8,
        occurrence_id: 3,
        starts_at: "2026-08-24T00:00:00Z",
        ends_at: "2026-08-24T01:00:00Z",
        reason: "planned rollout",
        status: "ACTIVE",
        ended_at: null,
        version: 1,
        created_at: "2026-08-24T00:00:00Z",
      } };
    });

    await api.updateSourceNoiseControls("source-a", {
      flapping_enabled: true,
      storm_enabled: true,
      storm_alert_threshold: 100,
      storm_occurrence_threshold: 20,
    }, 1);
    await api.createOccurrenceSuppression(3, 3600, "planned rollout");
    await api.endOccurrenceSuppression(3, 1);

    expect(calls.map((call) => call.url)).toEqual([
      "/api/v1/sources/source-a/noise-controls",
      "/api/v1/occurrences/3/suppression",
      "/api/v1/occurrences/3/suppression/end",
    ]);
    expect(String(calls[0].init?.body)).toContain('"expected_version":1');
    expect(String(calls[1].init?.body)).toContain('"duration_seconds":3600');
  });

  it("surfaces the stable safe code, message, and request id", async () => {
    stubFetch(() => ({
      status: 409,
      text: JSON.stringify({
        error: {
          code: "SOURCE_VERSION_CONFLICT",
          message: "数据源已被其他修改更新，请刷新后重试",
          request_id: "req-42",
        },
      }),
    }));
    const error = await api.enableEventSource("source-a", 3).catch((caught) => caught);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      status: 409,
      code: "SOURCE_VERSION_CONFLICT",
      requestId: "req-42",
    });
    expect(error.message).toBe("数据源已被其他修改更新，请刷新后重试");
  });

  it("does not display an arbitrary proxy body when the error is not JSON", async () => {
    stubFetch(() => ({ status: 500, text: "" }));
    await expect(api.enableEventSource("source-a", 3)).rejects.toThrow(
      "请求未完成（HTTP 500）",
    );
  });

  it("sends the page CSRF token on mutations but never on reads", async () => {
    vi.stubGlobal("document", {
      querySelector: (selector: string) =>
        selector === 'meta[name="csrf-token"]'
          ? { content: "candidate-page-csrf-token" }
          : null,
    });
    const calls = stubFetch(() => ({ json: { url: "https://workbench.invalid" } }));

    await api.workbenchUrl();
    await api.saveWorkbenchUrl("https://workbench.invalid");

    expect(new Headers(calls[0].init?.headers).has("X-CSRF-Token")).toBe(false);
    expect(new Headers(calls[1].init?.headers).get("X-CSRF-Token")).toBe(
      "candidate-page-csrf-token",
    );
  });

  it("reuses an idempotency key after a lost response", async () => {
    let attempts = 0;
    const calls = stubFetch(() => {
      attempts += 1;
      if (attempts === 1) throw new TypeError("connection closed after send");
      return { json: { id: 1, name: "Stable", priority: 10, enabled: true, group_labels: [], scope_mode: "ALL", source_ids: [], matchers: [], version: 1 } };
    });
    const draft = { name: "Stable", priority: 10, enabled: true, group_by: [], source_scope: { mode: "ALL", source_ids: [] }, matchers: [] } as never;

    await expect(api.createAggregationRule(draft)).rejects.toThrow();
    await api.createAggregationRule(draft);

    const first = new Headers(calls[0].init?.headers).get("Idempotency-Key");
    const second = new Headers(calls[1].init?.headers).get("Idempotency-Key");
    expect(first).toBeTruthy();
    expect(second).toBe(first);
  });

  it("keeps the intent key for unknown and server outcomes", async () => {
    let attempts = 0;
    const calls = stubFetch(() => {
      attempts += 1;
      if (attempts === 1) return { status: 409, text: JSON.stringify({ error: { code: "COMMAND_OUTCOME_UNKNOWN" } }) };
      if (attempts === 2) return { status: 502, text: "bad gateway" };
      return { status: 201, json: { id: 2, name: "Unknown", priority: 10, enabled: true, group_labels: [], scope_mode: "ALL", source_ids: [], matchers: [], version: 1 } };
    });
    const draft = { name: "Unknown", priority: 10, enabled: true, group_by: [], source_scope: { mode: "ALL", source_ids: [] }, matchers: [] } as never;

    await expect(api.createAggregationRule(draft)).rejects.toMatchObject({ code: "COMMAND_OUTCOME_UNKNOWN" });
    await expect(api.createAggregationRule(draft)).rejects.toMatchObject({ status: 502 });
    await api.createAggregationRule(draft);

    const keys = calls.map((call) => new Headers(call.init?.headers).get("Idempotency-Key"));
    expect(keys[0]).toBeTruthy();
    expect(keys).toEqual([keys[0], keys[0], keys[0]]);
  });
});
