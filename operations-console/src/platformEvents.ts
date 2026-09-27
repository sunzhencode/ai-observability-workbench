import type { QueryClient } from "@tanstack/react-query";

export const EVENT_FALLBACK_POLL_MS = 15_000;
export const EVENT_REFRESH_DEBOUNCE_MS = 1_500;

const PLATFORM_EVENT_TYPES = [
  "job.pending",
  "job.running",
  "job.retry_wait",
  "job.succeeded",
  "job.failed",
] as const;

interface EventStream {
  addEventListener(type: string, listener: EventListener): void;
  close(): void;
  onerror: ((event: Event) => void) | null;
  onopen: ((event: Event) => void) | null;
}

interface SubscriptionDependencies {
  createStream?: (url: string) => EventStream;
  setInterval?: typeof globalThis.setInterval;
  clearInterval?: typeof globalThis.clearInterval;
  setTimeout?: typeof globalThis.setTimeout;
  clearTimeout?: typeof globalThis.clearTimeout;
}

/**
 * Keep cached read models convergent with the persisted event stream.
 *
 * The stream is an acceleration path, not the only refresh path. If it cannot
 * stay connected, a bounded timer invalidates visible queries until the next
 * page load can establish a fresh stream.
 */
export function subscribePlatformEvents(
  queryClient: QueryClient,
  dependencies: SubscriptionDependencies = {},
): () => void {
  const createStream = dependencies.createStream ?? ((url) => new EventSource(url));
  const schedule = dependencies.setInterval ?? globalThis.setInterval;
  const cancel = dependencies.clearInterval ?? globalThis.clearInterval;
  const delay = dependencies.setTimeout ?? globalThis.setTimeout;
  const cancelDelay = dependencies.clearTimeout ?? globalThis.clearTimeout;
  const stream = createStream("/api/v1/events");
  let fallbackTimer: ReturnType<typeof globalThis.setInterval> | null = null;
  let eventRefreshTimer: ReturnType<typeof globalThis.setTimeout> | null = null;
  let stopped = false;

  const refresh = () => void queryClient.invalidateQueries();
  const stopEventRefresh = () => {
    if (eventRefreshTimer !== null) cancelDelay(eventRefreshTimer);
    eventRefreshTimer = null;
  };
  const scheduleEventRefresh = () => {
    stopEventRefresh();
    eventRefreshTimer = delay(() => {
      eventRefreshTimer = null;
      refresh();
    }, EVENT_REFRESH_DEBOUNCE_MS);
  };
  const stopFallback = () => {
    if (fallbackTimer !== null) cancel(fallbackTimer);
    fallbackTimer = null;
  };
  const startFallback = () => {
    if (fallbackTimer === null && !stopped) {
      fallbackTimer = schedule(refresh, EVENT_FALLBACK_POLL_MS);
    }
  };

  for (const eventType of PLATFORM_EVENT_TYPES) {
    stream.addEventListener(eventType, scheduleEventRefresh as EventListener);
  }
  stream.onopen = stopFallback;
  stream.onerror = () => {
    stopEventRefresh();
    stream.close();
    startFallback();
  };

  return () => {
    stopped = true;
    stopEventRefresh();
    stopFallback();
    stream.close();
  };
}
