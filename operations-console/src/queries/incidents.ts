/**
 * Incident list and detail.
 *
 * The detail query is keyed on the incident id alone. The shell used to refetch
 * it whenever the `incidents` array changed — every 15 seconds, whether or not
 * anything about that incident had moved. Here a list refresh only touches the
 * detail when the caller invalidates it.
 */
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { queryKeys } from "./keys";

/** Unchanged from the shell's old top-level poll. */
export const INCIDENT_POLL_MS = 15000;

export function useIncidents(sourceIds: readonly string[]) {
  return useQuery({
    queryKey: queryKeys.incidents(sourceIds),
    // Source filtering is server-side, so the filter belongs in the key: a
    // refetch can never widen the list to every source the way a hand-written
    // refresh once did.
    queryFn: () => api.incidents({ sourceIds: [...sourceIds] }),
    refetchInterval: INCIDENT_POLL_MS,
  });
}

export function useIncident(incidentId: number | null) {
  return useQuery({
    queryKey: queryKeys.incident(incidentId ?? -1),
    queryFn: () => api.incident(incidentId as number),
    enabled: incidentId !== null,
  });
}
