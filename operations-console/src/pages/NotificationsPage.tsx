/**
 * 通知 — the container for the three notification panels.
 *
 * The tab is a path segment (`/notifications/channels`) and the selected
 * policy / channel / delivery is a query parameter, so "look at this delivery"
 * is a link. Switching tabs pushes: the tabs are navigation, and back should
 * return to the one you came from.
 */
import { useCallback } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router";
import { ChannelsPanel } from "../components/notifications/ChannelsPanel";
import { DeliveriesPanel } from "../components/notifications/DeliveriesPanel";
import { PoliciesPanel } from "../components/notifications/PoliciesPanel";
import { WorkbenchUrlPanel } from "../components/notifications/WorkbenchUrlPanel";
import {
  NOTIFICATION_TABS,
  notificationsPath,
  parseNotificationTab,
  type NotificationTabName,
} from "../appUrl";
import { useEventSources } from "../queries/catalog";
import { useHealth } from "../queries/health";
import {
  useNotificationChannels,
  useNotificationInvalidation,
  useNotificationPolicies,
} from "../queries/notifications";

const TAB_LABELS: Record<NotificationTabName, string> = {
  policies: "策略",
  channels: "通道",
  deliveries: "投递记录",
};

/** A numeric query parameter, or null when it is absent or not a positive id. */
function numericParam(value: string | null): number | null {
  if (value === null || !/^\d+$/.test(value)) return null;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null;
}

function stringParam(value: string | null): string | null {
  return value?.trim() || null;
}

export function NotificationsPage() {
  const navigate = useNavigate();
  const { tab: tabParam } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const tab = parseNotificationTab(tabParam);

  const health = useHealth();
  const channelsQuery = useNotificationChannels();
  const policiesQuery = useNotificationPolicies();
  const sourcesQuery = useEventSources();
  const refresh = useNotificationInvalidation();

  const channels = channelsQuery.data ?? [];
  const policies = policiesQuery.data ?? [];
  const sources = sourcesQuery.data ?? [];

  const setSelection = useCallback(
    (key: string, value: string | null) => {
      const next = new URLSearchParams(searchParams);
      if (value === null) next.delete(key);
      else next.set(key, value);
      setSearchParams(next, { replace: true });
    },
    [searchParams, setSearchParams],
  );

  // Switching tabs drops the other tabs' selections: they name objects this
  // tab does not show, and carrying them would build links that look valid
  // and open the wrong thing.
  const goToTab = (next: NotificationTabName) => navigate(notificationsPath(next));

  const loadError =
    channelsQuery.isError || policiesQuery.isError || sourcesQuery.isError
      ? "通知配置暂时不可用；当前内容可能不完整，请刷新后重试。"
      : "";

  return (
    <main className="notifications-page management-page">
      <div className="domain-tabs" role="tablist" aria-label="通知管理">
        {NOTIFICATION_TABS.map((item) => (
          <button
            aria-selected={tab === item}
            className={tab === item ? "domain-tab--active" : ""}
            key={item}
            onClick={() => goToTab(item)}
            role="tab"
            type="button"
          >
            {TAB_LABELS[item]}
          </button>
        ))}
      </div>

      {health.data?.notifications && (
        <div
          className={`notification-health notification-health--${health.data.notifications.status}`}
          role="status"
        >
          <span
            className={`delivery-symbol delivery-symbol--${
              health.data.notifications.status === "healthy"
                ? "succeeded"
                : health.data.notifications.status === "degraded"
                  ? "permanent_failed"
                  : "pending"
            }`}
            aria-hidden="true"
          >
            ●
          </span>
          <strong>通知 {health.data.notifications.status}</strong>
          <span>策略 {health.data.notifications.active_policies}</span>
          <span>通道 {health.data.notifications.active_channels}</span>
          <span>待投递 {health.data.notifications.pending}</span>
          <span>重试中 {health.data.notifications.retrying}</span>
          <span>24h 永久失败 {health.data.notifications.permanent_failed_24h}</span>
          {health.data.notifications.oldest_pending_seconds !== null && (
            <span>最老积压 {health.data.notifications.oldest_pending_seconds}s</span>
          )}
          {health.data.notifications.last_error_code && (
            <code>{health.data.notifications.last_error_code}</code>
          )}
        </div>
      )}

      {loadError && (
        <div className="toast-line toast-line--error" role="alert">
          {loadError}
        </div>
      )}

      <div className="domain-body">
        {tab === "policies" && (
          <PoliciesPanel
            channels={channels}
            onSelect={(logicalId) => setSelection("policy", logicalId)}
            policies={policies}
            refresh={refresh}
            selectedLogicalId={searchParams.get("policy")}
            sources={sources}
          />
        )}
        {tab === "channels" && (
          <>
            <ChannelsPanel
              channels={channels}
              onSelect={(channelId) =>
                setSelection("channel", channelId === null ? null : String(channelId))
              }
              policies={policies}
              refresh={refresh}
              selectedId={stringParam(searchParams.get("channel"))}
            />
            <WorkbenchUrlPanel />
          </>
        )}
        {tab === "deliveries" && (
          <DeliveriesPanel
            channels={channels}
            onSelect={(deliveryId) =>
              setSelection("delivery", deliveryId === null ? null : String(deliveryId))
            }
            selectedId={numericParam(searchParams.get("delivery"))}
          />
        )}
      </div>
    </main>
  );
}
