// 业主首页「月份切换 + 收益趋势」用的纯月份运算。
// 与后端解耦：趋势图不新增接口，前端对最近 N 个月各拉一次 /owner/me/summary
// （TanStack Query 各月独立缓存），本文件只负责算「哪几个月」和位移边界。
// month 一律用人类口径 1-12，不用 JS Date 的 0-11，避免调用点反复 ±1 出错。

export interface YearMonth {
  year: number;
  month: number; // 1-12
}

// 把 {year, month} 压成可比较/可加减的月序号（内部用，不外泄 0-based）。
function toIndex(ym: YearMonth): number {
  return ym.year * 12 + (ym.month - 1);
}

function fromIndex(idx: number): YearMonth {
  return { year: Math.floor(idx / 12), month: (idx % 12) + 1 };
}

// 以 base 为基准平移 delta 个月（可正可负），正确跨年。
export function shiftMonth(base: YearMonth, delta: number): YearMonth {
  return fromIndex(toIndex(base) + delta);
}

export function isSameMonth(a: YearMonth, b: YearMonth): boolean {
  return a.year === b.year && a.month === b.month;
}

// target 是否晚于 current（业主看不了「未来」月份的收益，用来禁用「下一月」）。
export function isFutureMonth(target: YearMonth, current: YearMonth): boolean {
  return toIndex(target) > toIndex(current);
}

// 返回以 end 结尾、含 end 在内、共 n 个连续月，按时间升序（旧→新）。
// 趋势图从左到右就是时间顺序，最右一根即当前选中月。
export function lastNMonths(end: YearMonth, n: number): YearMonth[] {
  const out: YearMonth[] = [];
  for (let i = n - 1; i >= 0; i--) {
    out.push(shiftMonth(end, -i));
  }
  return out;
}

// 柱状图高度百分比：value 相对 max 归一化。
// 有值但极小的月份至少给 4% 高度，保证肉眼可见「这个月有收益但不多」；
// 真正为 0 的月份返回 0，视觉上就是空槽。
export function barHeightPct(value: number, max: number): number {
  if (max <= 0 || value <= 0) return 0;
  const pct = (value / max) * 100;
  return Math.max(4, Math.round(pct));
}
