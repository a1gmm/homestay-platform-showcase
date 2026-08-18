/**
 * 总账号（is_master）分层展示用的纯分组/小计工具。
 *
 * 场景：总账号下每个子业主 = 一层楼（如"16层"/"14层"）。月报/结算列表
 * 在 is_master 时按 owner_name 分组展示，且每组给出小计。非 master 账号的
 * 调用方不会调用本工具，直接走原有的不分组渲染路径。
 */

export interface OwnerGroup<T> {
  ownerName: string;
  items: T[];
}

const UNASSIGNED_OWNER_LABEL = "未分组";

/**
 * 按 owner_name 分组，保留数据首次出现的分组顺序（不额外排序，交给调用方决定展示顺序）。
 *
 * @param items 任意带 owner_name 字段的数组（RoomMonthlyStat / SettlementBrief）
 * @param getOwnerName 从每项里取 owner_name（允许空字符串/undefined）
 */
export function groupByOwnerName<T>(
  items: T[],
  getOwnerName: (item: T) => string | null | undefined,
): OwnerGroup<T>[] {
  const order: string[] = [];
  const buckets = new Map<string, T[]>();
  for (const it of items) {
    const key = getOwnerName(it)?.trim() || UNASSIGNED_OWNER_LABEL;
    if (!buckets.has(key)) {
      buckets.set(key, []);
      order.push(key);
    }
    buckets.get(key)!.push(it);
  }
  return order.map((ownerName) => ({ ownerName, items: buckets.get(ownerName)! }));
}

/**
 * 对一组数值字段求和（字符串数字/number 都兼容，NaN 按 0 处理）。
 * 用于每层小计（revenue / owner_net 等 Decimal 字段后端序列化为字符串）。
 */
export function sumField<T>(items: T[], getValue: (item: T) => string | number | null | undefined): number {
  return items.reduce((acc, it) => {
    const raw = getValue(it);
    const v = typeof raw === "string" ? parseFloat(raw) : raw ?? 0;
    return acc + (Number.isNaN(v as number) ? 0 : (v as number));
  }, 0);
}
