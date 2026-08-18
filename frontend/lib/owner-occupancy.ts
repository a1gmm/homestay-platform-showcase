// 业主门户「入住率」口径纯函数（从 app/owner/page.tsx 抽出，便于单测——
// page.tsx 只允许默认导出，具名导出会炸 Next build，故口径逻辑单独放这里）。
//
// 入住率 = 已入住晚数 / (房间数 × 有效天数)。
//   分子：作用域内所有房间当月每个订单 (退房日−入住日) 的天数之和（不 clip 跨月，既有近似）。
//   分母：房间数 × 有效天数。有效天数见 occupancyDenomDays——当前月对齐到「本月已过天数」，
//         历史月用整月天数。
import type { MonthlySummary } from "@/lib/owner-api";
import type { YearMonth } from "@/lib/owner-trend";

// 一个月的总天数。注意 ym.month 是 1-based（7 = 七月）：new Date(year, month, 0)
// 取的是「month(0-based) 月的第 0 天」= 目标月最后一天，正好利用 1-based 偏移。
export function daysInMonth(ym: YearMonth): number {
  return new Date(ym.year, ym.month, 0).getDate();
}

// 当月已入住晚数合计（KPI + 入住率分子共用）。
export function totalOccupancyNights(data: MonthlySummary): number {
  return data.rooms.reduce((acc, r) => acc + (r.occupancy_nights || 0), 0);
}

// 入住率分母的「有效天数」。
//   当前月：后端返回 as_of_date（=北京时间昨天 cutoff），此时分子只累计到该日已退房的
//           订单，分母也对齐到「本月已过天数」（as_of_date 的日号），避免月中用满月分母
//           把入住率算虚低——月底所有单退房后自然收敛到整月。
//   历史月：as_of_date 为 null → 用整月天数。
//   防御：as_of_date 与所选月份不一致（后端边界）时，退回整月天数。
export function occupancyDenomDays(data: MonthlySummary, ym: YearMonth): number {
  const full = daysInMonth(ym);
  const asOf = data.as_of_date;
  if (asOf) {
    const [y, m, d] = asOf.split("-").map(Number);
    if (y === ym.year && m === ym.month && d >= 1) {
      return Math.min(d, full);
    }
  }
  return full;
}

// 入住率百分比，1 位小数。denom ≤ 0 或无房时回退 0。
export function deriveOccupancyRate(data: MonthlySummary, ym: YearMonth): number {
  if (!data.rooms_total) return 0;
  const denom = data.rooms_total * occupancyDenomDays(data, ym);
  if (denom <= 0) return 0;
  return Math.round((totalOccupancyNights(data) / denom) * 1000) / 10;
}

// 环形副标题：「已入住晚数 / 可入住晚数 晚」。
export function occupancySubtitle(data: MonthlySummary, ym: YearMonth): string {
  const denom = data.rooms_total * occupancyDenomDays(data, ym);
  return `${totalOccupancyNights(data)} / ${denom} 晚`;
}
