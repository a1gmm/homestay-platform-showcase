import type { StayGroup } from "./types";

/**
 * 批量操作前把勾中的段 key（anchor_order_id）解析成要提交的订单 id 集合。
 *
 * 一段 = 前台眼里的一个订单 → 展开为该段**全部活单**（已取消段不碰），
 * 不能只动锚单——续住组的其余段会静默留在原状态。
 *
 * key 在当前页 rows 里找不到（勾选后翻页/换筛选的残留）→ **跳过并计入 staleKeys**，
 * 由调用方提示「有 N 个选中项因翻页失效未处理」。旧代码 fallback [k] 只发锚单且
 * 不吭声，是双重静默错误。页面层另在翻页/筛选变更时清空勾选，双保险。
 */
export function resolveBatchIds(
  selectedKeys: string[],
  rows: StayGroup[],
): { ids: string[]; staleKeys: string[] } {
  const ids: string[] = [];
  const staleKeys: string[] = [];
  for (const k of selectedKeys) {
    const row = rows.find((r) => r.anchor_order_id === k);
    if (!row) {
      staleKeys.push(k);
      continue;
    }
    for (const s of row.segments ?? []) {
      if (s.order_status !== "cancelled") ids.push(s.order_id);
    }
  }
  return { ids, staleKeys };
}
