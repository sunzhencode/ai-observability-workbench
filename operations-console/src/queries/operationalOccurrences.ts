import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type {
  OperationalSignalState,
  QueueView,
  ResolutionCode,
  IncidentTaskState,
} from "../types";
import { queryKeys } from "./keys";

export const INCIDENT_QUEUE_PAGE_SIZE = 50;

export interface OperationalOccurrenceFilters {
  view: QueueView;
  sourceIds: readonly string[];
  signalStates: readonly OperationalSignalState[];
  cursor: string | null;
}

export function useOperationalOccurrences(filters: OperationalOccurrenceFilters) {
  return useQuery({
    queryKey: queryKeys.operationalOccurrences(filters),
    queryFn: () => api.operationalOccurrences({
      view: filters.view,
      sourceIds: [...filters.sourceIds],
      signalStates: [...filters.signalStates],
      cursor: filters.cursor,
      limit: INCIDENT_QUEUE_PAGE_SIZE,
    }),
    refetchInterval: 15_000,
  });
}

export function useOperationalOccurrence(id: number | null) {
  return useQuery({
    queryKey: queryKeys.operationalOccurrence(id ?? 0),
    queryFn: () => api.operationalOccurrence(id ?? 0),
    enabled: id !== null,
    refetchInterval: 15_000,
  });
}

export function useOccurrenceTimeline(id: number | null) {
  return useQuery({
    queryKey: queryKeys.occurrenceTimeline(id ?? 0),
    queryFn: () => api.occurrenceTimeline(id ?? 0),
    enabled: id !== null,
    refetchInterval: 15_000,
  });
}

export function useOccurrenceTasks(id: number | null) {
  return useQuery({
    queryKey: queryKeys.occurrenceTasks(id ?? 0),
    queryFn: () => api.occurrenceTasks(id ?? 0),
    enabled: id !== null,
    refetchInterval: 15_000,
  });
}

export function useSimilarOccurrenceHistory(id: number | null) {
  return useQuery({
    queryKey: queryKeys.similarOccurrenceHistory(id ?? 0),
    queryFn: () => api.similarOccurrenceHistory(id ?? 0),
    enabled: id !== null,
    staleTime: 15_000,
  });
}

function useRefreshOccurrence(id: number | null) {
  const client = useQueryClient();
  return async () => {
    await Promise.all([
      client.invalidateQueries({ queryKey: ["operational-occurrences"] }),
      id === null ? Promise.resolve() : client.invalidateQueries({ queryKey: queryKeys.operationalOccurrence(id) }),
      id === null ? Promise.resolve() : client.invalidateQueries({ queryKey: queryKeys.occurrenceTimeline(id) }),
      id === null ? Promise.resolve() : client.invalidateQueries({ queryKey: queryKeys.occurrenceTasks(id) }),
      id === null ? Promise.resolve() : client.invalidateQueries({ queryKey: queryKeys.similarOccurrenceHistory(id) }),
    ]);
  };
}

export function useCreateOccurrenceTask(id: number | null) {
  const refresh = useRefreshOccurrence(id);
  return useMutation({
    mutationFn: (draft: {
      title: string;
      description: string | null;
      due_at: string | null;
      runbook_link: string | null;
    }) => api.createOccurrenceTask(id ?? 0, draft),
    onSuccess: refresh,
  });
}

export function useTransitionOccurrenceTask(id: number | null) {
  const refresh = useRefreshOccurrence(id);
  return useMutation({
    mutationFn: ({ taskId, expectedVersion, target, result, reason }: {
      taskId: number;
      expectedVersion: number;
      target: Exclude<IncidentTaskState, "TODO">;
      result: string | null;
      reason: string | null;
    }) => api.transitionOccurrenceTask(id ?? 0, taskId, expectedVersion, target, result, reason),
    onSuccess: refresh,
  });
}

export function useAddOccurrenceNote(id: number | null) {
  const refresh = useRefreshOccurrence(id);
  return useMutation({
    mutationFn: (text: string) => api.addOccurrenceNote(id ?? 0, text),
    onSuccess: refresh,
  });
}

export function useRedactOccurrenceNote(id: number | null) {
  const refresh = useRefreshOccurrence(id);
  return useMutation({
    mutationFn: ({ sequence, reason }: { sequence: number; reason: string }) =>
      api.redactOccurrenceNote(id ?? 0, sequence, reason),
    onSuccess: refresh,
  });
}

export function useStartHandlingOccurrence(id: number | null) {
  const refresh = useRefreshOccurrence(id);
  return useMutation({
    mutationFn: ({ expectedVersion, reason }: { expectedVersion: number; reason: string | null }) =>
      api.startHandlingOccurrence(id ?? 0, expectedVersion, reason),
    onSuccess: refresh,
  });
}

export function useResolveOccurrence(id: number | null) {
  const refresh = useRefreshOccurrence(id);
  return useMutation({
    mutationFn: ({ expectedVersion, resolutionCode, duplicateOfOccurrenceId, reason }: {
      expectedVersion: number;
      resolutionCode: ResolutionCode;
      duplicateOfOccurrenceId: number | null;
      reason: string | null;
    }) => api.resolveOccurrence(id ?? 0, expectedVersion, resolutionCode, duplicateOfOccurrenceId, reason),
    onSuccess: refresh,
  });
}

export function useBatchStartHandlingOccurrences() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ items, reason }: {
      items: { occurrence_id: number; expected_version: number }[];
      reason: string | null;
    }) => api.batchStartHandlingOccurrences(items, reason),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ["operational-occurrences"] });
    },
  });
}
