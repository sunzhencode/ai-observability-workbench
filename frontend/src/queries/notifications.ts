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

export const notificationKeys = {
  channels: () => ["notification-channels"] as const,
  policies: () => ["notification-policies"] as const,
  deliveries: (filters: { state: string; eventType: string; channelId: string }) =>
    ["notification-deliveries", filters] as const,
  delivery: (id: number) => ["notification-delivery", id] as const,
  workbenchUrl: () => ["workbench-url"] as const,
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
}) {
  return useQuery({
    queryKey: notificationKeys.deliveries(filters),
    queryFn: () =>
      api.notificationDeliveries({
        state: filters.state || undefined,
        eventType: filters.eventType || undefined,
        channelId: filters.channelId ? Number(filters.channelId) : undefined,
      }),
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
