/**
 * Everything 系统设置 reads and writes.
 *
 * The source list itself is in `catalog.ts` because four pages share it. What
 * lives here is the per-source detail only this page needs, plus the writes.
 *
 * Every mutation invalidates rather than patching local state: a save can move
 * lifecycle, freshness and endpoint identity at once, and the server is the
 * only thing that knows the result.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { EventSourceCandidate } from "../types";
import { queryKeys } from "./keys";

export function useEventSourceAudit(sourceId: string | null) {
  return useQuery({
    queryKey: queryKeys.eventSourceAudit(sourceId ?? ""),
    queryFn: () => api.eventSourceAudit(sourceId as string),
    enabled: sourceId !== null,
  });
}

export function useWatchdogClusters(sourceId: string | null) {
  return useQuery({
    queryKey: queryKeys.watchdogClusters(sourceId ?? ""),
    queryFn: () => api.watchdogClusters(sourceId as string),
    enabled: sourceId !== null,
  });
}

/** Invalidate every list that shows sources, archived or not. */
function useSourceListInvalidation() {
  const queryClient = useQueryClient();
  return () => queryClient.invalidateQueries({ queryKey: ["event-sources"] });
}

export function useSaveEventSource() {
  const invalidateSources = useSourceListInvalidation();
  return useMutation({
    mutationFn: ({
      sourceId,
      draft,
      version,
    }: {
      sourceId: string | null;
      draft: EventSourceCandidate;
      version: number | null;
    }) =>
      sourceId === null
        ? api.createEventSource({ ...draft, enable: true })
        : api.updateEventSource(sourceId, draft, version as number),
    onSuccess: () => invalidateSources(),
  });
}

export type SourceLifecycleAction = "enable" | "disable" | "archive";

export function useEventSourceLifecycle() {
  const invalidateSources = useSourceListInvalidation();
  return useMutation({
    mutationFn: ({
      sourceId,
      version,
      action,
    }: {
      sourceId: string;
      version: number;
      action: SourceLifecycleAction;
    }) => {
      if (action === "enable") return api.enableEventSource(sourceId, version);
      if (action === "disable") return api.disableEventSource(sourceId, version);
      return api.archiveEventSource(sourceId, version);
    },
    onSuccess: () => invalidateSources(),
  });
}

export function useTestEventSource() {
  const invalidateSources = useSourceListInvalidation();
  return useMutation({
    mutationFn: (sourceId: string) => api.testEventSource(sourceId),
    // The test records its outcome on the source, so the list's status pill
    // moves too. It does not bump `version`, which is what keeps the form and
    // this result from being reset underneath the user.
    onSuccess: () => invalidateSources(),
  });
}

export function useAddWatchdogCluster() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ sourceId, cluster }: { sourceId: string; cluster: string }) =>
      api.addWatchdogCluster(sourceId, cluster),
    onSuccess: (_result, { sourceId }) =>
      queryClient.invalidateQueries({ queryKey: queryKeys.watchdogClusters(sourceId) }),
  });
}

export function useUpdateWatchdogCluster() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      sourceId,
      clusterId,
      action,
      reason,
    }: {
      sourceId: string;
      clusterId: number;
      action: "EXPECT" | "IGNORE" | "RESTORE";
      reason?: string;
    }) => api.updateWatchdogCluster(sourceId, clusterId, action, reason),
    onSuccess: (_result, { sourceId }) =>
      queryClient.invalidateQueries({ queryKey: queryKeys.watchdogClusters(sourceId) }),
  });
}
