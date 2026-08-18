import { CHANNEL_LABELS } from "@/lib/channels";
import type { OrderListItem } from "@/lib/types";

/**
 * 「见过的订单」上限。必须远大于 GET /orders 的 page_size(20)——否则一条仍停留在
 * 第一页的老单会被淘汰出集合，下一轮就被当成新单重新弹一次（随机幽灵响声，极难查）。
 */
/** 「见过的订单」条目：判新只看 order_id，created_at 仅用于裁剪排序。 */
export type SeenItem = { order_id: string; created_at: string };

export const SEEN_LIMIT = 200;

/** 单轮新单超过这个数就合并成一条 toast，只响一声（断网恢复后不连击）。 */
export const MERGE_THRESHOLD = 3;

/**
 * 是否为该角色监听新订单。
 * 只给 operator（前台）——王总(admin)/财务(finance) 不该被订单叮。
 * 注意：admin 因此无法自行验收本功能，必须用前台账号验。
 */
export function shouldWatchNewOrders(role: string | undefined): boolean {
  return role === "operator";
}

/**
 * 挑出没见过的订单。
 * 按 order_id 判新，不按时间戳——同一秒进两单、慢事务写入过去的时间戳，都不会漏。
 */
export function diffNewOrders(
  seen: ReadonlyMap<string, string>,
  items: OrderListItem[],
): OrderListItem[] {
  return items.filter((o) => !seen.has(o.order_id));
}

/**
 * 并入新条目并按上限裁剪，返回**新 Map**（不改动入参：zustand 要整体替换，
 * 原地 mutate 不触发订阅更新）。
 * 裁剪按 created_at 最旧的先淘汰——插入顺序在「老单因取消掉页、又被顶回来」时
 * 与时间顺序不一致，按插入顺序会淘汰错对象。
 *
 * ⚠️ tsconfig target=es5 且未开 downlevelIteration：不能 spread/for...of 遍历 Map。
 */
export function mergeSeen(
  seen: ReadonlyMap<string, string>,
  items: SeenItem[],
  limit: number = SEEN_LIMIT,
): Map<string, string> {
  const next = new Map<string, string>();
  seen.forEach((createdAt, id) => next.set(id, createdAt));
  items.forEach((o) => next.set(o.order_id, o.created_at));

  if (next.size <= limit) return next;

  const entries = Array.from(next.entries());
  entries.sort((a, b) => (a[1] < b[1] ? 1 : a[1] > b[1] ? -1 : 0)); // created_at 降序
  const kept = new Map<string, string>();
  entries.slice(0, limit).forEach(([id, createdAt]) => kept.set(id, createdAt));
  return kept;
}

/** "2026-07-20" → "07-20"。toast 空间小，年份没信息量。 */
function shortDate(iso: string): string {
  return iso.slice(5);
}

/**
 * 单条新订单的 toast 文案。
 * 字段限于 OrderListItem 所有——**没有房型**，想显示得多查一次接口，不值得。
 */
export function formatOrderToast(o: OrderListItem): { message: string; description: string } {
  const channel = CHANNEL_LABELS[o.channel] ?? o.channel;
  const rooms = o.room_ids.length > 0 ? o.room_ids.join("、") : "待排房";
  return {
    message: `新订单 · ${channel} · ${o.guest_name}`,
    description: `${shortDate(o.check_in_date)} 至 ${shortDate(o.check_out_date)} · ${rooms}`,
  };
}

/** 一轮进来 >MERGE_THRESHOLD 单时的合并文案（断网恢复后不连击）。 */
export function formatMergedToast(count: number): { message: string; description: string } {
  return {
    message: `${count} 个新订单`,
    description: "点击查看订单列表",
  };
}
