import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";
import {
  EVENT_FALLBACK_POLL_MS,
  EVENT_REFRESH_DEBOUNCE_MS,
  subscribePlatformEvents,
} from "./platformEvents";

function streamFixture() {
  const listeners = new Map<string, EventListener>();
  return {
    listeners,
    close: vi.fn(),
    addEventListener: vi.fn((type: string, listener: EventListener) => {
      listeners.set(type, listener);
    }),
    onerror: null as ((event: Event) => void) | null,
    onopen: null as ((event: Event) => void) | null,
  };
}

describe("platform event subscription", () => {
  it("coalesces a persisted event burst into one cache refresh", () => {
    const client = new QueryClient();
    const invalidate = vi.spyOn(client, "invalidateQueries").mockResolvedValue();
    const stream = streamFixture();
    const callbacks: Array<() => void> = [];
    const schedule = vi.fn((callback: () => void) => {
      callbacks.push(callback);
      return callbacks.length as unknown as ReturnType<typeof setTimeout>;
    });
    const cancel = vi.fn();
    const unsubscribe = subscribePlatformEvents(client, {
      createStream: () => stream,
      setTimeout: schedule as unknown as typeof setTimeout,
      clearTimeout: cancel as typeof clearTimeout,
    });

    stream.listeners.get("job.pending")?.(new Event("job.pending"));
    stream.listeners.get("job.running")?.(new Event("job.running"));
    stream.listeners.get("job.succeeded")?.(new Event("job.succeeded"));
    expect(invalidate).not.toHaveBeenCalled();
    expect(schedule).toHaveBeenLastCalledWith(expect.any(Function), EVENT_REFRESH_DEBOUNCE_MS);
    expect(cancel).toHaveBeenCalledTimes(2);

    callbacks.at(-1)?.();
    expect(invalidate).toHaveBeenCalledOnce();

    unsubscribe();
    expect(stream.close).toHaveBeenCalledOnce();
  });

  it("falls back to bounded cache refresh when the stream disconnects", () => {
    const client = new QueryClient();
    const stream = streamFixture();
    const schedule = vi.fn(() => 42 as unknown as ReturnType<typeof setInterval>);
    const cancel = vi.fn();
    subscribePlatformEvents(client, {
      createStream: () => stream,
      setInterval: schedule as typeof setInterval,
      clearInterval: cancel as typeof clearInterval,
    });

    stream.onerror?.(new Event("error"));
    expect(stream.close).toHaveBeenCalledOnce();
    expect(schedule).toHaveBeenCalledWith(expect.any(Function), EVENT_FALLBACK_POLL_MS);

    stream.onopen?.(new Event("open"));
    expect(cancel).toHaveBeenCalled();
  });
});
