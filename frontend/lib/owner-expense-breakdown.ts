import type { ServiceFeeRates, SettlementItemDetail } from "@/lib/owner-api";
import {
  EXPENSE_CATEGORY_LABEL,
  LEGACY_EXPENSE_CATEGORY_LABEL,
  type OwnerCostShareCategory,
} from "@/lib/cost-share-constants";

export type { ServiceFeeRates };

/** 支出明细的一行。 */
export interface ExpenseLineItem {
  /** 稳定的 React key */
  key: string;
  /** 展示标签。用业主每月收到的 Excel 对账表的词：保洁/续住保洁/日耗/洗涤。 */
  label: string;
  /** "¥65 × 85次"（匹配上）或 "5笔"（降级）。 */
  detail: string | null;
  /** 该行业主实际承担合计 */
  ownerAmount: number;
}

type Line = NonNullable<SettlementItemDetail["cost_share_breakdown"]>[number];

export const round2 = (n: number) => Math.round(n * 100) / 100;

/** 元 → 整数分。金额比较/整除一律走这里，别拿浮点直接取模。 */
const cents = (n: number) => Math.round(n * 100);

/** 费率组：一个「¥单价 × N单位」的展示口径。 */
interface RateGroup {
  key: string;
  label: string;
  unit: string;
  rateCents: number;
  /** exact = 多费率类目按精确相等匹配；divisible = 单费率类目按整除反推数量 */
  mode: "exact" | "divisible";
}

function positiveRate(raw: string | undefined): number | null {
  if (raw === undefined || raw === null || raw === "") return null;
  const v = parseFloat(raw);
  return Number.isFinite(v) && v > 0 ? v : null;
}

/**
 * 按类目给出费率组。标签用业主 Excel 对账表的词（王总版式）。
 *
 * 保洁有**两个**费率（退房/续住）→ 必须精确相等匹配：用整除会把一笔 ¥60 的保洁
 * 误报成「续住保洁 ¥30 × 2次」。洗涤/日耗只有**一个**费率，金额多档只是数量不同
 * （¥44 = 22×2晚）→ 整除反推数量。
 */
function ratePlan(rates?: Partial<ServiceFeeRates> | null): Map<string, RateGroup[]> {
  const plan = new Map<string, RateGroup[]>();
  if (!rates) return plan;

  const checkout = positiveRate(rates.checkout_cleaning_fee);
  const instay = positiveRate(rates.instay_cleaning_fee);
  const cleaning: RateGroup[] = [];
  if (checkout !== null) {
    cleaning.push({ key: "cleaning:checkout", label: "保洁", unit: "次", rateCents: cents(checkout), mode: "exact" });
  }
  // 两个费率配成同一个值时只留前者：否则后者永不命中，白占一个组。
  if (instay !== null && (checkout === null || cents(instay) !== cents(checkout))) {
    cleaning.push({ key: "cleaning:instay", label: "续住保洁", unit: "次", rateCents: cents(instay), mode: "exact" });
  }
  if (cleaning.length > 0) plan.set("cleaning", cleaning);

  const laundry = positiveRate(rates.laundry_fee_per_room);
  if (laundry !== null) {
    plan.set("laundry", [{ key: "laundry", label: "洗涤", unit: "床位次", rateCents: cents(laundry), mode: "divisible" }]);
  }

  const consumable = positiveRate(rates.consumable_fee_per_room_night);
  if (consumable !== null) {
    plan.set("daily_supplies", [{ key: "daily_supplies", label: "日耗", unit: "间夜", rateCents: cents(consumable), mode: "divisible" }]);
  }

  return plan;
}

function labelFor(category: string): string {
  return (
    EXPENSE_CATEGORY_LABEL[category as OwnerCostShareCategory] ??
    LEGACY_EXPENSE_CATEGORY_LABEL[category] ??
    category
  );
}

/** 12.5 → "12.5"，65 → "65"（不显示无意义的 .00） */
function rateText(rateCents: number): string {
  return String(rateCents / 100);
}

interface MatchedAcc { label: string; unit: string; rateCents: number; qty: number; ownerCents: number }
interface DegradedAcc { count: number; ownerCents: number }

function foldLines(lines: Line[], rates?: Partial<ServiceFeeRates> | null): ExpenseLineItem[] {
  const plan = ratePlan(rates);
  const matched = new Map<string, MatchedAcc>();
  const degraded = new Map<string, DegradedAcc>();

  lines.forEach((l) => {
    // 平台补贴行没有 owner_amount（它是收入加项，不是业主支出）——在此被忽略，
    // 这正是「支出卡合计 == 概览卡扣除支出」这条不变量成立的原因。
    const owner = parseFloat((l as { owner_amount?: string }).owner_amount ?? "");
    if (!Number.isFinite(owner)) return;
    const ownerCents = cents(owner);

    const groups = plan.get(l.category);
    const amount = parseFloat(l.amount);
    const share = parseFloat(l.share_percent);
    // 只有业主全担(share=1)的行才做费率匹配。否则 amount=65/share=0.5 会显示成
    // 「保洁 ¥65 × 1次 = ¥32.5」——单价乘数量对不上金额，自相矛盾。
    const matchable =
      groups !== undefined &&
      Number.isFinite(amount) &&
      Number.isFinite(share) &&
      Math.round(share * 1000) === 1000;

    if (matchable) {
      const amountCents = cents(amount);
      for (const g of groups!) {
        const hit =
          g.mode === "exact"
            ? amountCents === g.rateCents
            : amountCents > 0 && amountCents % g.rateCents === 0;
        if (!hit) continue;
        const acc = matched.get(g.key) ?? { label: g.label, unit: g.unit, rateCents: g.rateCents, qty: 0, ownerCents: 0 };
        acc.qty += g.mode === "exact" ? 1 : amountCents / g.rateCents;
        acc.ownerCents += ownerCents;
        matched.set(g.key, acc);
        return;
      }
    }

    const acc = degraded.get(l.category) ?? { count: 0, ownerCents: 0 };
    acc.count += 1;
    acc.ownerCents += ownerCents;
    degraded.set(l.category, acc);
  });

  const out: ExpenseLineItem[] = [];
  // Map 不能用 for...of 遍历（tsconfig target es5 → TS2802），必须 forEach。
  matched.forEach((acc, key) => {
    out.push({
      key,
      label: acc.label,
      detail: `¥${rateText(acc.rateCents)} × ${acc.qty}${acc.unit}`,
      ownerAmount: round2(acc.ownerCents / 100),
    });
  });
  degraded.forEach((acc, category) => {
    // 该类目配了费率 → 匹配行会用「保洁」这种词，降级行必须加后缀，否则一张卡上
    // 两行都叫「保洁」，业主不知道差别在哪。未知类目不会撞名，用原文即可。
    const base = labelFor(category);
    out.push({
      key: `other:${category}`,
      label: plan.has(category) ? `${base} · 其他` : base,
      detail: `${acc.count}笔`,
      ownerAmount: round2(acc.ownerCents / 100),
    });
  });
  out.sort((a, b) => b.ownerAmount - a.ownerAmount);
  return out;
}

export function aggregateRoomBreakdown(
  item: SettlementItemDetail,
  rates?: Partial<ServiceFeeRates> | null,
): ExpenseLineItem[] {
  return foldLines(item.cost_share_breakdown ?? [], rates);
}

export function aggregateBreakdown(
  items: SettlementItemDetail[],
  rates?: Partial<ServiceFeeRates> | null,
): ExpenseLineItem[] {
  return foldLines(
    items.flatMap((i) => i.cost_share_breakdown ?? []),
    rates,
  );
}
