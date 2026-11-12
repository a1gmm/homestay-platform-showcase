"use client";

import { Button, Tooltip } from "antd";
import type { CalendarRoom } from "@/lib/types";
import { tokens } from "@/lib/design-tokens";

export function OverdueRoomNotice({ room, onOrderClick }: {
  room: CalendarRoom;
  onOrderClick?: (orderId: string) => void;
}) {
  const stay = room.overdue_stay;
  if (!stay) return null;
  return (
    <Tooltip title={`原定 ${stay.expected_checkout_date} 退房，系统仍记为在住。同客请处理续住，已离店请核实退房。点击查看原订单。`}>
      <Button
        type="text"
        aria-label={`${room.room_id} 超期未退，查看原订单`}
        href={onOrderClick ? undefined : `/orders?order_id=${encodeURIComponent(stay.order_id)}`}
        onClick={(event) => { event.stopPropagation(); onOrderClick?.(stay.order_id); }}
        style={{ width: "100%", minHeight: 44, padding: "2px 4px", color: tokens.anyu.color.clay,
          display: "flex", flexDirection: "column", gap: 0, lineHeight: 1.4, fontSize: 11 }}
      >
        <span>{room.room_id}</span>
        <span>超期未退</span>
      </Button>
    </Tooltip>
  );
}
