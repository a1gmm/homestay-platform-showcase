export const ROOM_STATUS: Record<string, { color: string; label: string; bg: string }> = {
  available: { color: "success", label: "空置", bg: "#F5F1EA" },
  occupied: { color: "processing", label: "在住", bg: "#F5F1EA" },
  reserved: { color: "purple", label: "已预订", bg: "#F5F1EA" },
  pending_clean: { color: "orange", label: "待清扫", bg: "#FFF3E6" },
  cleaning: { color: "gold", label: "清扫中", bg: "#FFF8DA" },
  maintenance: { color: "warning", label: "维修", bg: "#F0E4D6" },
  locked: { color: "error", label: "锁房", bg: "#F0E4D6" },
};

// 保洁流程态（待清扫 + 清扫中）。甘特网格里合并成一种「保洁中」色，
// 只在今天那一格铺色 —— 王总 2026-07-13 拍板。左列徽章仍按上面两色细分。
export const CLEANING_STATUSES = ["pending_clean", "cleaning"];
export const CLEANING_CELL = { bg: "#F4CE5E", fg: "#6B4E00", label: "🧹 保洁中" };

// 试运营房型角标：后端在备注命中关键词（当前=「极海」）的订单格子上带 trial_tag，
// 房态各视图（日历/单日/移动端）统一用这个金色小标渲染，一眼识别试运营单。
// 文字取后端返回的 trial_tag，试运营结束后端摘掉关键词即自动消失。
export const TRIAL_BADGE = { bg: "#F4CE5E", fg: "#6B4E00" };

// 进入维修/锁房前可能是任意状态。结束维修恢复时，若上一个状态是
// 在住/待清扫/清扫中这类「有客人/临时」态（或为空），统一回退到「空置」，
// 避免把没有客人的房间错误标成在住。否则恢复字面上的上一个状态。
const TRANSIENT_STATUSES = ["occupied", "pending_clean", "cleaning"];

export function restoreTarget(prev?: string | null): string {
  return prev && !TRANSIENT_STATUSES.includes(prev) ? prev : "available";
}

// 哪些状态提供「一键恢复上一个状态」快捷操作（临时屏蔽态）。
export const RESTORABLE_STATUSES = ["maintenance", "locked"];
