import { useQuery } from "@tanstack/react-query";
import { dashboardApi } from "@/lib/api";

/**
 * Single-batch dashboard query. One HTTP round trip returns:
 *   { today, monthly, trend, channel, comparison }
 */
export function useDashboard() {
  const today = new Date();
  const year = today.getFullYear();
  const month = today.getMonth() + 1;

  const overviewQuery = useQuery({
    queryKey: ["dashboard", "overview", year, month],
    queryFn: () => dashboardApi.overview(year, month, 6).then((r) => r.data),
    staleTime: 2 * 60 * 1000,
  });

  const data = overviewQuery.data;

  return {
    overview: data,
    todayStats: data?.today,
    monthlyStats: data?.monthly ?? undefined,
    trend: data?.trend ?? undefined,
    channel: data?.channel ?? undefined,
    comparison: data?.comparison ?? undefined,
    isLoading: overviewQuery.isLoading,
    // Legacy shape for callers that still reference *Query.isLoading
    overviewQuery,
    todayQuery: overviewQuery,
    monthlyQuery: overviewQuery,
    trendQuery: overviewQuery,
  };
}
