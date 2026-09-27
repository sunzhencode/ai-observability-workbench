import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "./client";

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
  it("returns the parsed body", async () => {
    stubFetch(() => ({ json: { ok: true } }));
    await expect(api.health()).resolves.toEqual({ ok: true });
  });

  it("throws with the status code when the response is not ok", async () => {
    stubFetch(() => ({ status: 503 }));
    await expect(api.health()).rejects.toThrow("Request failed (503)");
  });

  it("repeats source_ids once per selected source", async () => {
    const calls = stubFetch(() => ({ json: [] }));
    await api.incidents({ sourceIds: ["source-a", "source-b"] });
    expect(calls[0].url).toBe("/api/incidents?source_ids=source-a&source_ids=source-b");
  });

  it("sends no query string when no source is selected", async () => {
    const calls = stubFetch(() => ({ json: [] }));
    await api.incidents();
    expect(calls[0].url).toBe("/api/incidents");
    await api.incidents({ sourceIds: [] });
    expect(calls[1].url).toBe("/api/incidents");
  });

  it("percent-encodes source ids", async () => {
    const calls = stubFetch(() => ({ json: [] }));
    await api.incidents({ sourceIds: ["a b&c"] });
    expect(calls[0].url).toBe("/api/incidents?source_ids=a+b%26c");
  });
});

describe("write requests", () => {
  it("surfaces the server's message body so validation errors stay readable", async () => {
    stubFetch(() => ({ status: 409, text: "source has unapplied changes" }));
    await expect(api.enableEventSource("source-a", 3)).rejects.toThrow(
      "source has unapplied changes",
    );
  });

  it("falls back to the status code when the error body is empty", async () => {
    stubFetch(() => ({ status: 500, text: "" }));
    await expect(api.enableEventSource("source-a", 3)).rejects.toThrow(
      "Request failed (500)",
    );
  });

  it("PATCHes handling changes as JSON", async () => {
    const calls = stubFetch(() => ({ json: {} }));
    await api.updateHandling(7, "IN_PROGRESS", "看一下");
    expect(calls[0].url).toBe("/api/incidents/7/handling");
    expect(calls[0].init?.method).toBe("PATCH");
    expect(JSON.parse(String(calls[0].init?.body))).toMatchObject({
      state: "IN_PROGRESS",
      reason: "看一下",
    });
  });
});
