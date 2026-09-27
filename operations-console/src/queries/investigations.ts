import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { InvestigatorRun } from "../types";
import { queryKeys } from "./keys";

const ACTIVE_POLL_MS = 2_000;
const RECOVERY_POLL_MS = 15_000;

export function investigationRefetchInterval(
  investigations: ReadonlyArray<Pick<InvestigatorRun, "status">> | undefined,
  hasError: boolean,
): number | false {
  if (investigations?.some((item) => item.status === "QUEUED" || item.status === "RUNNING")) {
    return ACTIVE_POLL_MS;
  }
  return investigations !== undefined && hasError ? RECOVERY_POLL_MS : false;
}

export function useOccurrenceInvestigations(occurrenceId: number | null) {
  return useQuery({
    queryKey: queryKeys.occurrenceInvestigations(occurrenceId ?? 0),
    queryFn: () => api.occurrenceInvestigations(occurrenceId ?? 0),
    enabled: occurrenceId !== null,
    refetchInterval: (query) => investigationRefetchInterval(
      query.state.data,
      query.state.error !== null,
    ),
  });
}

export function useLegacyOccurrenceInvestigations(occurrenceId: number | null) {
  return useQuery({
    queryKey: queryKeys.legacyOccurrenceInvestigations(occurrenceId ?? 0),
    queryFn: () => api.legacyOccurrenceInvestigations(occurrenceId ?? 0),
    enabled: occurrenceId !== null,
    staleTime: 60_000,
  });
}

export function useStartOccurrenceInvestigation(occurrenceId: number | null) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => api.startOccurrenceInvestigation(occurrenceId ?? 0),
    onSuccess: async () => {
      if (occurrenceId !== null) {
        await client.invalidateQueries({ queryKey: queryKeys.occurrenceInvestigations(occurrenceId) });
      }
    },
  });
}

export function useCancelInvestigation(occurrenceId: number | null) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (investigationId: string) => api.cancelInvestigation(investigationId),
    onSuccess: async () => {
      if (occurrenceId !== null) {
        await client.invalidateQueries({ queryKey: queryKeys.occurrenceInvestigations(occurrenceId) });
      }
    },
  });
}

export function useInvestigationFeedback(occurrenceId: number | null) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (value: {
      investigationId: string;
      rating: "USEFUL" | "NOT_USEFUL" | "ADOPTED";
    }) => api.recordInvestigationFeedback(value.investigationId, value.rating),
    onSuccess: async () => {
      if (occurrenceId !== null) {
        await client.invalidateQueries({ queryKey: queryKeys.occurrenceInvestigations(occurrenceId) });
      }
    },
  });
}
