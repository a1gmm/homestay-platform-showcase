// 房态页头明细：把各状态桶按固定顺序拼成一行，加总 = 总房间数。
// 之前页头只显示「空置 / 在住」两桶，其余（已预订/待清扫/清扫中/维修/锁房）不显示 →
// 32 套 ≠ 19 空置 + 8 在住，看着像凭空少 5 间。这里全列出来，让数字对得上。
import { ROOM_STATUS } from "@/components/rooms/constants";

// 展示顺序：在住优先、空置垫底，中间按运营关注度。只列 ROOM_STATUS 里有的桶。
const SUMMARY_ORDER = [
  "occupied", "reserved", "pending_clean", "cleaning", "maintenance", "locked", "available",
];

/** 返回如「8 在住 · 4 已预订 · 3 维修 · 17 空置」，只含非零桶。全零/空 → 空串。 */
export function formatRoomStatusSummary(counts: Record<string, number>): string {
  return SUMMARY_ORDER
    .filter((k) => (counts?.[k] ?? 0) > 0)
    .map((k) => `${counts[k]} ${ROOM_STATUS[k]?.label ?? k}`)
    .join(" · ");
}
