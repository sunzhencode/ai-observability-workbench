import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { AnalyticsSelection } from "../appUrl";
import { queryKeys } from "./keys";

export function useAnalyticsOverview(filters: AnalyticsSelection) {
  return useQuery({
    queryKey: queryKeys.analytics(filters),
    queryFn: () => api.analyticsOverview(filters),
  });
}
