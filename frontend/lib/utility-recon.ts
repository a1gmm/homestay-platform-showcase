export type UtilityCategory = "water" | "electricity";
export type UtilityAnomaly = "category_mismatch" | "floor_mismatch" | "delayed_payment" | "merged_payment" | "receipt_only" | "expense_only" | "unparseable";

export const CATEGORY_LABELS: Record<UtilityCategory, string> = { water: "水费", electricity: "电费" };
export const ANOMALY_LABELS: Record<UtilityAnomaly, string> = {
  category_mismatch: "疑似科目写错", floor_mismatch: "疑似楼层写错",
  delayed_payment: "疑似延迟付款", merged_payment: "疑似合并付款",
  receipt_only: "疑似漏付或漏记", expense_only: "疑似多付、跨期费用或漏收",
  unparseable: "需要人工确认",
};

export interface UtilityPreflight {
  files: Array<{ filename: string; role: "receipt" | "expense"; months: string[]; mapping_status: string; sheets: Array<{ name: string; row_count: number; months: string[] }> }>;
  common_months: string[];
  receipt_only_months: string[];
  expense_only_months: string[];
}

export interface UtilityFloorSummary { floor: string; category: UtilityCategory; receipt: string; expense: string; difference: string }
export interface UtilitySummary { receipt_total: string; expense_total: string; total_difference: string; by_floor_category: UtilityFloorSummary[] }
export interface UtilityBatch { batch_id: string; month: string; status: string; raw_difference: string; corrected_difference: string; raw_summary: UtilitySummary; corrected_summary: UtilitySummary; anomaly_counts: Record<string, number>; created_at: string | null }
export interface UtilityRow { row_id: string; side: "receipt" | "expense"; business_date: string | null; floor: string | null; room: string | null; category: UtilityCategory | null; amount: string | null; source_filename: string; source_sheet: string; source_row_number: number; customer_name: string | null; disposition: string; exclusion_reason: string | null }
export interface UtilitySuggestion { suggestion_id: string; kind: UtilityAnomaly; related_row_ids: string[]; patch: Record<string, unknown>; evidence: Record<string, unknown>; confidence: string; impact: Record<string, unknown>; status: string }
export interface UtilityBatchDetail { batch: UtilityBatch; rows: UtilityRow[]; suggestions: UtilitySuggestion[] }
