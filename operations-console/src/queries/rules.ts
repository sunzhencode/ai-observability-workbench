/**
 * Aggregation-rule reads and writes.
 *
 * Publishing a rule regroups existing incidents, so a successful save has to
 * invalidate the incident list too — that is what the old `onRulesChanged`
 * callback into the shell was compensating for.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { AggregationRuleDraft } from "../types";
import { queryKeys } from "./keys";

export type LabelWindow = 24 | 168 | 720;

export function useAggregationLabels(lookbackHours: LabelWindow) {
  return useQuery({
    queryKey: queryKeys.aggregationLabels(lookbackHours),
    queryFn: () => api.aggregationLabels(lookbackHours),
  });
}

/**
 * A preview writes nothing, but it is still a POST with a body, so it is a
 * mutation rather than a query: nothing should re-run it in the background.
 */
export function usePreviewAggregationRule() {
  return useMutation({
    mutationFn: ({ draft, ruleId }: { draft: AggregationRuleDraft; ruleId: number | null }) =>
      api.previewAggregationRule(draft, ruleId),
  });
}

export function useSaveAggregationRule() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ draft, ruleId }: { draft: AggregationRuleDraft; ruleId: number | null }) =>
      ruleId === null
        ? api.createAggregationRule(draft)
        : api.updateAggregationRule(ruleId, draft),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: queryKeys.rules() }),
        // Saving republishes: every incident may have been regrouped.
        queryClient.invalidateQueries({ queryKey: ["incidents"] }),
      ]);
    },
  });
}
