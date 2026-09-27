import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type {
  ModelChannel,
  ModelChannelDraft,
  ModelChannelTestResult,
} from "../types";

export const modelChannelKeys = {
  all: () => ["model-channels"] as const,
};

export function useModelChannels() {
  return useQuery({
    queryKey: modelChannelKeys.all(),
    queryFn: () => api.modelChannels(),
  });
}

function useInvalidateModelChannels() {
  const queryClient = useQueryClient();
  return () =>
    queryClient.invalidateQueries({ queryKey: modelChannelKeys.all() });
}

export function useSaveModelChannel() {
  const invalidate = useInvalidateModelChannels();
  return useMutation({
    mutationFn: ({
      channelId,
      expectedRevisionId,
      draft,
    }: {
      channelId: number | null;
      expectedRevisionId: number | null;
      draft: ModelChannelDraft;
    }) =>
      channelId === null
        ? api.createModelChannel(draft)
        : api.updateModelChannel(
            channelId,
            draft,
            expectedRevisionId as number,
          ),
    onSuccess: () => invalidate(),
  });
}

export function useModelChannelAction() {
  const invalidate = useInvalidateModelChannels();
  return useMutation<
    ModelChannel | ModelChannelTestResult,
    Error,
    {
      action: "test" | "activate" | "enable" | "disable";
      channelId: number;
      revisionId: number;
    }
  >({
    mutationFn: ({
      action,
      channelId,
      revisionId,
    }: {
      action: "test" | "activate" | "enable" | "disable";
      channelId: number;
      revisionId: number;
    }) => {
      if (action === "test") return api.testModelChannel(revisionId);
      if (action === "activate") return api.activateModelChannel(revisionId);
      if (action === "enable") return api.enableModelChannel(channelId);
      return api.disableModelChannel(channelId);
    },
    onSuccess: () => invalidate(),
  });
}

/**
 * Populate the model picker from the service the user configured.
 *
 * A mutation because it causes an outbound request. It is free and read-only
 * upstream, so unlike a contract test this one is safe to press repeatedly —
 * but it still should not look like a cacheable fetch.
 */
/** Vendor presets. Static per build, so a plain query with no polling. */
/** Whether outbound model calls are faked by the default local-safe or mock mode. */
export function useModelRuntime() {
  return useQuery({
    queryKey: ["modelRuntime"],
    queryFn: () => api.modelRuntime(),
    staleTime: 60_000,
  });
}

export function useModelVendors() {
  return useQuery({
    queryKey: ["modelVendors"],
    queryFn: () => api.modelVendors(),
    staleTime: Infinity,
  });
}

export function useListModelChannelModels() {
  return useMutation({
    mutationFn: (revisionId: number) => api.listModelChannelModels(revisionId),
  });
}
