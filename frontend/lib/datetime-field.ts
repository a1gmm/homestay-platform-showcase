import type { Dayjs } from "dayjs";

// 移动端「日期 + 时间」双控件 → 单一 dayjs 值的合并逻辑。
// 背景：单 DatePicker+showTime 的弹层（日期面板 288px + 时间列）在 375px 视口
// 必然溢出（bottomRight 对齐时左缘 -168px，左侧日历滚不到）；居中则两边同时裁。
// 移动端拆成两个控件后各自弹层都放得下，这里负责把两次选择合回一个值。

// 选了新日期：保留原值的时分；原值为空用 fallback（调用方传 dayjs()）的时分。
// 清空日期 = 清空整个值。
export function mergePickedDate(
  prev: Dayjs | null | undefined,
  picked: Dayjs | null,
  fallback: Dayjs
): Dayjs | null {
  if (!picked) return null;
  const base = prev ?? fallback;
  return picked.hour(base.hour()).minute(base.minute()).second(0).millisecond(0);
}

// 选了新时间：保留原值的年月日；原值为空用 fallback 的年月日。
// 时间控件禁清空，picked=null 只是防御路径：有日期则时分归零，全空返回 null。
export function mergePickedTime(
  prev: Dayjs | null | undefined,
  picked: Dayjs | null,
  fallback: Dayjs
): Dayjs | null {
  if (!picked) {
    if (!prev) return null;
    return prev.hour(0).minute(0).second(0).millisecond(0);
  }
  const base = prev ?? fallback;
  return base.hour(picked.hour()).minute(picked.minute()).second(0).millisecond(0);
}
