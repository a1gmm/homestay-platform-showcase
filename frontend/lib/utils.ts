import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/**
 * 当前 CN 时区下的「今天」YYYY-MM-DD。避开 new Date().toISOString().slice(0,10)
 * 在非 UTC+8 浏览器上会偏一天的坑（CN 晚 16:00 之后尤甚）。Intl 现代浏览器原生支持。
 */
export function todayCNString(): string {
  const fmt = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  });
  return fmt.format(new Date()); // en-CA 输出 YYYY-MM-DD
}

export function todayCNYearMonth(): { year: number; month: number } {
  const [y, m] = todayCNString().split("-");
  return { year: Number(y), month: Number(m) };
}

// 2026-06-05 流程调整：确认收款挪到退房后。pending_payment 重定义为后期「待完成」态。
export const ORDER_STATUS_LABELS: Record<string, string> = {
  pending_confirm: "待确认",
  paid_pending_room: "待排房",
  roomed_pending_checkin: "待入住",
  checked_in: "在住",
  pending_checkout: "已退房待收款",
  pending_payment: "待完成",
  completed: "已完成",
  rescheduled: "已改期",
  abnormal: "异常订单",
  cancelled: "已取消",
};

// 渠道字典已统一到 lib/channels.ts，这里 re-export 兼容老 import 路径。
export { CHANNEL_LABELS } from "./channels";

export const PAYMENT_STATUS_LABELS: Record<string, string> = {
  unpaid: "未付款",
  partial: "部分付款",
  paid: "已付清",
  partial_refund: "部分退款",
  full_refund: "全额退款",
};

export function formatCurrency(amount: number | string): string {
  const num = typeof amount === "string" ? parseFloat(amount) : amount;
  return `¥${num.toFixed(2)}`;
}

// 只展示后端逐房取分后汇总的正式金额，不用 *_precise 参考值重算。
export function fmtSettlementAmount(amount: number | string | null | undefined): string {
  if (amount == null || amount === "") return "——";
  const value = Number(amount);
  if (!Number.isFinite(value)) return "——";
  return value.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

export function sumSettlementAmounts(amounts: (number | string | null | undefined)[]): number | null {
  let cents = 0;
  for (const amount of amounts) {
    // 隐藏金额或数据缺失时不展示一个不完整的小计。
    if (amount == null || amount === "" || !Number.isFinite(Number(amount))) return null;
    cents += Math.round(Number(amount) * 100);
  }
  return cents / 100;
}

export const SETTLEMENT_ROUNDING_NOTE = "结算金额按每个房间先取到分，再汇总；业主级费用另行扣除。";

export function downloadBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}
