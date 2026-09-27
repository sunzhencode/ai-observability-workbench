/**
 * Aggregation rules and event sources — the two lists more than one page needs.
 *
 * `event-sources` is the reason this file exists: the shell and three pages each
 * fetched it on their own, so four copies of the registry could disagree and
 * nothing brought them back together. One key, one copy.
 */
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { queryKeys } from "./keys";

export function useRules() {
  return useQuery({
    queryKey: queryKeys.rules(),
    queryFn: () => api.aggregationRules(),
  });
}

export function useEventSources(includeArchived = false) {
  return useQuery({
    queryKey: queryKeys.eventSources(includeArchived),
    // Archived sources come from a different server-side set, so this belongs
    // in the key rather than being filtered after the fact.
    queryFn: () => api.eventSources(includeArchived),
  });
}
