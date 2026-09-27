/**
 * Notification reads and writes.
 *
 * Every outbound-capable action here is deliberately explicit — sending a test
 * message and activating a channel are the only paths in this product that talk
 * to a real Feishu group (`AGENTS.md` 安全边界). Nothing in this file runs on a
 * timer, and no mutation is retried: an at-most-once attempt that the user
 * initiated is the whole point.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type {
  NotificationChannelDraft,
  NotificationPolicyDraft,
} from "../types";

export const notificationKeys = {
  channels: () => ["notification-channels"] as const,
  policies: () => ["notification-policies"] as const,
  deliveries: (filters: { state: string; eventType: string; channelId: string; incidentId?: number; beforeId?: number }) =>
    ["notification-deliveries", filters] as const,
  delivery: (id: number) => ["notification-delivery", id] as const,
  workbenchUrl: () => ["workbench-url"] as const,
  incident: (id: number) => ["incident-notification", id] as const,
};

export function useNotificationChannels() {
  return useQuery({ queryKey: notificationKeys.channels(), queryFn: () => api.notificationChannels() });
}

export function useNotificationPolicies() {
  return useQuery({ queryKey: notificationKeys.policies(), queryFn: () => api.notificationPolicies() });
}

export function useNotificationDeliveries(filters: {
  state: string;
  eventType: string;
  channelId: string;
  incidentId?: number;
  beforeId?: number;
}, enabled = true) {
  return useQuery({
    queryKey: notificationKeys.deliveries(filters),
    enabled,
    queryFn: () => {
      if (!enabled) throw new Error("请输入有效的正整数 Incident ID。");
      return api.notificationDeliveries({
        state: filters.state || undefined,
        eventType: filters.eventType || undefined,
        channelId: filters.channelId || undefined,
        incidentId: filters.incidentId,
        beforeId: filters.beforeId,
      });
    },
  });
}

export function useNotificationDelivery(deliveryId: number | null) {
  return useQuery({
    queryKey: notificationKeys.delivery(deliveryId ?? -1),
    queryFn: () => api.notificationDelivery(deliveryId as number),
    enabled: deliveryId !== null,
  });
}

export function useWorkbenchUrl() {
  return useQuery({ queryKey: notificationKeys.workbenchUrl(), queryFn: () => api.workbenchUrl() });
}

export function useIncidentNotification(incidentId: number | null) {
  return useQuery({
    queryKey: notificationKeys.incident(incidentId ?? -1),
    queryFn: () => api.incidentNotification(incidentId as number),
    enabled: incidentId !== null,
  });
}

/** Channel and policy writes all change the same two lists. */
export function useNotificationInvalidation() {
  const queryClient = useQueryClient();
  return () =>
    Promise.all([
      queryClient.invalidateQueries({ queryKey: notificationKeys.channels() }),
      queryClient.invalidateQueries({ queryKey: notificationKeys.policies() }),
    ]);
}

export function useNotificationAction<TArgs>(action: (args: TArgs) => Promise<unknown>) {
  const invalidate = useNotificationInvalidation();
  return useMutation({
    mutationFn: action,
    onSuccess: () => invalidate(),
  });
}

export function useDeliveryInvalidation() {
  const queryClient = useQueryClient();
  return (deliveryId: number) =>
    Promise.all([
      queryClient.invalidateQueries({ queryKey: ["notification-deliveries"] }),
      queryClient.invalidateQueries({ queryKey: notificationKeys.delivery(deliveryId) }),
    ]);
}

export function useSaveWorkbenchUrl() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (url: string) => api.saveWorkbenchUrl(url),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: notificationKeys.workbenchUrl() }),
  });
}

export function useNotificationCommands() {
  const invalidate = useNotificationInvalidation();
  const invalidateDelivery = useDeliveryInvalidation();
  const afterWrite = async <T>(write: Promise<T>): Promise<T> => {
    const result = await write;
    await invalidate();
    return result;
  };
  return {
    createNotificationChannel: (draft: NotificationChannelDraft) =>
      afterWrite(api.createNotificationChannel(draft)),
    updateNotificationChannel: (
      channelId: string,
      draft: NotificationChannelDraft,
      version: number,
    ) => afterWrite(api.updateNotificationChannel(channelId, draft, version)),
    enableNotificationChannel: (channelId: string, version: number) =>
      afterWrite(api.enableNotificationChannel(channelId, version)),
    disableNotificationChannel: (channelId: string, version: number) =>
      afterWrite(api.disableNotificationChannel(channelId, version)),
    testNotificationChannel: (channelId: string, version: number) =>
      afterWrite(api.testNotificationChannel(channelId, version)),
    activateNotificationChannel: (channelId: string, version: number) =>
      afterWrite(api.activateNotificationChannel(channelId, version)),
    createNotificationPolicy: (draft: NotificationPolicyDraft) =>
      afterWrite(api.createNotificationPolicy(draft)),
    updateNotificationPolicy: (
      logicalId: string,
      draft: NotificationPolicyDraft,
      version: number,
    ) => afterWrite(api.updateNotificationPolicy(logicalId, draft, version)),
    prepareNotificationPolicyActivation: api.prepareNotificationPolicyActivation,
    activateNotificationPolicy: (
      revisionId: number,
      version: number,
      notifyExisting: boolean,
      token: string,
    ) => afterWrite(
      api.activateNotificationPolicy(
        revisionId,
        version,
        notifyExisting,
        token,
      ),
    ),
    disableNotificationPolicy: (revisionId: number, version: number) =>
      afterWrite(api.disableNotificationPolicy(revisionId, version)),
    previewNotificationPolicy: api.previewNotificationPolicy,
    retryNotificationDelivery: async (deliveryId: number) => {
      const result = await api.retryNotificationDelivery(deliveryId);
      await invalidateDelivery(deliveryId);
      return result;
    },
  };
}
