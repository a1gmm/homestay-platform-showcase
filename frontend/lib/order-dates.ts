import dayjs from "dayjs";

export interface EditDatesRoomPayload {
  room_id: string | null;
  check_in_date: string;
  check_out_date: string;
  list_price: number | null;
  actual_price: number | null;
  guests_count: number;
  position: number;
}

export interface EditDatesPayload {
  rooms: EditDatesRoomPayload[];
  actual_price: number;
}

// 单房改日期 → 把当前 rooms 数组拷贝一份，只换目标行的日期，
// 走 update_order rooms 整体替换路径，后端有"续住保留 daily_prices"逻辑。
// 关键：目标房间的 actual_price 必须 = 新日期范围内 daily_prices 之和，
// 否则后端 fallthrough 会按"用户改了总价"重新均摊，缩短住时会把全额压到剩余天数。
//
// 续住新增日期(原 daily_prices 里没有的 key)按"上一日价"自动填，让运营点"续住+1晚"后
// actual_price 自动 += 上一晚价，无需手动单日改价。打折场景仍可用 chip 单日改价覆盖。
// 2026-05-28 王总确认。
export function buildEditDatesPayload(
  orderRooms: any[],
  orderRoomId: string,
  ci: string,
  co: string
): EditDatesPayload {
  const ors: any[] = Array.isArray(orderRooms) ? orderRooms : [];
  const rooms = ors.map((or, i) => {
    const isTarget = or.order_room_id === orderRoomId;
    let newActual = or.actual_price ?? null;
    if (isTarget) {
      const dp: Record<string, string> = or.daily_prices || {};
      const oldNights = or.nights ?? Object.keys(dp).length ?? 1;
      const avgPrice = or.actual_price != null && oldNights > 0
        ? Number(or.actual_price) / oldNights
        : 0;
      // string 形态累加分,避免浮点漂移
      let sumCents = 0;
      let d = dayjs(ci);
      const end = dayjs(co);
      while (d.isBefore(end)) {
        const k = d.format("YYYY-MM-DD");
        let priceStr = dp[k];
        if (priceStr == null || priceStr === "") {
          // 续住新增日期:取上一日价;若上一日也缺(理论不会发生)退到均价
          const prevK = d.subtract(1, "day").format("YYYY-MM-DD");
          priceStr = dp[prevK];
          if (priceStr == null || priceStr === "") {
            priceStr = avgPrice.toFixed(2);
          }
        }
        sumCents += Math.round(Number(priceStr) * 100);
        d = d.add(1, "day");
      }
      newActual = sumCents / 100;
    }
    return {
      room_id: or.room_id || null,
      check_in_date: isTarget ? ci : or.check_in_date,
      check_out_date: isTarget ? co : or.check_out_date,
      list_price: or.list_price ?? null,
      actual_price: newActual,
      guests_count: or.guests_count ?? 0,
      position: typeof or.position === "number" ? or.position : i,
    };
  });
  const totalCents = rooms.reduce(
    (acc, r) => acc + Math.round(Number(r.actual_price ?? 0) * 100),
    0,
  );
  return { rooms, actual_price: totalCents / 100 };
}
