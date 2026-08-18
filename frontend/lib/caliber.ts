/** 口径术语（对齐 bypms）：
 *  - 房费   = actual_price = 客人开票金额 = 平台单里我们挂的房费
 *  - 净房费 = ota_owner_revenue = 平台扣佣金后结给我们的钱（到手）
 *  铁律：房费 ≥ 净房费，差额 = 平台佣金。
 *
 *  防呆：平台单里「房费 ≤ 净房费」（含相等/佣金=0）几乎一定填错了——
 *  运营最常犯的错就是把「房费」下调到「净房费」的值(两者相等、佣金变0)，
 *  其次是干脆把净房费填进房费框(净房费>房费)。见 2026-07-02 口径对齐设计。
 */

const CENT = 0.01;

/** 检测平台单是否疑似把房费/净房费填反或抹平佣金。返回黄字提醒文案；无需提醒时返回 null。 */
export function reversedCaliberWarning(params: {
  isPlatform: boolean;
  ownerRevenue: number | null | undefined; // 净房费（到手）
  roomFeeTotal: number | null | undefined; // 房费合计
}): string | null {
  const { isPlatform, ownerRevenue, roomFeeTotal } = params;
  if (!isPlatform) return null;
  if (ownerRevenue == null || roomFeeTotal == null) return null;
  const net = Number(ownerRevenue);
  const fangfei = Number(roomFeeTotal);
  if (!(net > 0) || !(fangfei > 0)) return null;
  // 房费 ≤ 净房费（差额不足 1 分即视作相等）→ 平台佣金 ≤ 0，异常。
  if (fangfei - net < CENT) {
    if (net > fangfei + CENT) {
      return `净房费（¥${net.toLocaleString()}）比房费（¥${fangfei.toLocaleString()}）还大，通常是把两个数填反了。房费应 ≥ 净房费，差额 = 平台佣金。`;
    }
    return `房费和净房费都是 ¥${fangfei.toLocaleString()}，意味着平台没抽佣金。平台单通常房费 > 净房费（差额 = 佣金），是不是把房费误填成了净房费？`;
  }
  return null;
}

/**
 * 提交前的填反防呆：多房手填每房净值时按每房粒度查（便于定位是哪间填反），
 * 否则按整单查。返回首个命中的提醒文案（每房命中带房号前缀），无则 null。
 * 两个录单入口 + 编辑弹窗共用，避免防呆逻辑分叉（评审 2026-07-05）。
 */
export function perRoomOrOrderReversedWarning(params: {
  isPlatform: boolean;
  perRoomActive: boolean;
  rooms: Array<{
    room_id?: string | null;
    actual_price?: number | null;
    ota_owner_revenue?: number | null;
  }>;
  orderOwnerRevenue: number | null | undefined;
  roomFeeTotal: number | null | undefined;
}): string | null {
  const { isPlatform, perRoomActive, rooms, orderOwnerRevenue, roomFeeTotal } = params;
  if (perRoomActive) {
    for (const r of rooms) {
      const w = reversedCaliberWarning({
        isPlatform,
        ownerRevenue: r.ota_owner_revenue,
        roomFeeTotal: r.actual_price,
      });
      if (w) return `房间「${r.room_id || "待排房"}」：${w}`;
    }
    return null;
  }
  return reversedCaliberWarning({ isPlatform, ownerRevenue: orderOwnerRevenue, roomFeeTotal });
}
