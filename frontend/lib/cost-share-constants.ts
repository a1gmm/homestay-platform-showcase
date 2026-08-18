/**
 * v2-issue#3 — 业主费用支出占比常量（前端）
 *
 * 跟后端 backend/app/models/expense.py 的 ExpenseCategory + EXPENSE_CATEGORY_LABELS
 * 保持一致。OWNER_COST_SHARE_CATEGORIES 顺序按王总参考 PMS 截图 4。
 */
import type { BookingType } from "./types";

// v2#7 对齐王总《业主分成明细》：15 个占比类目。冷水/热水已合并进"水费(water)"停用，
// 采购费(supplies)启用入列。改此处须同步 backend OWNER_COST_SHARE_CATEGORIES + seed 迁移。
export type OwnerCostShareCategory =
  | "cleaning"
  | "public_utilities"
  | "water"
  | "broadband"
  | "daily_supplies"
  | "laundry"
  | "gas"
  | "property_fee"
  | "electricity"
  | "maintenance"
  | "other"
  | "kitchen_cleaning"
  | "property_guidance_fee"
  | "new_linen_prewash"
  | "supplies";

export const OWNER_COST_SHARE_CATEGORIES: OwnerCostShareCategory[] = [
  "cleaning",
  "public_utilities",
  "water",
  "broadband",
  "daily_supplies",
  "laundry",
  "gas",
  "property_fee",
  "electricity",
  "maintenance",
  "other",
  "kitchen_cleaning",
  "property_guidance_fee",
  "new_linen_prewash",
  "supplies",
];

export const EXPENSE_CATEGORY_LABEL: Record<OwnerCostShareCategory, string> = {
  cleaning: "保洁",
  public_utilities: "公摊水费",
  water: "水费",
  broadband: "宽带费",
  daily_supplies: "日耗",
  laundry: "洗涤",
  gas: "燃气费",
  property_fee: "物业费",
  electricity: "电费",
  maintenance: "维修费",
  other: "其他费用",
  kitchen_cleaning: "厨房保洁",
  property_guidance_fee: "物业引导费",
  new_linen_prewash: "新布草过水费",
  supplies: "采购费",
};

/**
 * 旧支出类目 → 中文标签。这些是 backend ExpenseCategory 里 @deprecated 的统称
 * （utilities/supplies 等），不在 OwnerCostShareCategory 里，但历史结算单的
 * cost_share_breakdown 里仍会出现。缺了这层兜底，labelFor 会把原始英文 key
 * （如 "utilities"）直接甩给业主看。见 backend/app/models/expense.py。
 */
export const LEGACY_EXPENSE_CATEGORY_LABEL: Record<string, string> = {
  utilities: "水电费",
  // cold_water/hot_water v2#7 已合并进"水费"停用，但历史业主账里仍会出现——
  // 用它们当年在账上的原词（无"旧"后缀），保持业主历史对账稳定。
  cold_water: "冷水",
  hot_water: "热水",
  platform_fee: "平台佣金",
  tax: "税费",
};

export const BOOKING_TYPE_TAB_LABEL: Record<BookingType, string> = {
  normal: "普通订单",
  trial: "试住订单",
  owner_self: "自住订单",
};

export const BOOKING_TYPES_FOR_COST_SHARE: BookingType[] = [
  "normal",
  "trial",
  "owner_self",
];

// ─── ShareRatioTemplate（v2#1 后端枚举的前端镜像）────────────────────────────
export type ShareRatioTemplate =
  | "owner_10"
  | "owner_20"
  | "owner_30"
  | "owner_40"
  | "owner_50"
  | "owner_60"
  | "owner_70"
  | "owner_80"
  | "owner_90"
  | "owner_full"
  | "none"
  | "custom";

export const SHARE_RATIO_TEMPLATE_LABEL: Record<ShareRatioTemplate, string> = {
  owner_10: "一九分（业主 10%）",
  owner_20: "二八分（业主 20%）",
  owner_30: "三七分（业主 30%）",
  owner_40: "四六分（业主 40%）",
  owner_50: "五五分（业主 50%）",
  owner_60: "六四分（业主 60%）",
  owner_70: "七三分（业主 70%）",
  owner_80: "八二分（业主 80%）",
  owner_90: "九一分（业主 90%）",
  owner_full: "业主全担（100%）",
  none: "业主不分成（0%）",
  custom: "自定义",
};

export const SHARE_RATIO_TEMPLATE_VALUES: Record<ShareRatioTemplate, number | null> = {
  owner_10: 0.1,
  owner_20: 0.2,
  owner_30: 0.3,
  owner_40: 0.4,
  owner_50: 0.5,
  owner_60: 0.6,
  owner_70: 0.7,
  owner_80: 0.8,
  owner_90: 0.9,
  owner_full: 1.0,
  none: 0.0,
  custom: null, // 走自定义数字字段
};

/** 反查模板：数字 → 模板枚举（找不到精确匹配返回 custom） */
export function templateForRatio(value: number | string | null | undefined): ShareRatioTemplate {
  if (value === null || value === undefined) return "custom";
  const n = Math.round(Number(value) * 1000) / 1000;
  for (const [tpl, ratio] of Object.entries(SHARE_RATIO_TEMPLATE_VALUES)) {
    if (ratio !== null && Math.abs(ratio - n) < 0.0005) {
      return tpl as ShareRatioTemplate;
    }
  }
  return "custom";
}

// ─── CostShareRule 类型 ─────────────────────────────────────────────────────
export interface CostShareRuleItem {
  booking_type: BookingType;
  expense_category: OwnerCostShareCategory;
  share_percent: number;
  unit_price?: number | null;
}

export interface CostShareRuleOut extends CostShareRuleItem {
  rule_id: string;
  room_id: string;
  created_at: string;
  updated_at: string;
}
