/**
 * Reading and writing the Grafana side of a source.
 *
 * The import drawer is a temporary flow, so its state stays in the component
 * and out of the URL — the same call already made for the channel test dialog.
 * What lives here is only what talks to the server.
 *
 * Preview is a mutation rather than a query on purpose: it is an explicit
 * action with a cost upstream (one dashboard fetch plus up to forty probe
 * queries), and a `useQuery` would re-run it on remount, refocus and every
 * cache decision react-query makes on its own.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { EventSourceGrafanaDraft, GrafanaImportItem } from "../types";
import { queryKeys } from "./keys";

export function useSaveGrafanaConfig() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      sourceId,
      draft,
    }: {
      sourceId: string;
      draft: EventSourceGrafanaDraft;
    }) => api.saveGrafanaConfig(sourceId, draft),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["event-sources"] }),
  });
}

export function useTestGrafanaConfig() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (sourceId: string) => api.testGrafanaConfig(sourceId),
    // The test result lives on the source, and the import button reads it from
    // there — so the list has to be refetched whether the test passed or not.
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["event-sources"] }),
  });
}

export function useGrafanaDashboards(sourceId: string | null, enabled: boolean) {
  return useQuery({
    queryKey: queryKeys.grafanaDashboards(sourceId ?? ""),
    queryFn: () => api.grafanaDashboards(sourceId as string, ""),
    enabled: sourceId !== null && enabled,
  });
}

export function usePreviewGrafanaImport() {
  return useMutation({
    mutationFn: ({
      sourceId,
      dashboardUid,
    }: {
      sourceId: string;
      dashboardUid: string;
    }) => api.previewGrafanaImport(sourceId, dashboardUid),
  });
}

export function useConfirmGrafanaImport() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      sourceId,
      items,
    }: {
      sourceId: string;
      items: GrafanaImportItem[];
    }) => api.confirmGrafanaImport(sourceId, items),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.metricTemplates() }),
  });
}
