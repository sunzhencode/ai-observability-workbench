/**
 * The occurrence history (F26 / CAP-11).
 *
 * No `refetchInterval`: history only grows when a poll seals an occurrence, and
 * a page of the past does not go stale while you read it. The alerts list polls
 * because it answers "what is happening now"; this one does not, and a spinner
 * every 15 seconds on immutable rows would be pure noise.
 */
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { HandlingState } from "../types";
import { queryKeys } from "./keys";

export const HISTORY_PAGE_SIZE = 25;

export function useIncidentOccurrences(filters: {
  sourceIds: readonly string[];
  conclusion: HandlingState | null;
  beforeId: string | null;
}) {
  return useQuery({
    queryKey: queryKeys.incidentOccurrences(filters),
    queryFn: () =>
      api.incidentOccurrences({
        sourceIds: [...filters.sourceIds],
        conclusion: filters.conclusion,
        cursor: filters.beforeId,
        limit: HISTORY_PAGE_SIZE,
      }),
  });
}
