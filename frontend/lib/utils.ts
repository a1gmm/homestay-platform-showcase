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

// 结算「到厘」展示：分成/应得优先显示后端到厘的 precise 值（三位小数），
// 旧结算无 precise（null/undefined/NaN）时回退到分（两位）。打款金额仍按分，
// 由后端另算——本函数只管展示。业主端与管理端共用，避免两处口径漂移。
export function fmtMille(
  precise: number | string | null | undefined,
  fallback: number | string | null | undefined,
): string {
  // 两值皆空 = 隐藏金额账号后端置 None（真实 0 走 Decimal 0，不会同时为 null）→「——」。
  if (precise == null && fallback == null) return "——";
  const p = typeof precise === "string" ? parseFloat(precise) : precise;
  if (p != null && !Number.isNaN(p)) {
    return p.toLocaleString("zh-CN", { minimumFractionDigits: 3, maximumFractionDigits: 3 });
  }
  const f = typeof fallback === "string" ? parseFloat(fallback) : fallback;
  const fv = f == null || Number.isNaN(f) ? 0 : f;
  return fv.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

// 结算页统一小字：说明分成到厘、打款按分
export const MILLE_NOTE = "分成精确至厘（小数第三位），实际打款按分四舍五入结算。";

export function downloadBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}
