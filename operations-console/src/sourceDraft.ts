// Pure form logic for 系统设置, kept out of the page so the minimal-registration
// contract can be tested without a DOM. See PRODUCT_SPEC.md CAP-01/CAP-08.
import type {
  EventSource,
  EventSourceAuthType,
  EventSourceCandidate,
  EventSourceEndpoint,
  EventSourceEndpointDraft,
  EventSourceThanosDraft,
} from "./types";

export function emptyEndpoint(): EventSourceEndpointDraft {
  return {
    position: null,
    url: "",
    enabled: true,
    auth_type: "NONE",
    username: "",
    secret: { action: "CLEAR" },
  };
}

export function emptyThanos(): EventSourceThanosDraft {
  return {
    url: "",
    auth_type: "NONE",
    username: "",
    secret: { action: "CLEAR" },
    timeout_seconds: 15,
  };
}

/**
 * Defaults good enough that adding a source needs a name and an address.
 * Everything else here is a value the user never has to see to get started.
 */
export function emptySourceDraft(): EventSourceCandidate {
  return {
    name: "",
    endpoints: [emptyEndpoint()],
    thanos: emptyThanos(),
    poll_interval_seconds: 30,
    resolution_grace_seconds: 60,
    max_parallel_endpoints: 4,
    watchdog_enabled: false,
    watchdog_alertname: "Watchdog",
    watchdog_identity_label: "cluster",
    watchdog_missing_after_seconds: 90,
  };
}

/** KEEP only where something is already stored; a secret is never read back. */
export function authSecret(
  authType: EventSourceAuthType | "NONE" | "BEARER",
  configured: boolean,
): EventSourceEndpointDraft["secret"] {
  if (authType === "NONE") return { action: "CLEAR" };
  return configured ? { action: "KEEP" } : { action: "REPLACE", value: "" };
}

export function endpointDraftFromOut(
  endpoint: EventSourceEndpoint,
): EventSourceEndpointDraft {
  return {
    position: endpoint.position,
    url: endpoint.url,
    enabled: endpoint.enabled,
    auth_type: endpoint.auth_type,
    username: endpoint.username,
    secret: authSecret(endpoint.auth_type, endpoint.secret_configured),
  };
}

export function draftFromSource(source: EventSource): EventSourceCandidate {
  const config = source.config;
  if (!config) return { ...emptySourceDraft(), name: source.name };
  return {
    name: source.name,
    endpoints: config.endpoints.map(endpointDraftFromOut),
    thanos: config.thanos
      ? {
          url: config.thanos.url,
          auth_type: config.thanos.auth_type,
          username: config.thanos.username,
          secret: authSecret(config.thanos.auth_type, config.thanos.secret_configured),
          timeout_seconds: config.thanos.timeout_seconds,
        }
      : emptyThanos(),
    poll_interval_seconds: config.poll_interval_seconds,
    resolution_grace_seconds: config.resolution_grace_seconds,
    max_parallel_endpoints: config.max_parallel_endpoints,
    watchdog_enabled: config.watchdog_enabled,
    watchdog_alertname: config.watchdog_alertname,
    watchdog_identity_label: config.watchdog_identity_label,
    watchdog_missing_after_seconds: config.watchdog_missing_after_seconds,
  };
}

/** Exactly two required fields. A failed connection test is not one of them. */
export function canSave(draft: EventSourceCandidate): boolean {
  return (
    draft.name.trim() !== ""
    && draft.endpoints.length > 0
    && draft.endpoints.every((endpoint) => endpoint.url.trim() !== "")
  );
}

/** Each collapsed section says what is inside, so it can stay collapsed. */
export function authSummary(draft: EventSourceCandidate): string {
  const kinds = new Set(draft.endpoints.map((item) => item.auth_type));
  if (kinds.size === 1 && kinds.has("NONE")) return "无认证";
  return [...kinds].filter((kind) => kind !== "NONE").join(" / ");
}

export function historySummary(draft: EventSourceCandidate): string {
  return draft.thanos?.url ? draft.thanos.url : "未配置";
}

export function backupSummary(draft: EventSourceCandidate): string {
  const backups = draft.endpoints.length - 1;
  return backups > 0 ? `${backups} 个` : "未配置";
}

export function advancedSummary(draft: EventSourceCandidate): string {
  return [
    `轮询 ${draft.poll_interval_seconds}s`,
    `恢复宽限 ${draft.resolution_grace_seconds}s`,
    `Watchdog ${draft.watchdog_enabled ? "开" : "关"}`,
  ].join(" · ");
}

/**
 * Give every endpoint a concrete slot before saving.
 *
 * Rows loaded from the server keep the slot they came from, so removing one
 * does not renumber the others. Rows the user just added claim the lowest slot
 * nobody holds. Without this the server falls back to array order and a `KEEP`
 * secret resolves to whoever used to sit at that index.
 */
export function withAssignedPositions(
  draft: EventSourceCandidate,
): EventSourceCandidate {
  // `secretField` is what the form box shows; it is not part of the contract and
  // must not be sent. Stripping it here keeps the one place that prepares a
  // payload responsible for the whole shape.
  const taken = new Set(
    draft.endpoints
      .map((endpoint) => endpoint.position)
      .filter((position): position is number => position !== null),
  );
  let next = 0;
  const claim = () => {
    while (taken.has(next)) next += 1;
    taken.add(next);
    return next;
  };
  const strip = <T extends { secretField?: unknown }>(item: T): T => {
    const { secretField: _dropped, ...rest } = item;
    return rest as T;
  };
  return {
    ...draft,
    thanos: draft.thanos ? strip(draft.thanos) : draft.thanos,
    endpoints: draft.endpoints.map((endpoint) =>
      strip(endpoint.position === null ? { ...endpoint, position: claim() } : endpoint),
    ),
  };
}
