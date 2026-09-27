/**
 * Every query key in one place.
 *
 * The point is that a resource has exactly one key: before F23 the source list
 * was fetched independently by the shell and by three pages, so four copies of
 * it could disagree with each other and nothing made them converge. A shared
 * key is what makes "write once, every page sees it" true.
 *
 * Keys are built here rather than inline so that an invalidation and the query
 * it is meant to hit cannot drift apart.
 */
export const queryKeys = {
  health: () => ["health"] as const,

  // Keyed on the alert and the window: the same group's other members answer a
  // different question, and switching window must not reuse the previous chart.
  metricEvidence: (alertId: number, windowMode: string) =>
    ["metric-evidence", alertId, windowMode] as const,
  metricTemplates: () => ["metric-templates"] as const,

  incidents: (sourceIds: readonly string[]) =>
    ["incidents", { sourceIds: [...sourceIds].sort() }] as const,
  incident: (id: number) => ["incident", id] as const,

  // Filter and cursor belong in the key: a refetch must not be able to answer
  // with a different page than the one the URL asks for.
  incidentOccurrences: (filters: {
    sourceIds: readonly string[];
    conclusion: string | null;
    beforeId: number | null;
  }) =>
    [
      "incident-occurrences",
      {
        sourceIds: [...filters.sourceIds].sort(),
        conclusion: filters.conclusion,
        beforeId: filters.beforeId,
      },
    ] as const,

  rules: () => ["rules"] as const,
  aggregationLabels: (lookbackHours: number) =>
    ["aggregation-labels", lookbackHours] as const,

  eventSources: (includeArchived: boolean) => ["event-sources", { includeArchived }] as const,
  eventSourceAudit: (sourceId: string) => ["event-source-audit", sourceId] as const,
  watchdogClusters: (sourceId: string) => ["watchdog-clusters", sourceId] as const,
  grafanaDashboards: (sourceId: string) => ["grafana-dashboards", sourceId] as const,
} as const;
