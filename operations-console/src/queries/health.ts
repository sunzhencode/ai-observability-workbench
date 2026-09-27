/**
 * Backend health, subscribed to by whoever is on screen.
 *
 * This is the first query to replace part of the shell's `setInterval`: the
 * top bar, the alerts page and the notifications page all used to receive
 * `health` as a prop from one poll that ran whatever the user was looking at.
 */
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { queryKeys } from "./keys";

/** Unchanged from the shell's old top-level poll. */
export const HEALTH_POLL_MS = 15000;

export function useHealth() {
  return useQuery({
    queryKey: queryKeys.health(),
    queryFn: () => api.health(),
    refetchInterval: HEALTH_POLL_MS,
  });
}
