import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { PromptGuidance } from "../types";

const key = ["prompt-profiles"] as const;

export function usePromptProfiles() {
  return useQuery({ queryKey: key, queryFn: () => api.promptProfiles() });
}

export function usePromptProfileRevisions(profileId: string | null, enabled: boolean) {
  return useQuery({
    queryKey: [...key, profileId, "revisions"],
    queryFn: () => api.promptProfileRevisions(profileId ?? "builtin-standard"),
    enabled: enabled && profileId !== null,
  });
}

function invalidate(client: ReturnType<typeof useQueryClient>) {
  return client.invalidateQueries({ queryKey: key });
}

export function useCopyPromptProfile() {
  const client = useQueryClient();
  return useMutation({ mutationFn: (name: string) => api.copyPromptProfile(name), onSuccess: () => invalidate(client) });
}

export function useUpdatePromptProfile() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (value: { id: string; guidance: PromptGuidance; revision: number }) => api.updatePromptProfile(value.id, value.guidance, value.revision),
    onSuccess: () => invalidate(client),
  });
}

export function usePreviewPromptProfile() {
  return useMutation({ mutationFn: (value: { id: string; revision: number }) => api.previewPromptProfile(value.id, value.revision) });
}

export function useTestPromptProfile() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (value: { id: string; revision: number }) => api.testPromptProfile(value.id, value.revision),
    onSuccess: () => invalidate(client),
  });
}

export function useActivatePromptProfile() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (value: { id: string; revision: number; globalDefault: boolean; serviceIds: number[] }) => api.activatePromptProfile(value.id, value.revision, value.globalDefault, value.serviceIds),
    onSuccess: () => invalidate(client),
  });
}
