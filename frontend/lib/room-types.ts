/**
 * 房型展示顺序（王总 2026-05-26 微信确认）。
 *
 * 用于:房态甘特图 / 业主端房间列表 — 同一份顺序常量，避免前台多处重复维护。
 *
 * 规则：
 *   1. 在 ROOM_TYPE_ORDER 里出现的房型按数组顺序排
 *   2. 不在列表里的房型按字母序追加在后面
 *   3. "未分组"（room_type 为 null/空）永远放最后（兜底"测试房放最下面"语义）
 */
export const ROOM_TYPE_ORDER: readonly string[] = [
  "精选市景·大床度假套房（复式楼王+电视投屏）",
  "侧面海景·亲子两床套房（复式楼王+电视投屏）",
  "私享180°海景·大床套房（复式楼王+电视投屏）",
  "尊享270°海景·两卧度假套房（复式楼王+弧形内阳台+电视投屏）",
];

export const UNGROUPED_LABEL = "未分组";

/**
 * 把一组带 room_type 的对象按 ROOM_TYPE_ORDER 分组并排序。
 *
 * @param items 任意带 room_type 字段的数组
 * @param getRoomType 从每项里取 room_type（允许 null/undefined）
 * @param sortInGroup 可选：组内排序函数（如"本月有入住的置顶"）
 */
export function groupByRoomType<T>(
  items: T[],
  getRoomType: (item: T) => string | null | undefined,
  sortInGroup?: (a: T, b: T) => number,
): Array<{ groupName: string; items: T[] }> {
  if (items.length === 0) return [];
  const buckets = new Map<string, T[]>();
  for (const it of items) {
    const key = getRoomType(it) || UNGROUPED_LABEL;
    if (!buckets.has(key)) buckets.set(key, []);
    buckets.get(key)!.push(it);
  }

  const ordered: string[] = [];
  for (const name of ROOM_TYPE_ORDER) {
    if (buckets.has(name)) ordered.push(name);
  }
  const rest = Array.from(buckets.keys())
    .filter((k) => k !== UNGROUPED_LABEL && !ROOM_TYPE_ORDER.includes(k))
    .sort();
  ordered.push(...rest);
  if (buckets.has(UNGROUPED_LABEL)) ordered.push(UNGROUPED_LABEL);

  return ordered.map((groupName) => {
    const items = buckets.get(groupName) || [];
    return {
      groupName,
      items: sortInGroup ? [...items].sort(sortInGroup) : items,
    };
  });
}
