import dayjs from "dayjs";

// 锁房日期语义：UI 全包含（员工选到哪天就锁到哪天晚上），后端/DB 存排他
// end_date（= 解锁日）。提交 +1 天换算，显示 -1 天还原。
// 背景：2026-06-12 生产事故 —— 同日范围被后端 422 拒绝且报错渲染失败。

export const NO_END_DATE = "2099-12-31";

export function resolveBlockEndDate(
  startDate: string,
  endDate: string | null,
  noEnd: boolean
): string | null {
  if (noEnd) return NO_END_DATE;
  if (!endDate) return null;
  return dayjs(endDate).add(1, "day").format("YYYY-MM-DD");
}

export function formatBlockRange(startDate: string, endDate: string): string {
  if (endDate === NO_END_DATE) return `${startDate} 起 · 无截止`;
  const lastNight = dayjs(endDate).subtract(1, "day");
  const nights = lastNight.diff(dayjs(startDate), "day") + 1;
  return `${startDate} 至 ${lastNight.format("YYYY-MM-DD")}（${nights}晚）`;
}
