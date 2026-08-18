// 房态甘特图「滚动日期窗口」纯逻辑（D1）。
// 与自然月解耦：窗口由 起始日 + 天数 描述，天然可跨月。
// 全部纯函数，便于单测（见 lib/__tests__/gantt-window.test.ts）。
//
// 日期一律用本地 YYYY-MM-DD 字符串，避免 toISOString 的 UTC 跨时区偏一天。

/**
 * 甘特图默认往前回看的天数：起始日 = 今天 - LOOKBACK，
 * 让今天落在第 3 列，左侧能看到最近两天（含今天要退房的房间）。
 */
export const GANTT_LOOKBACK_DAYS = 2;

/** 把 Date 安全格式化成本地 YYYY-MM-DD（不走 toISOString）。 */
export function ymdLocal(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

/** 解析 YYYY-MM-DD 为本地午夜 Date（避免被当成 UTC）。 */
export function parseYmd(s: string): Date {
  const [y, m, d] = s.split("-").map(Number);
  return new Date(y, m - 1, d);
}

/** 起始日 + 天数 → 连续 YYYY-MM-DD 数组（含起始日，长度 = rangeDays）。 */
export function buildWindowDays(startDate: string, rangeDays: number): string[] {
  const start = parseYmd(startDate);
  const n = Math.max(1, Math.floor(rangeDays));
  return Array.from({ length: n }, (_, i) => {
    const d = new Date(start.getFullYear(), start.getMonth(), start.getDate() + i);
    return ymdLocal(d);
  });
}

/** 把起始日整段平移 offsetDays 天（左右翻页用，offset 可负）。返回新的起始日字符串。 */
export function shiftWindow(startDate: string, offsetDays: number): string {
  const d = parseYmd(startDate);
  return ymdLocal(new Date(d.getFullYear(), d.getMonth(), d.getDate() + offsetDays));
}

/** 窗口的人类可读区间标签：04.20 → 05.19（跨月天然显示）。 */
export function windowRangeLabel(startDate: string, rangeDays: number): string {
  const days = buildWindowDays(startDate, rangeDays);
  const first = parseYmd(days[0]);
  const last = parseYmd(days[days.length - 1]);
  const fmt = (d: Date) =>
    `${String(d.getMonth() + 1).padStart(2, "0")}.${String(d.getDate()).padStart(2, "0")}`;
  return `${fmt(first)} → ${fmt(last)}`;
}

/**
 * 窗口锚点所在的年月：= (windowStart + GANTT_LOOKBACK_DAYS) 的年月。
 * 移动端甘特（按自然月渲染）用它跟随桌面滚动窗口的位置，
 * 这样跨 1024px 断点（缩放窗口）时不会丢失用户在桌面选的日期。
 *
 * 为什么加 LOOKBACK 而不直接取 windowStart 的月份：windowStart = 焦点日 - LOOKBACK，
 * 月初（如 7-1，windowStart=6-29）直接取会错一个月，把移动端拉回上个月。
 */
export function windowFocusMonth(startDate: string): { year: number; month: number } {
  const anchor = parseYmd(shiftWindow(startDate, GANTT_LOOKBACK_DAYS));
  return { year: anchor.getFullYear(), month: anchor.getMonth() + 1 };
}

/** 某日是否在窗口内（含端点）。 */
export function isDateInWindow(date: string, startDate: string, rangeDays: number): boolean {
  const days = buildWindowDays(startDate, rangeDays);
  return date >= days[0] && date <= days[days.length - 1];
}

export interface DayOccupancy {
  /** 该日已占用房间数 */
  used: number;
  /** 房间总数 */
  total: number;
  /** 剩余空房数 */
  free: number;
  /** 入住率 0-100（整数百分比；total=0 时为 0） */
  rate: number;
}

/**
 * 计算某一日的占用：在 days[date] 上有任意 booking 即算占用。
 * occupiedByDate: { roomId: Set<date> } 或更通用地传 每房的 days 映射。
 * 这里直接接收「每房在该日是否占用」的判定函数，保持纯粹。
 */
export function computeDayOccupancy(
  date: string,
  rooms: Array<{ days?: Record<string, unknown> }>
): DayOccupancy {
  const total = rooms.length;
  let used = 0;
  for (const r of rooms) {
    if (r.days && r.days[date] != null) used += 1;
  }
  const free = total - used;
  const rate = total > 0 ? Math.round((used / total) * 100) : 0;
  return { used, total, free, rate };
}

/** 批量算整个窗口每日占用，返回 { date: DayOccupancy }。 */
export function computeWindowOccupancy(
  days: string[],
  rooms: Array<{ days?: Record<string, unknown> }>
): Record<string, DayOccupancy> {
  const m: Record<string, DayOccupancy> = {};
  for (const d of days) m[d] = computeDayOccupancy(d, rooms);
  return m;
}
