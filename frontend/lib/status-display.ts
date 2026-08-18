/** 订单/房态展示常量的单一来源。
 *
 *  历史上 STATUS_COLOR 在 GanttView / MobileGantt / OwnerGanttView 各抄一份，
 *  ROOM_STATUS_COLOR/LABEL 在 OrderRoomsField / EditOrderModal / AssignRoomModal /
 *  TransferRoomModal 各抄一份，新增状态要改 7+ 处。收敛到本模块后组件一律 import。
 *
 *  甘特彩虹状态色是王总拍板的正式功能色（设计系统单色例外），改色值前先对齐业务。
 *
 *  有意未收编的变体（各有独立语境，勿盲目合并）：
 *  - GlobalSearch / owner/rooms/[id] / booking/orders / guests：antd Tag 色名版
 *    订单状态映射，各页色值选择不同（待统一设计裁决）
 *  - TodayRoomList：rescheduled=「重新排房」、OwnerDailyList：子集——rescheduled
 *    全库有「改期中/已改期/重新排房」三种文案，需王总拍板后统一（#183 评审记录）
 *  - staff/keeper·cleaner：角色端设计 token 色/任务状态，独立体系
 *  - components/rooms/constants.ts ROOM_STATUS：房态网格视图带 bg 色变体 */

/** 甘特条按订单状态的填充色（hex）。 */
export const ORDER_STATUS_HEX: Record<string, string> = {
  pending_confirm: "#F59E0B",
  pending_payment: "#F59E0B",
  paid_pending_room: "#6366F1",
  roomed_pending_checkin: "#3B82F6",
  checked_in: "#10B981",
  pending_checkout: "#F59E0B",
  completed: "#6B7280",
  cancelled: "#D4D4D8",
};

/** 完整状态名（含 RoomBlock 的 "block:xxx" 前缀键）。
 *  注意 pending_payment 语义已反转为「退房后待完成」（2026-06 订单流重排）。 */
export const ORDER_STATUS_LABEL: Record<string, string> = {
  pending_confirm: "待确认",
  paid_pending_room: "待排房",
  roomed_pending_checkin: "待入住",
  checked_in: "在住",
  pending_checkout: "已退房待收款",
  pending_payment: "待完成",
  completed: "已完成",
  cancelled: "已取消",
  rescheduled: "改期中",
  abnormal: "异常",
  "block:maintenance": "维修",
  "block:owner_use": "业主自用",
  "block:reserved": "预留",
  "block:other": "屏蔽",
};

/** 订单状态 → antd Tag 预设色名（列表/搜索/业主端等徽标的单一来源）。
 *  历史上 booking/orders、owner/rooms/[id]、GlobalSearch 各抄一份且色值分叉
 *  （#批6 收敛：以 booking/owner 多数派为规范，GlobalSearch 归一至此）。
 *  注意与 ORDER_STATUS_HEX（甘特彩虹专用 hex）是两套体系，勿混用。
 *  未收编：TodayRoomList 的 STATUS_COLOR 绑 rescheduled=「重新排房」文案，
 *  属 #183 待王总拍板项，先不动。 */
export const ORDER_STATUS_TAG_COLOR: Record<string, string> = {
  pending_confirm: "orange",
  paid_pending_room: "blue",
  roomed_pending_checkin: "blue",
  checked_in: "cyan",
  pending_checkout: "cyan",
  pending_payment: "geekblue",
  completed: "green",
  cancelled: "default",
  rescheduled: "default",
  abnormal: "red",
};

/** 订单状态 → Tag 色，未知/缺失回退 default（antd Tag 认得，渲染成灰）。 */
export function orderStatusTagColor(s: string | undefined): string {
  if (!s) return "default";
  return ORDER_STATUS_TAG_COLOR[s] || "default";
}

/** 渠道模式下条内的「状态」短标：单色半透明，做「宾」不抢渠道色的戏。
 *  cancelled 不出标（整条置灰+划掉已表达）；未知态不出标。 */
export const ORDER_BAR_STATUS_TAG: Record<string, string> = {
  pending_confirm: "待确认",
  pending_payment: "待完成",
  paid_pending_room: "待排房",
  roomed_pending_checkin: "待入住",
  checked_in: "在住",
  pending_checkout: "待退房",
  completed: "已完成",
};

/** 锁房类型名（不带 block: 前缀的场景，如锁房弹窗/图例）。 */
export const BLOCK_LABEL: Record<string, string> = {
  maintenance: "维修",
  owner_use: "业主自用",
  reserved: "预留",
  other: "其他",
};

/** RoomBlock 条统一灰色。 */
export const BLOCK_BAR_HEX = "#9CA3AF";

/** 房态徽标：antd Tag 色名。 */
export const ROOM_STATUS_COLOR: Record<string, string> = {
  available: "green",
  occupied: "red",
  reserved: "purple",
  pending_clean: "orange",
  cleaning: "gold",
  maintenance: "default",
  locked: "default",
};

/** 房态徽标：中文名。 */
export const ROOM_STATUS_LABEL: Record<string, string> = {
  available: "空置",
  occupied: "在住",
  reserved: "已预订",
  pending_clean: "待清扫",
  cleaning: "清扫中",
  maintenance: "维修",
  locked: "锁房",
};

/** 甘特条填充色：block: 前缀统一灰，未知状态回退调用方 fallback
 *  （后台用 brand token、业主/移动端用 anyu token，故 fallback 由调用方传入）。 */
export function orderStatusHex(s: string | undefined, fallback: string): string {
  if (!s) return fallback;
  if (s.startsWith("block:")) return BLOCK_BAR_HEX;
  return ORDER_STATUS_HEX[s] || fallback;
}
