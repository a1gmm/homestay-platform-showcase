import { useQuery } from "@tanstack/react-query";
import { roomsApi } from "@/lib/api";

export function useRoomAvailability(year: number, month: number, enabled: boolean = true) {
  const calendarQuery = useQuery({
    queryKey: ["rooms", "calendar", year, month],
    queryFn: () => roomsApi.calendar(year, month).then((r) => r.data),
    enabled,
  });

  return {
    calendar: calendarQuery.data,
    isLoading: calendarQuery.isLoading,
    error: calendarQuery.error,
    query: calendarQuery,
  };
}

