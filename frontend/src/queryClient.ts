/**
 * Query defaults for a local, single-user workbench.
 *
 * The backend is on localhost, so a failed request is almost always a real
 * failure rather than a flaky network: retrying hides the one thing the user
 * needs to see (the backend is down, or the source is misconfigured). Failures
 * surface immediately and `requestError.ts` decides how they read.
 */
import { QueryClient } from "@tanstack/react-query";

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        // Polled data is meant to be current; refetching on focus on top of the
        // interval would only add requests nobody asked for.
        refetchOnWindowFocus: false,
        // Keep showing the previous page of data while a changed filter loads,
        // so the list does not blank out between keystrokes.
        placeholderData: <T,>(previous: T) => previous,
      },
      mutations: { retry: false },
    },
  });
}
