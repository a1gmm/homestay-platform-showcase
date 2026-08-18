/**
 * 渠道分布占比——支持按「单量」或「净收入」两种口径切换。
 *
 * 老板真正关心的往往不是哪个渠道单多,而是哪个渠道扣完佣金到手多。
 * 携程可能单量第一,但净收入未必第一。
 */

export type ChannelMetric = "orders" | "net_revenue";

export interface ChannelRow {
  channel: string;
  order_count: number;
  /** 净收入 = 成交价 - 佣金;前台角色被后端裁剪为 null */
  net_revenue: number | null;
}

export interface ChannelShareItem {
  channel: string;
  value: number;
  pct: number;
}

export function buildChannelShare(
  rows: ChannelRow[],
  metric: ChannelMetric
): { items: ChannelShareItem[]; total: number } {
  const raw = rows.map((r) => ({
    channel: r.channel,
    value: metric === "orders" ? r.order_count || 0 : r.net_revenue || 0,
  }));
  const total = raw.reduce((s, r) => s + r.value, 0);
  const items = raw.map((r) => ({
    ...r,
    pct: total > 0 ? (r.value / total) * 100 : 0,
  }));
  return { items, total };
}
