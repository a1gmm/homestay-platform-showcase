import type { RoomOut } from "@/lib/types";

export interface AssignableRoom {
  room: RoomOut;
  /** 可排给本单（在后端按日期算出的可用集合里，且不是当前房） */
  assignable: boolean;
  /** 换房时的当前所在房——显示但不可排给自己 */
  isCurrent: boolean;
}

/**
 * 把「后端按订单日期算出的可排 room_id 集合」映射成排房选择器要展示的房列表。
 *
 * 与旧 AssignRoomModal「列出全部房、冲突留给后端拒」不同：这里前台只看得到
 * 那几天真正能排的房，选了就不会被后端 409 打回。
 *
 * - 只保留 availableIds 里的房（availableIds 之外的房日期冲突/维修/锁房，不展示）
 * - 换房场景 currentRoomId：当前房本身对这单不算冲突，后端 availableIds 会排除它，
 *   这里补进来并标 isCurrent（供 UI 显示「当前」灰态），assignable=false 不可排给自己
 * - 排序：空置(available)优先 → base_price 升序 → room_id 升序
 */
export function buildAssignableRooms(
  rooms: RoomOut[],
  availableIds: readonly string[],
  currentRoomId?: string | null,
): AssignableRoom[] {
  const byId = new Map(rooms.map((r) => [r.room_id, r]));
  const ids = new Set(availableIds);

  const out: AssignableRoom[] = [];
  const seen = new Set<string>();
  for (const id of availableIds) {
    if (seen.has(id)) continue; // 去重（后端理论上不重复，防御一下）
    seen.add(id);
    const room = byId.get(id);
    if (!room) continue; // availableIds 里的房已不在房表（极少见），跳过不崩
    out.push({ room, assignable: id !== currentRoomId, isCurrent: id === currentRoomId });
  }

  // 当前房不在可排集合里时补进来（换房常态：当前房被 availableIds 排除）
  if (currentRoomId && !ids.has(currentRoomId)) {
    const room = byId.get(currentRoomId);
    if (room) out.push({ room, assignable: false, isCurrent: true });
  }

  const price = (r: RoomOut) => Number(r.base_price) || 0;
  out.sort((a, b) => {
    const aAvail = a.room.room_status === "available";
    const bAvail = b.room.room_status === "available";
    if (aAvail !== bAvail) return aAvail ? -1 : 1;
    if (price(a.room) !== price(b.room)) return price(a.room) - price(b.room);
    return a.room.room_id.localeCompare(b.room.room_id);
  });
  return out;
}
