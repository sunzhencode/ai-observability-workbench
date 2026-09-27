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
  analytics: (filters: {
    range: string;
    sourceId: string | null;
    serviceId: number | null;
    signalSeverity: string | null;
  }) => ["analytics", filters] as const,

  // Keyed on the alert and the window: the same group's other members answer a
  // different question, and switching window must not reuse the previous chart.
  metricEvidence: (alertId: number, windowMode: string) =>
    ["metric-evidence", alertId, windowMode] as const,
  metricTemplates: () => ["metric-templates"] as const,

  incidents: (sourceIds: readonly string[]) =>
    ["incidents", { sourceIds: [...sourceIds].sort() }] as const,
  incident: (id: number) => ["incident", id] as const,
  operationalOccurrences: (filters: {
    view: string;
    sourceIds: readonly string[];
    signalStates: readonly string[];
    cursor: string | null;
  }) => [
    "operational-occurrences",
    {
      view: filters.view,
      sourceIds: [...filters.sourceIds].sort(),
      signalStates: [...filters.signalStates].sort(),
      cursor: filters.cursor,
    },
  ] as const,
  operationalOccurrence: (id: number) => ["operational-occurrence", id] as const,
  occurrenceTimeline: (id: number) => ["occurrence-timeline", id] as const,
  occurrenceTasks: (id: number) => ["occurrence-tasks", id] as const,
  similarOccurrenceHistory: (id: number) => ["similar-occurrence-history", id] as const,
  occurrenceNoise: (id: number) => ["occurrence-noise", id] as const,
  occurrenceInvestigations: (id: number) => ["occurrence-investigations", id] as const,
  legacyOccurrenceInvestigations: (id: number) => ["legacy-occurrence-investigations", id] as const,

  // Filter and cursor belong in the key: a refetch must not be able to answer
  // with a different page than the one the URL asks for.
  incidentOccurrences: (filters: {
    sourceIds: readonly string[];
    conclusion: string | null;
    beforeId: string | null;
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
  services: (includeArchived: boolean) => ["services", { includeArchived }] as const,
  serviceMappingRules: () => ["service-mapping-rules"] as const,
  aggregationLabels: (lookbackHours: number) =>
    ["aggregation-labels", lookbackHours] as const,

  eventSources: (includeArchived: boolean) => ["event-sources", { includeArchived }] as const,
  eventSourceAudit: (sourceId: string) => ["event-source-audit", sourceId] as const,
  sourceNoiseControls: (sourceId: string) => ["source-noise-controls", sourceId] as const,
  maintenanceWindows: (includeEnded: boolean) => ["maintenance-windows", { includeEnded }] as const,
  watchdogClusters: (sourceId: string) => ["watchdog-clusters", sourceId] as const,
  grafanaDashboards: (sourceId: string) => ["grafana-dashboards", sourceId] as const,
} as const;
