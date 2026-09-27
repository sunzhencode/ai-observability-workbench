import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { ServiceDraft, ServiceMappingRuleDraft } from "../types";
import { queryKeys } from "./keys";

export function useServices(includeArchived = false) {
  return useQuery({
    queryKey: queryKeys.services(includeArchived),
    queryFn: () => api.services(includeArchived),
  });
}

export function useSaveService() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, draft, expectedVersion }: {
      id: number | null;
      draft: ServiceDraft;
      expectedVersion: number | null;
    }) => id === null
      ? api.createService(draft)
      : api.updateService(id, draft, expectedVersion ?? 0),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ["services"] });
    },
  });
}

export function useArchiveService() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, expectedVersion }: { id: number; expectedVersion: number }) =>
      api.archiveService(id, expectedVersion),
    onSuccess: async () => {
      await Promise.all([
        client.invalidateQueries({ queryKey: ["services"] }),
        client.invalidateQueries({ queryKey: ["operational-occurrences"] }),
      ]);
    },
  });
}

export function useServiceMappingRules() {
  return useQuery({
    queryKey: queryKeys.serviceMappingRules(),
    queryFn: () => api.serviceMappingRules(),
  });
}

export function useSaveServiceMappingRule() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, draft, expectedVersion }: {
      id: number | null;
      draft: ServiceMappingRuleDraft;
      expectedVersion: number | null;
    }) => id === null
      ? api.createServiceMappingRule(draft)
      : api.updateServiceMappingRule(id, draft, expectedVersion ?? 0),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: queryKeys.serviceMappingRules() });
    },
  });
}

export function usePreviewServiceMappingRule() {
  return useMutation({ mutationFn: (id: number) => api.previewServiceMappingRule(id) });
}

export function usePublishServiceMappingRule() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, expectedVersion }: { id: number; expectedVersion: number }) =>
      api.publishServiceMappingRule(id, expectedVersion),
    onSuccess: async () => {
      await Promise.all([
        client.invalidateQueries({ queryKey: queryKeys.serviceMappingRules() }),
        client.invalidateQueries({ queryKey: ["operational-occurrences"] }),
        client.invalidateQueries({ queryKey: ["occurrence-timeline"] }),
      ]);
    },
  });
}

export function useAssignOccurrenceService(occurrenceId: number | null) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ serviceId, expectedVersion }: {
      serviceId: number;
      expectedVersion: number;
    }) => api.assignOccurrenceService(occurrenceId ?? 0, serviceId, expectedVersion),
    onSuccess: async () => {
      await Promise.all([
        client.invalidateQueries({ queryKey: ["operational-occurrences"] }),
        occurrenceId === null
          ? Promise.resolve()
          : client.invalidateQueries({ queryKey: queryKeys.operationalOccurrence(occurrenceId) }),
        occurrenceId === null
          ? Promise.resolve()
          : client.invalidateQueries({ queryKey: queryKeys.occurrenceTimeline(occurrenceId) }),
      ]);
    },
  });
}
