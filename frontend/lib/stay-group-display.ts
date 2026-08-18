import type { StaySegment } from "./types";

// 续住组段数的展示口径（2026-07-17 拍板）：
// 「续住 N 段」/「共 N 段」按**非取消段**计——后端 group_view 的 nights/total_amount
// 本就排除取消段，段数含取消段会自相矛盾（标 3 段、金额只有 2 段的）。
// 组内确有取消段时注明「（另 M 段已取消）」，别静默吞掉：做账要能看见它存在过，
// 分段明细里取消段也仍然逐行列出（带「已取消」标）。
export function countSegments(segments?: StaySegment[] | null): {
  active: number;
  cancelled: number;
  total: number;
} {
  const all = segments ?? [];
  const active = all.filter((s) => s.order_status !== "cancelled").length;
  return { active, cancelled: all.length - active, total: all.length };
}

/** "2 段" / "2 段（另 1 段已取消）" —— 列表 Tag 与详情页「共 N 段」共用同一份口径 */
export function segmentCountLabel(segments?: StaySegment[] | null): string {
  const { active, cancelled } = countSegments(segments);
  return `${active} 段${cancelled > 0 ? `（另 ${cancelled} 段已取消）` : ""}`;
}
