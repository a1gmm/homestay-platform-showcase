"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { roomsApi } from "@/lib/api";

/** Selected-room explanation and a fresh check before form submission. */
export function useSelectedRoomAvailability(roomId?: string, checkIn?: string, checkOut?: string, excludeOrderId?: string, validateAvailability = false) {
  const qc = useQueryClient();
  const validDates = !!checkIn && !!checkOut && checkOut > checkIn;
  const list = useQuery({
    queryKey: ["rooms", "availability", checkIn, checkOut, excludeOrderId ?? null],
    queryFn: () => roomsApi.availabilityList(checkIn!, checkOut!, excludeOrderId).then(r => r.data),
    enabled: validDates,
  });
  const options = {
    queryKey: ["rooms", "availability", "selected", roomId, checkIn, checkOut, excludeOrderId ?? null],
    queryFn: () => roomsApi.checkAvailability({ room_id: roomId!, check_in_date: checkIn!, check_out_date: checkOut!, exclude_order_id: excludeOrderId }).then(r => r.data),
    staleTime: 0,
    retry: false as const,
  };
  const selected = useQuery({ ...options, enabled: validateAvailability && !!roomId && validDates });
  const validate = async (_: unknown, value?: string) => {
    if (!validateAvailability || !value || !validDates) return;
    let result;
    try {
      result = await qc.fetchQuery({ ...options,
        queryKey: ["rooms", "availability", "selected", value, checkIn, checkOut, excludeOrderId ?? null],
        queryFn: () => roomsApi.checkAvailability({ room_id: value, check_in_date: checkIn!, check_out_date: checkOut!, exclude_order_id: excludeOrderId }).then(r => r.data),
      });
    } catch {
      throw new Error("房态检查失败，请重试后再保存。");
    }
    if (!result.available) throw new Error("请先处理房态冲突，或更换房间、日期。");
  };
  return { list, selected, validate, validDates };
}
