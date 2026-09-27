import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { MaintenanceWindowDraft, SourceNoiseControls } from "../types";
import { queryKeys } from "./keys";

export function useSourceNoiseControls(sourceId: string | null) {
  return useQuery({
    queryKey: queryKeys.sourceNoiseControls(sourceId ?? ""),
    queryFn: () => api.sourceNoiseControls(sourceId as string),
    enabled: sourceId !== null,
  });
}

export function useUpdateSourceNoiseControls(sourceId: string | null) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({
      value,
      expectedVersion,
    }: {
      value: Omit<SourceNoiseControls, "source_id" | "storm_active" | "storm_started_at" | "version">;
      expectedVersion: number;
    }) => api.updateSourceNoiseControls(sourceId as string, value, expectedVersion),
    onSuccess: async () => {
      if (sourceId !== null) {
        await client.invalidateQueries({ queryKey: queryKeys.sourceNoiseControls(sourceId) });
      }
    },
  });
}

export function useMaintenanceWindows(includeEnded = false) {
  return useQuery({
    queryKey: queryKeys.maintenanceWindows(includeEnded),
    queryFn: () => api.maintenanceWindows(includeEnded),
  });
}

export function useCreateMaintenanceWindow() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (draft: MaintenanceWindowDraft) => api.createMaintenanceWindow(draft),
    onSuccess: () => client.invalidateQueries({ queryKey: ["maintenance-windows"] }),
  });
}

export function useEndMaintenanceWindow() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, expectedVersion }: { id: number; expectedVersion: number }) =>
      api.endMaintenanceWindow(id, expectedVersion),
    onSuccess: () => client.invalidateQueries({ queryKey: ["maintenance-windows"] }),
  });
}

export function useOccurrenceNoise(occurrenceId: number | null) {
  return useQuery({
    queryKey: queryKeys.occurrenceNoise(occurrenceId ?? 0),
    queryFn: () => api.occurrenceNoise(occurrenceId as number),
    enabled: occurrenceId !== null,
  });
}

export function useCreateOccurrenceSuppression(occurrenceId: number | null) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({
      durationSeconds,
      reason,
    }: {
      durationSeconds: 900 | 3600 | 14400 | 86400;
      reason: string;
    }) => api.createOccurrenceSuppression(occurrenceId as number, durationSeconds, reason),
    onSuccess: async () => {
      if (occurrenceId === null) return;
      await Promise.all([
        client.invalidateQueries({ queryKey: queryKeys.occurrenceNoise(occurrenceId) }),
        client.invalidateQueries({ queryKey: queryKeys.operationalOccurrence(occurrenceId) }),
        client.invalidateQueries({ queryKey: ["operational-occurrences"] }),
        client.invalidateQueries({ queryKey: queryKeys.occurrenceTimeline(occurrenceId) }),
      ]);
    },
  });
}

export function useEndOccurrenceSuppression(occurrenceId: number | null) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (expectedVersion: number) =>
      api.endOccurrenceSuppression(occurrenceId as number, expectedVersion),
    onSuccess: async () => {
      if (occurrenceId === null) return;
      await Promise.all([
        client.invalidateQueries({ queryKey: queryKeys.occurrenceNoise(occurrenceId) }),
        client.invalidateQueries({ queryKey: queryKeys.operationalOccurrence(occurrenceId) }),
        client.invalidateQueries({ queryKey: ["operational-occurrences"] }),
        client.invalidateQueries({ queryKey: queryKeys.occurrenceTimeline(occurrenceId) }),
      ]);
    },
  });
}
