import type { components, paths } from "./platform-schema";
import { serializeChannelConfig } from "../channelDraft";
import type {
  AggregationLabelCatalog,
  AggregationRule,
  AggregationRuleDraft,
  AggregationRulePreview,
  ConnectionTestResult,
  EventSource,
  EventSourceAudit,
  EventSourceCandidate,
  EventSourceCreate,
  EventSourceGrafanaDraft,
  EventSourceTestResult,
  SourceNoiseControls,
  GrafanaDashboardSummary,
  GrafanaImportItem,
  GrafanaImportPreview,
  GrafanaImportResult,
  HandlingAudit,
  HandlingState,
  Health,
  IncidentDetail,
  IncidentNotification,
  IncidentOccurrencePage,
  IncidentSummary,
  MetricCurve,
  MetricEvidence,
  MetricTemplate,
  ModelChannel,
  ModelChannelDraft,
  ModelChannelRevision,
  ModelChannelTestResult,
  ModelListResult,
  ModelVendor,
  PromptGuidance,
  PromptProfile,
  PromptProfilePreview,
  PromptProfileRevision,
  NotificationChannel,
  NotificationChannelDraft,
  NotificationDelivery,
  NotificationDeliveryDetail,
  NotificationPolicy,
  NotificationPolicyDraft,
  NotificationPolicyPreview,
  OperationalOccurrence,
  OperationalOccurrencePage,
  Investigation,
  InvestigatorRun,
  IncidentTimelineEntry,
  IncidentTask,
  IncidentTaskState,
  CollaborationCommandResult,
  ResponseCommandResult,
  BatchStartHandlingResult,
  ResolutionCode,
  OccurrenceNoise,
  Suppression,
  MaintenanceWindow,
  MaintenanceWindowDraft,
  Service,
  ServiceAssignmentResult,
  ServiceDraft,
  ServiceMappingPreview,
  ServiceMappingRule,
  ServiceMappingRuleDraft,
  OperationalSignalState,
  SimilarHistoryItem,
  PreparedPolicyActivation,
  QueueView,
  SecretUpdate,
  WatchdogCluster,
} from "../types";

type Schema<Name extends keyof components["schemas"]> = components["schemas"][Name];
type PlatformPath = keyof paths;

type SourceResponse = Schema<"SourceResponse">;
type MonitoringResponse = Schema<"MonitoringConnectionResponse">;
type RuleResponse = Schema<"RuleResponse">;
type IncidentResponse = Schema<"IncidentResponse">;
type IncidentDetailResponse = Schema<"IncidentDetailResponse">;
type AlertResponse = Schema<"AlertResponse">;
type OccurrencePageResponse = Schema<"IncidentOccurrencePageResponse">;
type OperationalOccurrenceResponse = Schema<"OperationalOccurrenceResponse">;
type OperationalOccurrencePageResponse = Schema<"OperationalOccurrencePageResponse">;
type SimilarHistoryResponse = Schema<"SimilarHistoryResponse">;
type InvestigationResponse = Schema<"InvestigationResponse">;
type InvestigatorRunResponse = Schema<"InvestigatorRunResponse">;
type TimelineEntryResponse = Schema<"TimelineEntryResponse">;
type ResponseCommandResponse = Schema<"ResponseCommandResponse">;
type BatchStartHandlingResponse = Schema<"BatchStartHandlingResponse">;
type IncidentTaskResponse = Schema<"IncidentTaskResponse">;
type CollaborationCommandResponse = Schema<"CollaborationCommandResponse">;
type MetricTemplateResponse = Schema<"MetricTemplateResponse">;
type MetricEvidenceResponse = Schema<"MetricEvidenceResponse">;
type ModelChannelResponse = Schema<"ModelChannelResponse">;
type PromptProfileResponse = Schema<"PromptProfileResponse">;
type PromptProfileRevisionResponse = Schema<"PromptProfileRevisionResponse">;
type NotificationChannelResponse = Schema<"NotificationChannelResponse">;
type NotificationPolicyResponse = Schema<"NotificationPolicyResponse">;
type NotificationDeliveryResponse = Schema<"NotificationDeliveryResponse">;
type ServiceResponse = Schema<"ServiceResponse">;
type ServiceMappingRuleResponse = Schema<"ServiceMappingRuleResponse">;
type ServiceMappingPreviewResponse = Schema<"ServiceMappingPreviewResponse">;
type ServiceMappingPublishResponse = Schema<"ServiceMappingPublishResponse">;
type ServiceAssignmentResponse = Schema<"ServiceAssignmentResponse">;
type SourceNoiseControlsResponse = Schema<"SourceNoiseControlsResponse">;
type OccurrenceNoiseResponse = Schema<"OccurrenceNoiseResponse">;
type SuppressionResponse = Schema<"SuppressionResponse">;
type MaintenanceResponse = Schema<"MaintenanceResponse">;
type PlatformHealthResponse = Schema<"PlatformHealthResponse">;
export type AnalyticsOverview = Schema<"AnalyticsOverviewResponse">;

/** A transport failure with the stable platform safe code and request id. */
export class ApiError extends Error {
  readonly status: number | null;
  readonly body: string | null;
  readonly code: string | null;
  readonly requestId: string | null;

  constructor(
    message: string,
    status: number | null,
    body: string | null = null,
    code: string | null = null,
    requestId: string | null = null,
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
    this.code = code;
    this.requestId = requestId;
  }
}

function platformPath<Path extends PlatformPath>(path: Path): Path {
  return path;
}

async function requestJson<T>(
  url: string,
  init: RequestInit = {},
): Promise<T> {
  const method = (init.method ?? "GET").toUpperCase();
  const csrfToken =
    typeof document === "undefined"
      ? null
      : document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]')?.content;
  const response = await fetch(url, {
    ...init,
    headers: {
      Accept: "application/json",
      ...(init.body === undefined ? {} : { "Content-Type": "application/json" }),
      ...(method === "GET" || method === "HEAD" || !csrfToken
        ? {}
        : { "X-CSRF-Token": csrfToken }),
      ...init.headers,
    },
  });
  if (!response.ok) {
    const body = await response.text();
    let code: string | null = null;
    let message = `请求未完成（HTTP ${response.status}）`;
    let requestId: string | null = response.headers.get("x-request-id");
    try {
      const parsed = JSON.parse(body) as {
        error?: { code?: string; message?: string; request_id?: string };
      };
      code = parsed.error?.code ?? null;
      message = parsed.error?.message ?? message;
      requestId = parsed.error?.request_id ?? requestId;
    } catch {
      // A proxy can replace the JSON body. Do not surface an arbitrary HTML page.
    }
    throw new ApiError(message, response.status, body || null, code, requestId);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

function json(method: "POST" | "PUT" | "PATCH" | "DELETE", body: unknown): RequestInit {
  return { method, body: JSON.stringify(body) };
}

const pendingIntents = new Map<string, string>();

function stableDigest(value: string): string {
  let first = 0x811c9dc5;
  let second = 0x9e3779b9;
  for (let index = 0; index < value.length; index += 1) {
    const code = value.charCodeAt(index);
    first = Math.imul(first ^ code, 0x01000193) >>> 0;
    second = Math.imul(second ^ code, 0x85ebca6b) >>> 0;
  }
  return `${first.toString(16).padStart(8, "0")}${second.toString(16).padStart(8, "0")}`;
}

function intentKey(scope: string, body: unknown): string {
  return `${scope}:${stableDigest(JSON.stringify(body))}`;
}

function createCommand(
  scope: string,
  body: unknown,
  method: "POST" | "PUT" = "POST",
): RequestInit {
  const intent = intentKey(scope, body);
  let key = pendingIntents.get(intent);
  if (!key) {
    const nonce = globalThis.crypto?.randomUUID?.()
      ?? `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;
    key = `operator-${nonce}`;
    pendingIntents.set(intent, key);
  }
  return {
    ...json(method, body),
    headers: { "Idempotency-Key": key },
  };
}

async function commandJson<T>(
  scope: string,
  url: string,
  body: unknown,
  method: "POST" | "PUT" = "POST",
): Promise<T> {
  const intent = intentKey(scope, body);
  try {
    const result = await requestJson<T>(url, createCommand(scope, body, method));
    pendingIntents.delete(intent);
    return result;
  } catch (error) {
    if (
      error instanceof ApiError
      && error.status !== null
      && error.status >= 400
      && error.status < 500
      && ![408, 429].includes(error.status)
      && error.code !== "COMMAND_OUTCOME_UNKNOWN"
    ) pendingIntents.delete(intent);
    throw error;
  }
}

function sourceState(value: string): IncidentSummary["source_state"] {
  if (value === "FIRING") return "firing";
  if (value === "PENDING_RESOLUTION") return "pending_resolution";
  if (value === "RECOVERED") return "recovered";
  return "unknown";
}

function scope(sourceIds: string[]) {
  return { mode: sourceIds.length === 0 ? "ALL" as const : "SELECTED" as const, source_ids: sourceIds };
}

function mapRule(value: RuleResponse): AggregationRule {
  return {
    id: value.id,
    name: value.name,
    priority: value.priority,
    enabled: value.enabled,
    version: value.version,
    matchers: value.matchers.map((item) => ({
      ...item,
      operator: item.operator as AggregationRule["matchers"][number]["operator"],
    })),
    group_by_labels: value.group_by_labels,
    source_scope: scope(value.source_ids),
    grouping_window_seconds: value.grouping_window_seconds,
    created_at: value.created_at,
    updated_at: value.updated_at,
  };
}

function mapIncident(value: IncidentResponse): IncidentSummary {
  return {
    id: value.id,
    source_id: value.source_id,
    source_name: value.source_name,
    title: value.title,
    severity: value.severity as IncidentSummary["severity"],
    source_state: sourceState(value.source_state),
    freshness_state: value.freshness_state as IncidentSummary["freshness_state"],
    handling_state: value.handling_state as HandlingState,
    handling_version: value.handling_version,
    member_count: value.member_count,
    updated_at: value.updated_at,
    aggregation_rule_id: value.aggregation_rule_id,
    aggregation_rule_name: value.aggregation_rule_name,
    aggregation_status: value.aggregation_status,
  };
}

function mapAlert(value: AlertResponse) {
  return {
    id: value.id,
    fingerprint: value.upstream_fingerprint,
    source_id: value.source_id,
    alertname: value.alertname,
    severity: value.severity as IncidentSummary["severity"],
    cluster: value.cluster,
    labels: value.labels,
    annotations: value.annotations,
    starts_at: value.starts_at,
    ends_at: null,
    source_state: sourceState(value.source_state),
    missing_since_at: value.missing_since_at,
    origin: value.origin.toLowerCase(),
    evidence_completeness: value.evidence_completeness,
    first_seen_at: value.starts_at ?? value.last_seen_at,
    last_seen_at: value.last_seen_at,
  };
}

function mapIncidentDetail(value: IncidentDetailResponse): IncidentDetail {
  return {
    ...mapIncident({
      ...value,
      aggregation_rule_id: value.aggregation_rule_id,
      aggregation_rule_name: value.aggregation_rule_name,
      aggregation_status: value.aggregation_status,
    }),
    group_key: value.group_key,
    grouping_explanation: value.grouping_explanation,
    occurrence_no: value.occurrence_no,
    policy_version: value.aggregation_rule_version ?? 0,
    created_at: value.occurrence_started_at,
    members: value.members.map(mapAlert),
    handling_history: value.handling_history as HandlingAudit[],
  };
}

function mapGrafanaMonitoring(value: MonitoringResponse | null) {
  if (!value) return null;
  return {
    url: value.base_url,
    secret_configured: value.secret_configured,
    timeout_seconds: 15,
    last_test_status: value.last_test_code,
    tested_at: value.tested_at,
    safe_error_code: value.last_test_code,
  };
}

function mapThanosMonitoring(value: MonitoringResponse | null) {
  if (!value) return null;
  return {
    url: value.base_url,
    auth_type: value.secret_configured ? "BEARER" as const : "NONE" as const,
    username: "",
    secret_configured: value.secret_configured,
    timeout_seconds: 15,
    last_test_status: value.last_test_code,
    tested_at: value.tested_at,
    safe_error_code: value.last_test_code,
  };
}

async function mapSource(value: SourceResponse): Promise<EventSource> {
  const sourceId = encodeURIComponent(value.id);
  const monitoring = await requestJson<MonitoringResponse[]>(
    `/api/v1/sources/${sourceId}/monitoring`,
  );
  const thanos = monitoring.find((item) => item.kind === "THANOS") ?? null;
  const grafana = monitoring.find((item) => item.kind === "GRAFANA") ?? null;
  return {
    id: value.id,
    type: "ALERTMANAGER",
    name: value.name,
    lifecycle_state: value.state as EventSource["lifecycle_state"],
    status: value.state === "ENABLED" && value.last_poll_completeness === "FAILED"
      ? "CONNECTION_ERROR"
      : value.state,
    version: value.version,
    config: {
      poll_interval_seconds: value.poll_interval_seconds,
      resolution_grace_seconds: value.resolution_grace_seconds,
      max_parallel_endpoints: value.max_parallel_endpoints,
      watchdog_enabled: value.watchdog_enabled,
      watchdog_alertname: value.watchdog_alertname,
      watchdog_identity_label: value.watchdog_identity_label,
      watchdog_missing_after_seconds: value.watchdog_missing_after_seconds,
      last_test_status: value.last_poll_completeness,
      tested_at: value.last_poll_at,
      safe_error_code: value.last_poll_safe_error_codes[0] ?? null,
      endpoints: value.endpoints.map((endpoint) => ({
        position: endpoint.position,
        url: endpoint.canonical_url,
        enabled: endpoint.enabled,
        auth_type: endpoint.auth_kind as "NONE" | "BEARER" | "BASIC",
        username: endpoint.username,
        secret_configured: endpoint.secret_configured,
        last_test: null,
      })),
      thanos: mapThanosMonitoring(thanos),
      grafana: mapGrafanaMonitoring(grafana),
    },
    created_at: value.created_at,
    updated_at: value.updated_at,
    archived_at: value.state === "ARCHIVED" ? value.updated_at : null,
  };
}

function secretMap(value: SecretUpdate): Schema<"SecretUpdateInput"> {
  return { action: value.action, value: value.value ?? null };
}

function sourceBody(draft: EventSourceCandidate) {
  return {
    name: draft.name,
    endpoints: draft.endpoints.map((endpoint) => ({
      position: endpoint.position ?? 0,
      url: endpoint.url,
      enabled: endpoint.enabled,
      auth_kind: endpoint.auth_type,
      username: endpoint.username,
      secret: secretMap(endpoint.secret),
      timeout_seconds: 10,
    })),
    poll_interval_seconds: draft.poll_interval_seconds,
    resolution_grace_seconds: draft.resolution_grace_seconds,
    max_parallel_endpoints: draft.max_parallel_endpoints,
    watchdog_enabled: draft.watchdog_enabled,
    watchdog_alertname: draft.watchdog_alertname,
    watchdog_identity_label: draft.watchdog_identity_label,
    watchdog_missing_after_seconds: draft.watchdog_missing_after_seconds ?? 90,
  };
}

async function saveThanos(sourceId: string, draft: EventSourceCandidate): Promise<void> {
  if (!draft.thanos?.url) return;
  const monitoring = await requestJson<MonitoringResponse[]>(
    `/api/v1/sources/${encodeURIComponent(sourceId)}/monitoring`,
  );
  const current = monitoring.find((item) => item.kind === "THANOS") ?? null;
  await requestJson(
    `/api/v1/sources/${encodeURIComponent(sourceId)}/monitoring/THANOS`,
    json("PUT", {
      base_url: draft.thanos.url,
      secret: secretMap(draft.thanos.secret),
      expected_version: current?.version ?? null,
    }),
  );
}

function ruleBody(draft: AggregationRuleDraft) {
  return {
    name: draft.name,
    priority: draft.priority,
    enabled: draft.enabled,
    matchers: draft.matchers,
    group_by_labels: draft.group_by_labels,
    grouping_window_seconds: draft.grouping_window_seconds ?? 30,
    source_ids: draft.source_scope?.mode === "SELECTED" ? draft.source_scope.source_ids : [],
  };
}

function template(value: MetricTemplateResponse): MetricTemplate {
  return {
    id: value.id,
    name: value.name,
    promql: value.promql,
    required_labels: value.required_labels,
    description: value.description,
    builtin_key: value.builtin_key,
    user_modified: value.user_modified,
    enabled: value.enabled,
    priority: value.priority,
    display_unit: value.unit,
    source_scope: scope(value.source_ids),
    origin: value.grafana_origin
      ? {
          source_id: value.grafana_origin.source_id,
          dashboard_uid: value.grafana_origin.dashboard_uid,
          dashboard_title: value.grafana_origin.dashboard_title,
          panel_id: value.grafana_origin.panel_id,
          panel_title: value.grafana_origin.panel_title,
        }
      : null,
  };
}

function metricCurve(
  value: MetricEvidenceResponse["curves"][number],
  evidence: MetricEvidenceResponse,
  index: number,
): MetricCurve {
  const series = value.result.series.map((item) => {
    const raw = item as { metric?: Record<string, string>; values?: [number, string | number][] };
    return {
      labels: raw.metric ?? {},
      points: (raw.values ?? []).map(([timestamp, sample]) => [Number(timestamp), Number(sample)]),
    };
  });
  return {
    curve_id: `${value.kind.toLowerCase()}-${index}`,
    kind: value.kind === "MAIN" ? "PRIMARY" : "AUXILIARY",
    title: value.name,
    query: value.promql,
    expr_origin: value.kind === "MAIN" ? "RULES_API" : "TEMPLATE",
    tier: null,
    metric_type: "UNKNOWN",
    threshold: null,
    threshold_operator: null,
    display_unit: value.unit,
    window_mode: "trigger",
    queried_at: evidence.window_end,
    window_start: evidence.window_start,
    window_end: evidence.window_end,
    step_seconds: evidence.step_seconds,
    series,
  };
}

function channel(value: NotificationChannelResponse): NotificationChannel {
  const target = value.provider === "SMTP"
    ? String(value.config.host ?? "")
    : value.provider === "GENERIC_WEBHOOK"
      ? String(value.config.url ?? "")
      : "飞书群机器人";
  return {
    id: value.id,
    name: value.name,
    state: value.enabled ? "ENABLED" : "DISABLED",
    active_revision_id: value.revision_state === "ACTIVE" ? value.id : null,
    created_at: value.tested_at ?? "",
    updated_at: value.tested_at ?? "",
    disabled_at: value.enabled ? null : value.tested_at,
    revisions: [{
      id: value.id,
      version: value.revision_no,
      state: value.revision_state as "DRAFT" | "ACTIVE" | "RETIRED",
      provider: value.provider,
      config: value.config,
      secret_configured: value.secret_configured,
      config_summary: {
        kind: value.provider,
        target,
        detail: value.provider,
        secret_configured: Object.values(value.secret_configured).some(Boolean),
      },
      webhook_configured: value.secret_configured.webhook ?? false,
      signing_secret_configured: value.secret_configured.signing_secret ?? false,
      required_keyword: String(value.config.required_keyword ?? "") || null,
      mention_mode: (value.config.mention_mode ?? "NONE") as "NONE" | "USERS" | "ALL",
      mention_users: (value.config.mention_users ?? []) as { open_id: string }[],
      mention_on: (value.config.mention_on ?? {}) as Record<string, boolean>,
      last_tested_at: value.tested_at,
      last_test_status: value.last_test_code,
      last_test_error_code: value.last_test_code === "OK" ? null : value.last_test_code,
      created_at: value.tested_at ?? "",
      updated_at: value.tested_at ?? "",
      activated_at: value.revision_state === "ACTIVE" ? value.tested_at : null,
    }],
  };
}

function modelChannel(value: ModelChannelResponse): ModelChannel {
  return {
    id: value.id,
    name: value.name,
    kind: "OPENAI_COMPATIBLE",
    enabled: value.enabled,
    active_revision_id: value.state === "ACTIVE" ? value.id : null,
    created_at: value.tested_at ?? "",
    updated_at: value.tested_at ?? "",
    revisions: [{
      id: value.id,
      version: value.revision_no,
      state: value.state as "DRAFT" | "ACTIVE" | "RETIRED",
      base_url: value.base_url,
      base_host: (() => { try { return new URL(value.base_url).host; } catch { return ""; } })(),
      model: value.model,
      provider_profile_id: value.provider_profile_id,
      provider_id: value.provider_id as ModelChannelRevision["provider_id"],
      protocol_profile: value.protocol_profile as ModelChannelRevision["protocol_profile"],
      support_level: value.support_level as ModelChannelRevision["support_level"],
      secret_configured: value.api_key_configured,
      tested_ok_at: value.last_test_code === "OK" ? value.tested_at : null,
      last_tested_at: value.tested_at,
      last_test_code: value.last_test_code,
      created_at: value.tested_at ?? "",
    }],
  };
}

function policy(value: NotificationPolicyResponse): NotificationPolicy {
  return {
    id: value.revision_id,
    logical_id: value.logical_id,
    version: value.version,
    state: value.state as NotificationPolicy["state"],
    name: value.name,
    priority: value.priority,
    matchers: value.matchers,
    repeat_interval_seconds: value.repeat_interval_seconds,
    channel_ids: value.channel_ids,
    source_scope: scope(value.source_ids),
    created_at: "",
    updated_at: "",
    activated_at: value.state === "ACTIVE" ? "" : null,
  };
}

function delivery(value: NotificationDeliveryResponse): NotificationDeliveryDetail {
  return {
    id: value.id,
    event_key: value.event_key,
    incident_id: value.incident_id,
    route_id: value.route_id,
    route_target_id: value.route_target_id,
    channel_id: value.channel_id,
    channel_name: value.channel_name,
    source_id: String(value.payload_snapshot.source_id ?? ""),
    source_name: String(value.payload_snapshot.source_name ?? "") || null,
    event_type: value.event_type as NotificationDelivery["event_type"],
    state: value.state,
    attempt_count: value.attempt_count,
    scheduled_at: value.scheduled_at,
    next_attempt_at: value.next_attempt_at,
    suppression_reason: value.suppression_reason,
    created_at: value.scheduled_at,
    updated_at: value.succeeded_at ?? value.next_attempt_at,
    succeeded_at: value.succeeded_at,
    payload_snapshot: value.payload_snapshot,
    attempts: (value.attempts ?? []).map((attempt) => ({ ...attempt, error_summary: attempt.error_code })),
  };
}

function policyBody(draft: NotificationPolicyDraft) {
  return {
    name: draft.name,
    priority: draft.priority,
    matchers: draft.matchers,
    repeat_interval_seconds: draft.repeat_interval_seconds,
    channel_ids: draft.channel_ids,
    scope_mode: draft.source_scope?.mode ?? "ALL",
    source_ids: draft.source_scope?.mode === "SELECTED" ? draft.source_scope.source_ids : [],
  };
}

export const api = {
  analyticsOverview: (filters: {
    range: "24h" | "7d" | "30d";
    sourceId: string | null;
    serviceId: number | null;
    signalSeverity: "critical" | "warning" | "info" | "unknown" | null;
  }) => {
    const query = new URLSearchParams({ range: filters.range });
    if (filters.sourceId !== null) query.set("source_id", filters.sourceId);
    if (filters.serviceId !== null) query.set("service_id", String(filters.serviceId));
    if (filters.signalSeverity !== null) {
      query.set("signal_severity", filters.signalSeverity);
    }
    return requestJson<AnalyticsOverview>(
      `${platformPath("/api/v1/analytics/overview")}?${query}`,
    );
  },
  health: () => requestJson<PlatformHealthResponse>(
    platformPath("/api/v1/platform-health"),
  ) as Promise<Health>,
  incidents: async (filters: { sourceIds?: string[] } = {}) => {
    const rows = await requestJson<IncidentResponse[]>(platformPath("/api/v1/incidents"));
    const selected = new Set(filters.sourceIds ?? []);
    return rows.filter((item) => selected.size === 0 || selected.has(item.source_id)).map(mapIncident);
  },
  incident: async (id: number) => mapIncidentDetail(await requestJson<IncidentDetailResponse>(`/api/v1/incidents/${id}`)),
  incidentOccurrences: async (filters: { sourceIds?: string[]; conclusion?: HandlingState | null; cursor?: string | null; limit?: number } = {}) => {
    const query = new URLSearchParams();
    if (filters.sourceIds?.length) query.set("source_ids", filters.sourceIds.join(","));
    if (filters.conclusion) query.set("conclusion", filters.conclusion);
    if (filters.cursor) query.set("cursor", filters.cursor);
    if (filters.limit) query.set("limit", String(filters.limit));
    const page = await requestJson<OccurrencePageResponse>(`/api/v1/incident-occurrences?${query}`);
    return { items: page.items, next_cursor: page.next_cursor } as IncidentOccurrencePage;
  },
  operationalOccurrences: async (filters: {
    view?: QueueView;
    sourceIds?: string[];
    signalStates?: OperationalSignalState[];
    cursor?: string | null;
    limit?: number;
  } = {}) => {
    const query = new URLSearchParams();
    if (filters.view) query.set("view", filters.view);
    if (filters.sourceIds?.length) query.set("source_ids", filters.sourceIds.join(","));
    if (filters.signalStates?.length) query.set("signal_states", filters.signalStates.join(","));
    if (filters.cursor) query.set("cursor", filters.cursor);
    if (filters.limit) query.set("limit", String(filters.limit));
    const page = await requestJson<OperationalOccurrencePageResponse>(`/api/v1/occurrences?${query}`);
    return page as OperationalOccurrencePage;
  },
  operationalOccurrence: async (id: number) =>
    requestJson<OperationalOccurrenceResponse>(`/api/v1/occurrences/${id}`) as Promise<OperationalOccurrence>,
  similarOccurrenceHistory: async (id: number) =>
    requestJson<SimilarHistoryResponse[]>(`/api/v1/occurrences/${id}/similar`) as Promise<SimilarHistoryItem[]>,
  occurrenceInvestigations: (id: number) =>
    requestJson<InvestigatorRunResponse[]>(
      `/api/v1/occurrences/${id}/investigator-runs`,
    ) as Promise<InvestigatorRun[]>,
  legacyOccurrenceInvestigations: (id: number) =>
    requestJson<InvestigationResponse[]>(
      `/api/v1/occurrences/${id}/investigations`,
    ) as Promise<Investigation[]>,
  startOccurrenceInvestigation: (id: number) =>
    commandJson<InvestigatorRunResponse>(
      `occurrence.${id}.investigator-run.start`,
      `/api/v1/occurrences/${id}/investigator-runs`,
      {},
    ) as Promise<InvestigatorRun>,
  cancelInvestigation: (id: string) =>
    commandJson<InvestigatorRunResponse>(
      `investigator-run.${id}.cancel`,
      `/api/v1/investigator-runs/${encodeURIComponent(id)}/cancel`,
      {},
    ) as Promise<InvestigatorRun>,
  recordInvestigationFeedback: (id: string, rating: "USEFUL" | "NOT_USEFUL" | "ADOPTED") =>
    requestJson<Schema<"InvestigationFeedbackResponse">>(
      `/api/v1/investigator-runs/${encodeURIComponent(id)}/feedback`,
      json("POST", { rating }),
    ),
  occurrenceNoise: (id: number) => requestJson<OccurrenceNoiseResponse>(
    `/api/v1/occurrences/${id}/noise`,
  ) as Promise<OccurrenceNoise>,
  createOccurrenceSuppression: (
    id: number,
    durationSeconds: 900 | 3600 | 14400 | 86400,
    reason: string,
  ) => commandJson<SuppressionResponse>(
    `occurrence.${id}.suppression.create`,
    `/api/v1/occurrences/${id}/suppression`,
    { duration_seconds: durationSeconds, reason },
  ) as Promise<Suppression>,
  endOccurrenceSuppression: (id: number, expectedVersion: number) =>
    requestJson<SuppressionResponse>(
      `/api/v1/occurrences/${id}/suppression/end`,
      json("POST", { expected_version: expectedVersion }),
    ) as Promise<Suppression>,
  assignOccurrenceService: async (
    id: number,
    serviceId: number,
    expectedVersion: number,
  ) => commandJson<ServiceAssignmentResponse>(
    `occurrence.${id}.service.assign`,
    `/api/v1/occurrences/${id}/service`,
    { service_id: serviceId, expected_version: expectedVersion },
    "PUT",
  ) as Promise<ServiceAssignmentResult>,
  occurrenceTimeline: async (id: number) =>
    requestJson<TimelineEntryResponse[]>(`/api/v1/occurrences/${id}/timeline`) as Promise<IncidentTimelineEntry[]>,
  occurrenceTasks: async (id: number) =>
    requestJson<IncidentTaskResponse[]>(`/api/v1/occurrences/${id}/tasks`) as Promise<IncidentTask[]>,
  createOccurrenceTask: async (id: number, draft: {
    title: string;
    description: string | null;
    due_at: string | null;
    runbook_link: string | null;
  }) => commandJson<CollaborationCommandResponse>(
    `occurrence.${id}.task.create`,
    `/api/v1/occurrences/${id}/tasks`,
    draft,
  ) as Promise<CollaborationCommandResult>,
  transitionOccurrenceTask: async (
    occurrenceId: number,
    taskId: number,
    expectedVersion: number,
    target: Exclude<IncidentTaskState, "TODO">,
    result: string | null,
    reason: string | null,
  ) => commandJson<CollaborationCommandResponse>(
    `occurrence.${occurrenceId}.task.${taskId}.transition`,
    `/api/v1/occurrences/${occurrenceId}/tasks/${taskId}/transition`,
    { expected_version: expectedVersion, target, result, reason },
  ) as Promise<CollaborationCommandResult>,
  addOccurrenceNote: async (id: number, text: string) =>
    commandJson<CollaborationCommandResponse>(
      `occurrence.${id}.note.create`,
      `/api/v1/occurrences/${id}/notes`,
      { text },
    ) as Promise<CollaborationCommandResult>,
  redactOccurrenceNote: async (id: number, sequence: number, reason: string) =>
    commandJson<CollaborationCommandResponse>(
      `occurrence.${id}.note.${sequence}.redact`,
      `/api/v1/occurrences/${id}/notes/${sequence}/redact`,
      { reason },
    ) as Promise<CollaborationCommandResult>,
  startHandlingOccurrence: async (id: number, expectedVersion: number, reason: string | null) =>
    commandJson<ResponseCommandResponse>(
      `occurrence.${id}.start-handling`,
      `/api/v1/occurrences/${id}/start-handling`,
      { expected_version: expectedVersion, reason },
    ) as Promise<ResponseCommandResult>,
  resolveOccurrence: async (
    id: number,
    expectedVersion: number,
    resolutionCode: ResolutionCode,
    duplicateOfOccurrenceId: number | null,
    reason: string | null,
  ) => commandJson<ResponseCommandResponse>(
    `occurrence.${id}.resolve`,
    `/api/v1/occurrences/${id}/resolve`,
    {
      expected_version: expectedVersion,
      resolution_code: resolutionCode,
      duplicate_of_occurrence_id: duplicateOfOccurrenceId,
      reason,
    },
  ) as Promise<ResponseCommandResult>,
  batchStartHandlingOccurrences: async (
    items: { occurrence_id: number; expected_version: number }[],
    reason: string | null,
  ) => commandJson<BatchStartHandlingResponse>(
    "occurrence.batch-start-handling",
    platformPath("/api/v1/occurrences/batch-start-handling"),
    { items, reason },
  ) as Promise<BatchStartHandlingResult>,
  aggregationRules: async () => (await requestJson<RuleResponse[]>(platformPath("/api/v1/aggregation-rules"))).map(mapRule),
  services: (includeArchived = false) => requestJson<ServiceResponse[]>(
    `${platformPath("/api/v1/services")}${includeArchived ? "?include_archived=true" : ""}`,
  ) as Promise<Service[]>,
  createService: (draft: ServiceDraft) => commandJson<ServiceResponse>(
    "service.create",
    platformPath("/api/v1/services"),
    draft,
  ) as Promise<Service>,
  updateService: (id: number, draft: ServiceDraft, expectedVersion: number) =>
    requestJson<ServiceResponse>(
      `/api/v1/services/${id}`,
      json("PUT", { ...draft, expected_version: expectedVersion }),
    ) as Promise<Service>,
  archiveService: (id: number, expectedVersion: number) =>
    requestJson<ServiceResponse>(
      `/api/v1/services/${id}/archive`,
      json("POST", { expected_version: expectedVersion }),
    ) as Promise<Service>,
  serviceMappingRules: () => requestJson<ServiceMappingRuleResponse[]>(
    platformPath("/api/v1/service-mapping-rules"),
  ) as Promise<ServiceMappingRule[]>,
  createServiceMappingRule: (draft: ServiceMappingRuleDraft) =>
    commandJson<ServiceMappingRuleResponse>(
      "service-mapping-rule.create",
      platformPath("/api/v1/service-mapping-rules"),
      draft,
    ) as Promise<ServiceMappingRule>,
  updateServiceMappingRule: (
    id: number,
    draft: ServiceMappingRuleDraft,
    expectedVersion: number,
  ) => requestJson<ServiceMappingRuleResponse>(
    `/api/v1/service-mapping-rules/${id}`,
    json("PUT", { ...draft, expected_version: expectedVersion }),
  ) as Promise<ServiceMappingRule>,
  previewServiceMappingRule: (id: number) =>
    requestJson<ServiceMappingPreviewResponse>(
      `/api/v1/service-mapping-rules/${id}/preview`,
      json("POST", {}),
    ) as Promise<ServiceMappingPreview>,
  publishServiceMappingRule: (id: number, expectedVersion: number) =>
    requestJson<ServiceMappingPublishResponse>(
      `/api/v1/service-mapping-rules/${id}/publish`,
      json("POST", { expected_version: expectedVersion }),
    ),
  aggregationLabels: (lookbackHours: 24 | 168 | 720) => requestJson<AggregationLabelCatalog>(`/api/v1/aggregation-labels?lookback_hours=${lookbackHours}`),
  previewAggregationRule: (draft: AggregationRuleDraft, ruleId: number | null) => requestJson<AggregationRulePreview>(platformPath("/api/v1/aggregation-rules/preview"), json("POST", { ...ruleBody(draft), rule_id: ruleId })),
  createAggregationRule: async (draft: AggregationRuleDraft) => mapRule(await commandJson<RuleResponse>("aggregation-rule.create", platformPath("/api/v1/aggregation-rules"), ruleBody(draft))),
  updateAggregationRule: async (ruleId: number, draft: AggregationRuleDraft) => {
    const current = (await requestJson<RuleResponse[]>(platformPath("/api/v1/aggregation-rules"))).find((item) => item.id === ruleId);
    if (!current) throw new ApiError("未找到指定聚合规则", 404, null, "AGGREGATION_RULE_NOT_FOUND");
    return mapRule(await requestJson<RuleResponse>(`/api/v1/aggregation-rules/${ruleId}`, json("PUT", { ...ruleBody(draft), expected_version: current.version })));
  },
  eventSources: async (includeArchived = false) => Promise.all((await requestJson<SourceResponse[]>(`/api/v1/sources${includeArchived ? "?include_archived=true" : ""}`)).map(mapSource)),
  createEventSource: async (draft: EventSourceCreate) => {
    const created = await commandJson<SourceResponse>("source.create", platformPath("/api/v1/sources"), sourceBody(draft));
    await saveThanos(created.id, draft);
    return mapSource(created);
  },
  updateEventSource: async (sourceId: string, draft: EventSourceCandidate, expectedVersion: number) => {
    const updated = await requestJson<SourceResponse>(`/api/v1/sources/${encodeURIComponent(sourceId)}`, json("PUT", { ...sourceBody(draft), expected_version: expectedVersion }));
    await saveThanos(sourceId, draft);
    return mapSource(updated);
  },
  testEventSource: async (sourceId: string) => {
    const source = await requestJson<SourceResponse>(`/api/v1/sources/${encodeURIComponent(sourceId)}`);
    const result = await requestJson<Schema<"SourceTestResponse">>(`/api/v1/sources/${encodeURIComponent(sourceId)}/test`, json("POST", { expected_version: source.version }));
    return { ok: result.ok, code: result.completeness, endpoints: result.endpoints.map((item) => ({ position: item.position, ok: item.ok, code: item.safe_error_code ?? item.status })) } as EventSourceTestResult;
  },
  enableEventSource: (sourceId: string, expectedVersion: number) => requestJson<SourceResponse>(`/api/v1/sources/${encodeURIComponent(sourceId)}/enable`, json("POST", { expected_version: expectedVersion })).then(mapSource),
  disableEventSource: (sourceId: string, expectedVersion: number) => requestJson<SourceResponse>(`/api/v1/sources/${encodeURIComponent(sourceId)}/disable`, json("POST", { expected_version: expectedVersion })).then(mapSource),
  archiveEventSource: (sourceId: string, expectedVersion: number) => requestJson<SourceResponse>(`/api/v1/sources/${encodeURIComponent(sourceId)}/archive`, json("POST", { expected_version: expectedVersion })).then(mapSource),
  eventSourceAudit: async (sourceId: string) => (await requestJson<Schema<"SourceAuditResponse">[]>(`/api/v1/sources/${encodeURIComponent(sourceId)}/audit`)).map((item) => ({ id: item.sequence, action: item.action, actor: "local-user", result: "SUCCESS", changes: { source_version: item.source_version }, created_at: item.changed_at } as EventSourceAudit)),
  sourceNoiseControls: (sourceId: string) => requestJson<SourceNoiseControlsResponse>(
    `/api/v1/sources/${encodeURIComponent(sourceId)}/noise-controls`,
  ) as Promise<SourceNoiseControls>,
  updateSourceNoiseControls: (
    sourceId: string,
    value: Omit<SourceNoiseControls, "source_id" | "storm_active" | "storm_started_at" | "version">,
    expectedVersion: number,
  ) => requestJson<SourceNoiseControlsResponse>(
    `/api/v1/sources/${encodeURIComponent(sourceId)}/noise-controls`,
    json("PUT", { ...value, expected_version: expectedVersion }),
  ) as Promise<SourceNoiseControls>,
  watchdogClusters: async (sourceId: string) => (await requestJson<Schema<"WatchdogClusterResponse">[]>(`/api/v1/sources/${encodeURIComponent(sourceId)}/watchdog-clusters`)).map((item) => ({ id: item.id, source_id: item.source_id, identity_value: item.identity_value, cluster: item.identity_value, inventory_state: item.inventory_state as WatchdogCluster["inventory_state"], health_state: item.health_state === "NOT_MONITORED" ? null : item.health_state as WatchdogCluster["health_state"], status: (item.health_state === "HEALTHY" ? "healthy" : item.health_state === "MISSING" ? "missing" : "unknown") as WatchdogCluster["status"], first_discovered_at: item.last_observed_at ?? "", monitoring_started_at: item.last_observed_at ?? "", last_seen_at: item.last_observed_at, ignored_at: null, ignored_reason: null, still_emitting: item.health_state === "HEALTHY", freshness_seconds: null, version: 1 })),
  addWatchdogCluster: async (sourceId: string, identityValue: string) => {
    await requestJson(`/api/v1/sources/${encodeURIComponent(sourceId)}/watchdog-clusters`, json("POST", { identity_value: identityValue }));
    const rows = await api.watchdogClusters(sourceId);
    const created = rows.find((item) => item.identity_value === identityValue);
    if (!created) throw new ApiError("Watchdog 清单写入后未找到目标", 409, null, "WATCHDOG_WRITE_NOT_VISIBLE");
    return created;
  },
  updateWatchdogCluster: async (sourceId: string, clusterId: number, action: "EXPECT" | "IGNORE" | "RESTORE", _reason?: string) => {
    const rows = await api.watchdogClusters(sourceId);
    const current = rows.find((item) => item.id === clusterId);
    if (!current) throw new ApiError("未找到指定 Watchdog cluster", 404, null, "WATCHDOG_CLUSTER_NOT_FOUND");
    const inventory_state = action === "IGNORE" ? "IGNORED" : "EXPECTED";
    await requestJson(`/api/v1/sources/${encodeURIComponent(sourceId)}/watchdog-clusters/${encodeURIComponent(current.identity_value)}`, json("PUT", { inventory_state }));
    return (await api.watchdogClusters(sourceId)).find((item) => item.id === clusterId) ?? current;
  },
  saveGrafanaConfig: async (sourceId: string, draft: EventSourceGrafanaDraft) => {
    const monitoring = await requestJson<MonitoringResponse[]>(`/api/v1/sources/${encodeURIComponent(sourceId)}/monitoring`);
    const current = monitoring.find((item) => item.kind === "GRAFANA") ?? null;
    await requestJson(`/api/v1/sources/${encodeURIComponent(sourceId)}/monitoring/GRAFANA`, json("PUT", { base_url: draft.url, secret: secretMap(draft.secret), expected_version: current?.version ?? null }));
    return mapSource(await requestJson<SourceResponse>(`/api/v1/sources/${encodeURIComponent(sourceId)}`));
  },
  testGrafanaConfig: async (sourceId: string) => {
    const current = await requestJson<MonitoringResponse>(`/api/v1/sources/${encodeURIComponent(sourceId)}/monitoring/GRAFANA`);
    const tested = await requestJson<MonitoringResponse>(`/api/v1/sources/${encodeURIComponent(sourceId)}/monitoring/GRAFANA/test`, json("POST", { expected_version: current.version }));
    return { ok: tested.last_test_code === "OK", code: tested.last_test_code ?? tested.state };
  },
  grafanaDashboards: async (sourceId: string, query: string) => (await requestJson<Schema<"DashboardSearchResponse">[]>(`/api/v1/sources/${encodeURIComponent(sourceId)}/grafana/dashboards?query=${encodeURIComponent(query)}`)).map((item) => ({ uid: item.uid, title: item.title, folder_title: "" } as GrafanaDashboardSummary)),
  previewGrafanaImport: async (sourceId: string, dashboardUid: string) => {
    const candidates = await requestJson<Schema<"GrafanaCandidateResponse">[]>(`/api/v1/sources/${encodeURIComponent(sourceId)}/grafana/import/preview`, json("POST", { dashboard_uid: dashboardUid }));
    return { dashboard_uid: dashboardUid, dashboard_title: candidates[0]?.dashboard_title ?? dashboardUid, probed: true, enabled_template_count: 0, max_auxiliary_curves: 5, candidates: candidates.map((item, index) => ({ panel_id: item.panel_id, panel_title: item.panel_title, ref_id: item.ref_id, raw_promql: item.imported_promql, resolved_promql: item.imported_promql, status: item.status === "UNSUPPORTED" ? "UNSUPPORTED" : item.probe_status === "SUCCESS" ? "READY" : item.required_variables.length ? "NEEDS_DECISION" : "UNVERIFIED", suggested_name: item.panel_title, order: index, reason: item.reason || item.probe_status, substitutions: [], pending_variables: item.required_variables.map((name) => ({ name, suggestion: "BIND_LABEL" as const, sample_value: "", multi: false })), display_unit: item.unit, probe_value: null, probe_note: item.probe_status, diff_kind: item.change_kind, template_id: item.template_id, imported_promql: item.imported_promql, current_promql: item.current_promql })) } as GrafanaImportPreview;
  },
  confirmGrafanaImport: async (sourceId: string, items: GrafanaImportItem[]) => {
    const selected = items.map((item) => ({ dashboard_uid: item.origin.dashboard_uid, dashboard_title: item.origin.dashboard_title, panel_id: item.origin.panel_id, panel_title: item.origin.panel_title, ref_id: item.origin.ref_id, imported_promql: item.imported_promql, required_variables: item.required_labels, legend_format: "", unit: item.display_unit, name: item.name, final_promql: item.final_promql, priority: item.order }));
    const templates = await requestJson<MetricTemplateResponse[]>(`/api/v1/sources/${encodeURIComponent(sourceId)}/grafana/import/confirm`, json("POST", { selections: selected }));
    return { created_template_ids: templates.map((item) => item.id), updated_template_ids: [] } as GrafanaImportResult;
  },
  workbenchUrl: () => requestJson<{ url: string }>(platformPath("/api/v1/settings/workbench-url")),
  saveWorkbenchUrl: (url: string) => requestJson<{ url: string }>(platformPath("/api/v1/settings/workbench-url"), json("PUT", { url })),
  maintenanceWindows: (includeEnded = false) => requestJson<MaintenanceResponse[]>(
    `/api/v1/maintenance-windows${includeEnded ? "?include_ended=true" : ""}`,
  ) as Promise<MaintenanceWindow[]>,
  createMaintenanceWindow: (draft: MaintenanceWindowDraft) =>
    commandJson<MaintenanceResponse>(
      "maintenance-window.create",
      platformPath("/api/v1/maintenance-windows"),
      draft,
    ) as Promise<MaintenanceWindow>,
  endMaintenanceWindow: (id: number, expectedVersion: number) =>
    requestJson<MaintenanceResponse>(
      `/api/v1/maintenance-windows/${id}/end`,
      json("POST", { expected_version: expectedVersion }),
    ) as Promise<MaintenanceWindow>,
  notificationChannels: async () => (await requestJson<NotificationChannelResponse[]>(platformPath("/api/v1/notification-channels"))).map(channel),
  createNotificationChannel: async (draft: NotificationChannelDraft) => {
    const shape = serializeChannelConfig(draft.config, secretMap);
    return channel(await commandJson<NotificationChannelResponse>("notification-channel.create", platformPath("/api/v1/notification-channels"), { name: draft.name, ...shape }));
  },
  updateNotificationChannel: async (channelId: string, draft: NotificationChannelDraft, expectedVersion: number) => {
    const shape = serializeChannelConfig(draft.config, secretMap);
    return channel(await requestJson<NotificationChannelResponse>(`/api/v1/notification-channels/${encodeURIComponent(channelId)}/draft`, json("PUT", { name: draft.name, ...shape, expected_revision: expectedVersion })));
  },
  testNotificationChannel: async (channelId: string, expectedVersion: number) => {
    const value = await commandJson<NotificationChannelResponse>(`notification-channel.test:${channelId}`, `/api/v1/notification-channels/${encodeURIComponent(channelId)}/test`, { expected_revision: expectedVersion });
    return { ok: value.last_test_code === "OK", code: value.last_test_code ?? "CHANNEL_TEST_FAILED" } as ConnectionTestResult;
  },
  activateNotificationChannel: async (channelId: string, expectedVersion: number) => channel(await requestJson<NotificationChannelResponse>(`/api/v1/notification-channels/${encodeURIComponent(channelId)}/activate`, json("POST", { expected_revision: expectedVersion }))),
  disableNotificationChannel: async (channelId: string, expectedVersion: number) => channel(await requestJson<NotificationChannelResponse>(`/api/v1/notification-channels/${encodeURIComponent(channelId)}/disable`, json("POST", { expected_revision: expectedVersion }))),
  enableNotificationChannel: async (channelId: string, expectedVersion: number) => channel(await requestJson<NotificationChannelResponse>(`/api/v1/notification-channels/${encodeURIComponent(channelId)}/enable`, json("POST", { expected_revision: expectedVersion }))),
  modelChannels: async () => (await requestJson<ModelChannelResponse[]>(platformPath("/api/v1/model-channels"))).map(modelChannel),
  createModelChannel: async (draft: ModelChannelDraft) => modelChannel(await commandJson<ModelChannelResponse>("model-channel.create", platformPath("/api/v1/model-channels"), { name: draft.name, kind: draft.config.kind, provider_id: draft.config.provider_id, base_url: draft.config.base_url, model: draft.config.model, api_key: secretMap(draft.config.api_key) })),
  updateModelChannel: async (channelId: string, draft: ModelChannelDraft, expectedRevision: number) => modelChannel(await requestJson<ModelChannelResponse>(`/api/v1/model-channels/${encodeURIComponent(channelId)}/draft`, json("PUT", { name: draft.name, kind: draft.config.kind, provider_id: draft.config.provider_id, base_url: draft.config.base_url, model: draft.config.model, api_key: secretMap(draft.config.api_key), expected_revision: expectedRevision }))),
  testModelChannel: async (channelId: string, revisionNo: number) => {
    const value = await commandJson<ModelChannelResponse>(`model-channel.test:${channelId}`, `/api/v1/model-channels/${encodeURIComponent(channelId)}/test`, { expected_revision: revisionNo });
    return { ok: value.last_test_code === "OK", code: value.last_test_code ?? "MODEL_TEST_FAILED", possibly_billed: true, detail: value.last_test_code ?? value.state } as ModelChannelTestResult;
  },
  modelRuntime: () => requestJson<Schema<"ModelRuntimeResponse">>(platformPath("/api/v1/model-runtime")),
  modelVendors: () => requestJson<ModelVendor[]>(platformPath("/api/v1/model-vendors")),
  listModelChannelModels: async (channelId: string) => ({ ok: true, code: "OK", detail: "", ...(await requestJson<Schema<"ModelListResponse">>(`/api/v1/model-channels/${encodeURIComponent(channelId)}/models`, json("POST", {}))) } as ModelListResult),
  activateModelChannel: async (channelId: string, revisionNo: number) => modelChannel(await requestJson<ModelChannelResponse>(`/api/v1/model-channels/${encodeURIComponent(channelId)}/activate`, json("POST", { expected_revision: revisionNo }))),
  disableModelChannel: async (channelId: string) => modelChannel(await requestJson<ModelChannelResponse>(`/api/v1/model-channels/${encodeURIComponent(channelId)}/disable`, json("POST", {}))),
  enableModelChannel: async (channelId: string) => modelChannel(await requestJson<ModelChannelResponse>(`/api/v1/model-channels/${encodeURIComponent(channelId)}/enable`, json("POST", {}))),
  promptProfiles: () => requestJson<PromptProfileResponse[]>(platformPath("/api/v1/prompt-profiles")) as Promise<PromptProfile[]>,
  promptProfileRevisions: (profileId: string) => requestJson<PromptProfileRevisionResponse[]>(`/api/v1/prompt-profiles/${encodeURIComponent(profileId)}/revisions`) as Promise<PromptProfileRevision[]>,
  copyPromptProfile: (name: string) => requestJson<PromptProfileResponse>(platformPath("/api/v1/prompt-profiles/copy-standard"), json("POST", { name })) as Promise<PromptProfile>,
  updatePromptProfile: (profileId: string, guidance: PromptGuidance, expectedRevision: number) => requestJson<PromptProfileResponse>(`/api/v1/prompt-profiles/${encodeURIComponent(profileId)}/draft`, json("PUT", { ...guidance, expected_revision: expectedRevision })) as Promise<PromptProfile>,
  previewPromptProfile: (profileId: string, expectedRevision: number) => requestJson<Schema<"PromptProfilePreviewResponse">>(`/api/v1/prompt-profiles/${encodeURIComponent(profileId)}/preview`, json("POST", { expected_revision: expectedRevision })) as Promise<PromptProfilePreview>,
  testPromptProfile: (profileId: string, expectedRevision: number) => requestJson<PromptProfileResponse>(`/api/v1/prompt-profiles/${encodeURIComponent(profileId)}/test`, json("POST", { expected_revision: expectedRevision })) as Promise<PromptProfile>,
  activatePromptProfile: (profileId: string, expectedRevision: number, globalDefault: boolean, serviceIds: number[]) => requestJson<PromptProfileResponse>(`/api/v1/prompt-profiles/${encodeURIComponent(profileId)}/activate`, json("POST", { expected_revision: expectedRevision, global_default: globalDefault, service_ids: serviceIds })) as Promise<PromptProfile>,
  notificationPolicies: async () => (await requestJson<NotificationPolicyResponse[]>(platformPath("/api/v1/notification-policies"))).map(policy),
  createNotificationPolicy: async (draft: NotificationPolicyDraft) => policy(await commandJson<NotificationPolicyResponse>("notification-policy.create", platformPath("/api/v1/notification-policies"), policyBody(draft))),
  updateNotificationPolicy: async (logicalId: string, draft: NotificationPolicyDraft, expectedVersion: number) => policy(await requestJson<NotificationPolicyResponse>(`/api/v1/notification-policies/${encodeURIComponent(logicalId)}/draft`, json("PUT", { ...policyBody(draft), expected_version: expectedVersion }))),
  previewNotificationPolicy: async (draft: NotificationPolicyDraft) => requestJson<NotificationPolicyPreview>(platformPath("/api/v1/notification-policies/preview"), json("POST", policyBody(draft))),
  prepareNotificationPolicyActivation: (revisionId: number, expectedVersion: number, notifyExisting: boolean) => requestJson<PreparedPolicyActivation>(`/api/v1/notification-policies/${revisionId}/prepare-activation`, json("POST", { expected_version: expectedVersion, notify_existing: notifyExisting })),
  activateNotificationPolicy: async (revisionId: number, expectedVersion: number, notifyExisting: boolean, confirmToken: string) => policy(await requestJson<NotificationPolicyResponse>(`/api/v1/notification-policies/${revisionId}/activate`, json("POST", { expected_version: expectedVersion, notify_existing: notifyExisting, confirm_token: confirmToken }))),
  disableNotificationPolicy: async (revisionId: number, expectedVersion: number) => policy(await requestJson<NotificationPolicyResponse>(`/api/v1/notification-policies/${revisionId}/disable`, json("POST", { expected_version: expectedVersion }))),
  notificationDeliveries: async (filters: { state?: string; eventType?: string; channelId?: string; incidentId?: number; beforeId?: number } = {}) => {
    const query = new URLSearchParams();
    if (filters.state) query.set("state", filters.state);
    if (filters.eventType) query.set("event_type", filters.eventType);
    if (filters.channelId) query.set("channel_id", filters.channelId);
    if (filters.incidentId) query.set("incident_id", String(filters.incidentId));
    if (filters.beforeId) query.set("before_id", String(filters.beforeId));
    return (await requestJson<NotificationDeliveryResponse[]>(`/api/v1/notification-deliveries?${query}`)).map(delivery);
  },
  notificationDelivery: async (deliveryId: number) => delivery(await requestJson<NotificationDeliveryResponse>(`/api/v1/notification-deliveries/${deliveryId}`)),
  retryNotificationDelivery: async (deliveryId: number) => {
    const value = await requestJson<Schema<"NotificationRetryResponse">>(`/api/v1/notification-deliveries/${deliveryId}/retry`, json("POST", {}));
    return { delivery: delivery(value.delivery), warning: value.warning };
  },
  incidentNotification: (incidentId: number) => requestJson<IncidentNotification>(`/api/v1/incidents/${incidentId}/notification`),
  metricEvidence: async (alertId: number) => {
    const value = await requestJson<MetricEvidenceResponse>(`/api/v1/alerts/${alertId}/metric-evidence`);
    const unavailable = value.curves.filter((curve) => curve.result.status === "SOURCE_UNAVAILABLE");
    const empty = value.curves.filter((curve) => curve.result.status === "EMPTY_NO_DATA");
    return {
      alert_starts_at: value.alert_starts_at,
      curves: value.curves.map((curve, index) => metricCurve(curve, value, index)),
      warnings: empty.map((curve) => ({
        kind: "EMPTY_NO_DATA",
        subject: curve.name,
        detail: "查询成功，时间窗内没有数据",
      })),
      failures: [
        ...value.failures.map((code) => ({ kind: code, subject: code, detail: code })),
        ...unavailable.map((curve) => ({
          kind: "SOURCE_UNAVAILABLE",
          subject: curve.name,
          detail: curve.result.safe_error_code ?? "METRIC_SOURCE_UNAVAILABLE",
        })),
      ],
    } as MetricEvidence;
  },
  metricTemplates: async () => (await requestJson<MetricTemplateResponse[]>(platformPath("/api/v1/metric-templates"))).map(template),
  createMetricTemplate: async (body: { name: string; promql: string; required_labels: string[]; description: string; priority?: number; display_unit?: string; source_scope?: MetricTemplate["source_scope"] }) => template(await commandJson<MetricTemplateResponse>("metric-template.create", platformPath("/api/v1/metric-templates"), { name: body.name, promql: body.promql, required_labels: body.required_labels, description: body.description, priority: body.priority ?? 100, unit: body.display_unit ?? "", enabled: true, source_ids: body.source_scope?.mode === "SELECTED" ? body.source_scope.source_ids : [], legend_format: "" })),
  updateMetricTemplate: async (templateId: number, body: Partial<Pick<MetricTemplate, "enabled" | "priority" | "promql" | "name" | "display_unit" | "source_scope">>) => {
    const current = (await requestJson<MetricTemplateResponse[]>(platformPath("/api/v1/metric-templates"))).find((item) => item.id === templateId);
    if (!current) throw new ApiError("未找到指定指标模板", 404, null, "METRIC_TEMPLATE_NOT_FOUND");
    return template(await requestJson<MetricTemplateResponse>(`/api/v1/metric-templates/${templateId}`, json("PUT", { name: body.name ?? current.name, promql: body.promql ?? current.promql, enabled: body.enabled ?? current.enabled, priority: body.priority ?? current.priority, source_ids: body.source_scope ? (body.source_scope.mode === "SELECTED" ? body.source_scope.source_ids : []) : current.source_ids, description: current.description, required_labels: current.required_labels, legend_format: current.legend_format, unit: body.display_unit ?? current.unit, expected_version: current.version })));
  },
};
