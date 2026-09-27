import type {
  MetricEvidence,
  MetricTemplate,
  HandlingAudit,
  HandlingState,
  Health,
  IncidentDetail,
  IncidentOccurrencePage,
  IncidentSummary,
  AggregationLabelCatalog,
  AggregationRule,
  AggregationRuleDraft,
  AggregationRulePreview,
  EventSource,
  EventSourceAudit,
  EventSourceCandidate,
  EventSourceCreate,
  EventSourceGrafanaDraft,
  EventSourceTestResult,
  ConnectionTestResult,
  GrafanaDashboardSummary,
  GrafanaImportItem,
  GrafanaImportPreview,
  GrafanaImportResult,
  IncidentNotification,
  NotificationChannel,
  NotificationChannelDraft,
  NotificationDelivery,
  NotificationDeliveryDetail,
  NotificationPolicy,
  NotificationPolicyDraft,
  NotificationPolicyPreview,
  PreparedPolicyActivation,
  ModelChannel,
  ModelChannelDraft,
  ModelChannelTestResult,
  Investigation,
  ModelListResult,
  ModelVendor,
  WatchdogCluster,
} from "../types";

/**
 * A failed request, carrying what `requestError.ts` needs to describe it.
 *
 * The message is unchanged from the strings this module used to throw, so the
 * pages that still render `cause.message` read the same as before; `status` is
 * there so nobody has to parse "Request failed (409)" back apart.
 */
export class ApiError extends Error {
  readonly status: number | null;
  readonly body: string | null;

  constructor(message: string, status: number | null, body: string | null = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

async function getJson<T>(url: string): Promise<T> {
  const resp = await fetch(url, { headers: { Accept: "application/json" } });
  if (!resp.ok) {
    throw new ApiError(`Request failed (${resp.status})`, resp.status);
  }
  return (await resp.json()) as T;
}

async function patchJson<T>(url: string, body: unknown): Promise<T> {
  const resp = await fetch(url, {
    method: "PATCH",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    throw new ApiError(`Request failed (${resp.status})`, resp.status);
  }
  return (await resp.json()) as T;
}

async function sendJson<T>(
  url: string,
  method: "POST" | "PUT" | "PATCH",
  body: unknown,
): Promise<T> {
  const resp = await fetch(url, {
    method,
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    const message = await resp.text();
    throw new ApiError(message || `Request failed (${resp.status})`, resp.status, message || null);
  }
  return (await resp.json()) as T;
}

async function postAction<T>(url: string, body: unknown = {}): Promise<T> {
  return sendJson<T>(url, "POST", body);
}

export const api = {
  health: () => getJson<Health>("/api/health"),
  incidents: (filters: { sourceIds?: string[] } = {}) => {
    const query = new URLSearchParams();
    filters.sourceIds?.forEach((sourceId) => query.append("source_ids", sourceId));
    const suffix = query.size ? `?${query.toString()}` : "";
    return getJson<IncidentSummary[]>(`/api/incidents${suffix}`);
  },
  incident: (id: number) => getJson<IncidentDetail>(`/api/incidents/${id}`),
  incidentOccurrences: (
    filters: {
      sourceIds?: string[];
      conclusion?: HandlingState | null;
      beforeId?: number | null;
      limit?: number;
    } = {},
  ) => {
    const query = new URLSearchParams();
    filters.sourceIds?.forEach((sourceId) => query.append("source_ids", sourceId));
    if (filters.conclusion) query.set("conclusion", filters.conclusion);
    if (filters.beforeId) query.set("before_id", String(filters.beforeId));
    if (filters.limit) query.set("limit", String(filters.limit));
    const suffix = query.size ? `?${query.toString()}` : "";
    return getJson<IncidentOccurrencePage>(`/api/incident-occurrences${suffix}`);
  },
  updateHandling: (id: number, state: HandlingState, reason: string) =>
    patchJson<HandlingAudit>(`/api/incidents/${id}/handling`, { state, reason }),
  aggregationRules: () => getJson<AggregationRule[]>("/api/aggregation-rules"),
  aggregationLabels: (lookbackHours: 24 | 168 | 720) =>
    getJson<AggregationLabelCatalog>(
      `/api/aggregation-labels?lookback_hours=${lookbackHours}`,
    ),
  previewAggregationRule: (draft: AggregationRuleDraft, ruleId: number | null) =>
    sendJson<AggregationRulePreview>(
      "/api/aggregation-rules/preview",
      "POST",
      { ...draft, rule_id: ruleId },
    ),
  createAggregationRule: (draft: AggregationRuleDraft) =>
    sendJson<AggregationRule>(
      "/api/aggregation-rules",
      "POST",
      draft,
    ),
  updateAggregationRule: (ruleId: number, draft: AggregationRuleDraft) =>
    sendJson<AggregationRule>(
      `/api/aggregation-rules/${ruleId}`,
      "PUT",
      draft,
    ),
  eventSources: (includeArchived = false) =>
    getJson<EventSource[]>(
      `/api/event-sources${includeArchived ? "?include_archived=true" : ""}`,
    ),
  createEventSource: (draft: EventSourceCreate) =>
    postAction<EventSource>("/api/event-sources", draft),
  updateEventSource: (
    sourceId: string,
    draft: EventSourceCandidate,
    expectedVersion: number,
  ) =>
    patchJson<EventSource>(
      `/api/event-sources/${encodeURIComponent(sourceId)}`,
      { ...draft, expected_version: expectedVersion },
    ),
  testEventSource: (
    sourceId: string,
    candidate?: EventSourceCandidate,
    positions?: number[],
  ) =>
    postAction<EventSourceTestResult>(
      `/api/event-sources/${encodeURIComponent(sourceId)}/test`,
      { candidate, positions },
    ),
  enableEventSource: (sourceId: string, expectedVersion: number) =>
    postAction<EventSource>(
      `/api/event-sources/${encodeURIComponent(sourceId)}/enable`,
      { expected_version: expectedVersion },
    ),
  disableEventSource: (sourceId: string, expectedVersion: number) =>
    postAction<EventSource>(
      `/api/event-sources/${encodeURIComponent(sourceId)}/disable`,
      { expected_version: expectedVersion },
    ),
  archiveEventSource: (sourceId: string, expectedVersion: number) =>
    postAction<EventSource>(
      `/api/event-sources/${encodeURIComponent(sourceId)}/archive`,
      { expected_version: expectedVersion },
    ),
  eventSourceAudit: (sourceId: string) =>
    getJson<EventSourceAudit[]>(
      `/api/event-sources/${encodeURIComponent(sourceId)}/audit`,
    ),
  watchdogClusters: (sourceId: string) =>
    getJson<WatchdogCluster[]>(
      `/api/event-sources/${encodeURIComponent(sourceId)}/watchdog-clusters`,
    ),
  addWatchdogCluster: (sourceId: string, identityValue: string) =>
    postAction<WatchdogCluster>(
      `/api/event-sources/${encodeURIComponent(sourceId)}/watchdog-clusters`,
      { identity_value: identityValue },
    ),
  updateWatchdogCluster: (
    sourceId: string,
    clusterId: number,
    action: "EXPECT" | "IGNORE" | "RESTORE",
    reason?: string,
  ) =>
    patchJson<WatchdogCluster>(
      `/api/event-sources/${encodeURIComponent(sourceId)}/watchdog-clusters/${clusterId}`,
      { action, reason: reason || null },
    ),
  saveGrafanaConfig: (sourceId: string, draft: EventSourceGrafanaDraft) =>
    sendJson<EventSource>(
      `/api/event-sources/${encodeURIComponent(sourceId)}/grafana`,
      "PUT",
      draft,
    ),
  testGrafanaConfig: (sourceId: string) =>
    postAction<EventSourceTestResult | { ok: boolean; code: string }>(
      `/api/event-sources/${encodeURIComponent(sourceId)}/grafana/test`,
    ),
  grafanaDashboards: (sourceId: string, query: string) =>
    getJson<GrafanaDashboardSummary[]>(
      `/api/event-sources/${encodeURIComponent(sourceId)}/grafana/dashboards` +
        `?query=${encodeURIComponent(query)}`,
    ),
  previewGrafanaImport: (sourceId: string, dashboardUid: string) =>
    postAction<GrafanaImportPreview>(
      `/api/event-sources/${encodeURIComponent(sourceId)}/grafana/import/preview`,
      { dashboard_uid: dashboardUid },
    ),
  confirmGrafanaImport: (sourceId: string, items: GrafanaImportItem[]) =>
    postAction<GrafanaImportResult>(
      `/api/event-sources/${encodeURIComponent(sourceId)}/grafana/import/confirm`,
      { items },
    ),
  workbenchUrl: () => getJson<{ url: string }>("/api/settings/workbench-url"),
  saveWorkbenchUrl: (url: string) =>
    sendJson<{ url: string }>("/api/settings/workbench-url", "PUT", { url }),
  notificationChannels: () =>
    getJson<NotificationChannel[]>("/api/notification-channels"),
  createNotificationChannel: (draft: NotificationChannelDraft) =>
    postAction<NotificationChannel>("/api/notification-channels", {
      name: draft.name,
      config: draft.config,
    }),
  // The name is not sent: a channel's name and its kind are both fixed at
  // creation, because either one changing means a different destination.
  updateNotificationChannel: (
    channelId: number,
    draft: NotificationChannelDraft,
    expectedVersion: number,
  ) =>
    sendJson<NotificationChannel>(
      `/api/notification-channels/${channelId}/draft`,
      "PUT",
      { config: draft.config, expected_version: expectedVersion },
    ),
  testNotificationChannel: (revisionId: number) =>
    postAction<ConnectionTestResult>(
      `/api/notification-channel-revisions/${revisionId}/test`,
    ),
  activateNotificationChannel: (revisionId: number, expectedVersion: number) =>
    postAction<NotificationChannel>(
      `/api/notification-channel-revisions/${revisionId}/activate`,
      { expected_version: expectedVersion },
    ),
  disableNotificationChannel: (channelId: number) =>
    postAction<NotificationChannel>(`/api/notification-channels/${channelId}/disable`),
  enableNotificationChannel: (channelId: number) =>
    postAction<NotificationChannel>(`/api/notification-channels/${channelId}/enable`),
  modelChannels: () => getJson<ModelChannel[]>("/api/model-channels"),
  createModelChannel: (draft: ModelChannelDraft) =>
    postAction<ModelChannel>("/api/model-channels", draft),
  updateModelChannel: (
    channelId: number,
    draft: ModelChannelDraft,
    expectedRevisionId: number,
  ) =>
    sendJson<ModelChannel>(
      `/api/model-channels/${channelId}/draft`,
      "PUT",
      {
        expected_revision_id: expectedRevisionId,
        config: draft.config,
      },
    ),
  testModelChannel: (revisionId: number) =>
    postAction<ModelChannelTestResult>(
      `/api/model-channel-revisions/${revisionId}/test`,
    ),
  modelRuntime: () => getJson<{ fake_mode: boolean }>("/api/model-runtime"),
  modelVendors: () => getJson<ModelVendor[]>("/api/model-vendors"),
  listModelChannelModels: (revisionId: number) =>
    postAction<ModelListResult>(
      `/api/model-channel-revisions/${revisionId}/models`,
    ),
  activateModelChannel: (revisionId: number) =>
    postAction<ModelChannel>(
      `/api/model-channel-revisions/${revisionId}/activate`,
    ),
  disableModelChannel: (channelId: number) =>
    postAction<ModelChannel>(`/api/model-channels/${channelId}/disable`),
  enableModelChannel: (channelId: number) =>
    postAction<ModelChannel>(`/api/model-channels/${channelId}/enable`),
  notificationPolicies: () =>
    getJson<NotificationPolicy[]>("/api/notification-policies"),
  createNotificationPolicy: (draft: NotificationPolicyDraft) =>
    postAction<NotificationPolicy>("/api/notification-policies", draft),
  updateNotificationPolicy: (
    logicalId: string,
    draft: NotificationPolicyDraft,
    expectedVersion: number,
  ) =>
    sendJson<NotificationPolicy>(
      `/api/notification-policies/${logicalId}/draft`,
      "PUT",
      { ...draft, expected_version: expectedVersion },
    ),
  previewNotificationPolicy: (
    draft: NotificationPolicyDraft,
    logicalId: string | null,
  ) =>
    postAction<NotificationPolicyPreview>("/api/notification-policies/preview", {
      ...draft,
      logical_id: logicalId,
    }),
  prepareNotificationPolicyActivation: (
    revisionId: number,
    expectedVersion: number,
    notifyExisting: boolean,
  ) =>
    postAction<PreparedPolicyActivation>(
      `/api/notification-policies/${revisionId}/prepare-activation`,
      { expected_version: expectedVersion, notify_existing: notifyExisting },
    ),
  activateNotificationPolicy: (
    revisionId: number,
    expectedVersion: number,
    notifyExisting: boolean,
    confirmToken: string,
  ) =>
    postAction<NotificationPolicy>(`/api/notification-policies/${revisionId}/activate`, {
      expected_version: expectedVersion,
      notify_existing: notifyExisting,
      confirm_token: confirmToken,
    }),
  disableNotificationPolicy: (revisionId: number) =>
    postAction<NotificationPolicy>(`/api/notification-policies/${revisionId}/disable`),
  notificationDeliveries: (filters: {
    state?: string;
    eventType?: string;
    channelId?: number;
  } = {}) => {
    const query = new URLSearchParams();
    if (filters.state) query.set("state", filters.state);
    if (filters.eventType) query.set("event_type", filters.eventType);
    if (filters.channelId) query.set("channel_id", String(filters.channelId));
    const suffix = query.size ? `?${query.toString()}` : "";
    return getJson<NotificationDelivery[]>(`/api/notification-deliveries${suffix}`);
  },
  notificationDelivery: (deliveryId: number) =>
    getJson<NotificationDeliveryDetail>(`/api/notification-deliveries/${deliveryId}`),
  retryNotificationDelivery: (deliveryId: number) =>
    postAction<{ delivery: NotificationDelivery; warning: string }>(
      `/api/notification-deliveries/${deliveryId}/retry`,
    ),
  incidentNotification: (incidentId: number) =>
    getJson<IncidentNotification>(`/api/incidents/${incidentId}/notification`),

  // F27. Keyed on the **alert**, not the group: members of one group share their
  // grouping labels and nothing else, so there is no member that can stand in
  // for the others.
  metricEvidence: (alertId: number, windowMode: string) =>
    getJson<MetricEvidence>(
      `/api/alerts/${alertId}/metric-evidence?window_mode=${windowMode}`,
  ),
  metricTemplates: () => getJson<MetricTemplate[]>("/api/metric-templates"),
  investigateAlert: (alertId: number) =>
    postAction<Investigation>(`/api/alerts/${alertId}/investigate`),
  createMetricTemplate: (body: {
    name: string;
    promql: string;
    required_labels: string[];
    description: string;
    priority?: number;
    display_unit?: string;
    source_scope?: MetricTemplate["source_scope"];
  }) => postAction<MetricTemplate>("/api/metric-templates", body),
  updateMetricTemplate: (
    templateId: number,
    body: Partial<Pick<MetricTemplate, "enabled" | "priority" | "promql" | "name" | "display_unit" | "source_scope">>,
  ) => patchJson<MetricTemplate>(`/api/metric-templates/${templateId}`, body),
};
