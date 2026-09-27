/**
 * Metric evidence and the template catalogue (F27 / CAP-12).
 *
 * **No `refetchInterval`.** Curves are "I opened this alert" data, not a live
 * feed: the top-level polling F23 removed must not creep back in as a per-chart
 * timer. The reader is given the query time and an explicit refresh instead,
 * which is honest about staleness without spending queries nobody asked for.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { MetricTemplate } from "../types";
import { queryKeys } from "./keys";

export function useMetricEvidence(alertId: number | null, windowMode: string) {
  return useQuery({
    queryKey: queryKeys.metricEvidence(alertId ?? 0, windowMode),
    queryFn: () => api.metricEvidence(alertId as number, windowMode),
    // Nothing is fetched until a member is actually selected.
    enabled: alertId != null,
    staleTime: 30_000,
    retry: false,
  });
}

export function useMetricTemplates() {
  return useQuery({
    queryKey: queryKeys.metricTemplates(),
    queryFn: () => api.metricTemplates(),
  });
}

export function useUpdateMetricTemplate() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({
      templateId,
      body,
    }: {
      templateId: number;
      body: Partial<Pick<MetricTemplate, "enabled" | "priority" | "promql" | "name" | "display_unit" | "source_scope">>;
    }) => api.updateMetricTemplate(templateId, body),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: queryKeys.metricTemplates() });
      // A template change alters which auxiliary curves are drawn.
      client.invalidateQueries({ queryKey: ["metric-evidence"] });
    },
  });
}

export function useDuplicateMetricTemplate() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (template: MetricTemplate) =>
      api.createMetricTemplate({
        name: `${template.name} 副本`,
        promql: template.promql,
        required_labels: template.required_labels,
        description: template.description,
        priority: template.priority,
        display_unit: template.display_unit,
        source_scope: template.source_scope,
      }),
    onSuccess: () => void client.invalidateQueries({ queryKey: queryKeys.metricTemplates() }),
  });
}

/**
 * Ask the model to explain this alert. **Explicit click only** (D13).
 *
 * A mutation, not a query: it costs money, so no render, refetch or poll may
 * ever trigger it.
 */
export function useInvestigateAlert() {
  return useMutation({
    mutationFn: (alertId: number) => api.investigateAlert(alertId),
  });
}
