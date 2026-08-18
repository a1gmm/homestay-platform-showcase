// 已入住但「该收的钱没收」风险判定 —— 给前台的醒目提醒，防漏收房费/押金。
// 纯函数，组件只渲染结果（见 OrderDetailModal）。仅在订单已入住(checked_in)时判定：
//   - 押金未收：任何渠道，押金 > 0 且 deposit_status=not_collected。
//   - 房费未收：仅「直收」渠道（非平台）。平台单（携程/去哪儿/美团等）客人钱付给平台、
//     酒店 payment_status=unpaid 是正常的，不报警，否则每张 OTA 单都误报成噪音。
//     自住/试住等非计费渠道也不报警。
import { PLATFORM_CHANNELS } from "./channels";

export interface CheckinRisk {
  key: "deposit" | "payment";
  label: string;
}

// 非计费渠道（内部住，不产生应收房费）——即使非平台也不报房费风险。
const NON_BILLABLE_CHANNELS = new Set<string>(["self_used", "trial_stay"]);

export function computeCheckinFinancialRisks(order: any): CheckinRisk[] {
  const status = order?.order_status ?? order?.status;
  if (status !== "checked_in") return [];

  const risks: CheckinRisk[] = [];

  // 押金未收（任何渠道通用）
  const deposit = Number(order?.deposit ?? 0);
  const depositStatus = order?.deposit_status ?? "not_collected";
  if (deposit > 0 && depositStatus === "not_collected") {
    risks.push({ key: "deposit", label: `押金 ¥${deposit.toLocaleString()} 未收` });
  }

  // 房费未收（仅直收渠道）
  const channel = order?.channel ?? "";
  const isPlatform = PLATFORM_CHANNELS.has(channel);
  const isNonBillable = NON_BILLABLE_CHANNELS.has(channel);
  const price = Number(order?.actual_price ?? 0);
  const pay = order?.payment_status ?? "unpaid";
  if (!isPlatform && !isNonBillable && price > 0 && (pay === "unpaid" || pay === "partial")) {
    risks.push({ key: "payment", label: pay === "partial" ? "房费未收齐" : "房费未收" });
  }

  return risks;
}
