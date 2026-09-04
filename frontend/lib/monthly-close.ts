export type MonthlyCloseStepStatus = "ready" | "blocked" | "locked" | "confirmed" | "stale";
export type MonthlyCloseSourceState = "pending" | "uploaded" | "not_applicable";

export interface MonthlyCloseIssue {
  code: string;
  resource_id: string;
  message: string;
  subject?: string;
  impact?: string;
  cause?: string;
  next_step?: string;
  action?: {
    kind: "navigate" | "inline";
    path: string;
    label: string;
    query?: Record<string, string>;
  };
  [key: string]: unknown;
}

export interface MonthlyCloseDocument {
  document_id: string;
  filename: string;
  byte_size: number;
  processing_status: "stored" | "processed" | "rejected";
  processing_error?: string | null;
  engine_type?: string | null;
  engine_id?: string | null;
  uploaded_at?: string | null;
}

export interface ServiceStatementMapping {
  sheet: string;
  header_row: number;
  columns: Record<string, number>;
}

export interface SpreadsheetSheetPreview {
  name: string;
  row_count: number;
  column_count: number;
  rows: string[][];
}

export interface ServiceStatementAnalysis {
  mapping: ServiceStatementMapping;
  line_count: number;
  total_amount: string;
  suggested_by: "deterministic" | "ai" | "remembered" | "administrator";
  field_confidence: Record<string, number>;
  reasons: Record<string, string>;
  needs_confirmation: boolean;
  sheets?: SpreadsheetSheetPreview[];
}

export interface OperatingExpenseMapping {
  sheet: string;
  header_row: number;
  columns: Record<string, number>;
  category_values?: Record<string, string>;
  payer_values?: Record<string, string>;
}

export interface OperatingExpenseAnalysis {
  mapping: OperatingExpenseMapping;
  valid_row_count: number;
  invalid_row_count: number;
  total_amount: string;
  failures: Array<{ row: number; errors: string }>;
  suggested_by: "deterministic" | "ai" | "remembered" | "administrator";
  field_confidence: Record<string, number>;
  reasons: Record<string, string>;
  needs_confirmation: boolean;
  unmapped_categories?: string[];
  unmapped_payers?: string[];
  sheets?: SpreadsheetSheetPreview[];
}

export interface MonthlyCloseSourceClassification {
  source_type: string;
  confidence: number;
  reason: string;
  suggested_by: "deterministic" | "ai";
}

export interface UtilityStatementMapping {
  role: "receipt" | "expense";
  sheet: string;
  header_row: number;
  columns: Record<string, number>;
}

export interface UtilityStatementAnalysis {
  mapping: UtilityStatementMapping;
  months: string[];
  suggested_by: "deterministic" | "ai" | "remembered" | "administrator";
  needs_confirmation: boolean;
  sheets?: SpreadsheetSheetPreview[];
}

export interface MonthlyCloseSource {
  source_type: string;
  state: MonthlyCloseSourceState;
  not_applicable_reason: string | null;
  decided_by: string | null;
  decided_at: string | null;
  documents: MonthlyCloseDocument[];
}

export interface MonthlyCloseStep {
  position: number;
  step_key: string;
  label: string;
  status: MonthlyCloseStepStatus;
  evidence_hash: string;
  blocking_count: number;
  summary: Record<string, unknown>;
  issues: MonthlyCloseIssue[];
  confirmation_title?: string;
  confirmation_items?: string[];
  confirmed_by: string | null;
  confirmed_at: string | null;
}

export interface MonthlyCloseCycle {
  cycle_id: string;
  billing_month: string;
  status: "open" | "completed" | "reopened" | "needs_recheck";
  stored_status: "open" | "completed" | "reopened";
  current_step: string | null;
  progress: number;
  steps: MonthlyCloseStep[];
  sources: MonthlyCloseSource[];
  completed_by: string | null;
  completed_at: string | null;
  reopen_reason: string | null;
  updated_at?: string | null;
}

export type MonthlyCloseInboxStatus = "received" | "classified" | "needs_review" | "confirmed" | "failed" | "dismissed";

export interface MonthlyCloseInboxItem {
  item_id: string;
  filename: string;
  byte_size: number;
  source_type: string | null;
  confidence: number | null;
  suggested_by: string | null;
  classification_reason: string | null;
  status: MonthlyCloseInboxStatus;
  last_error: string | null;
  origin: "admin" | "external";
  submitted_label: string | null;
  document_id: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface MonthlyCloseOverview {
  cycle_id: string;
  billing_month: string;
  status: MonthlyCloseCycle["status"];
  stored_status: MonthlyCloseCycle["stored_status"];
  progress: number;
  current_step: string | null;
  current_step_label: string | null;
  blocking_count: number;
  missing_source_count: number;
  missing_sources: string[];
  inbox_pending_count: number;
  last_actor: string | null;
  updated_at: string | null;
  completed_at: string | null;
}

export interface MonthlyCloseIntakeLink {
  link_id: string;
  source_type: string | null;
  label: string;
  expires_at: string;
  revoked_at: string | null;
  created_at: string | null;
  last_uploaded_at: string | null;
}

export interface MonthlyCloseIntakeLinkCreated extends MonthlyCloseIntakeLink {
  token: string;
}

export interface MonthlyClosePublicIntake {
  label: string;
  source_type: string | null;
  source_label: string | null;
  billing_month: string;
  expires_at: string;
}

export interface MonthlyCloseLayoutMemory {
  document_id: string;
  filename: string;
  source_type: string;
  signature: string;
  version: number;
  mapping: Record<string, unknown>;
  enabled: boolean;
  approved_by: string | null;
  approved_at: string | null;
  use_count: number;
  last_used_at: string | null;
  disabled_by: string | null;
  disabled_at: string | null;
}

export interface MonthlyCloseLayoutMetrics {
  memory_count: number;
  active_memory_count: number;
  disabled_memory_count: number;
  reuse_count: number;
  file_count: number;
  deterministic_recognition_count: number;
  ai_suggestion_count: number;
  remembered_layout_count: number;
  administrator_correction_count: number;
  needs_confirmation_count: number;
  automation_rate: number;
}

export const MONTHLY_CLOSE_SOURCE_LABELS: Record<string, string> = {
  cleaning_statement: "保洁供应商对账单",
  linen_statement: "布草供应商对账单",
  utility_receipt: "水电已收明细",
  utility_expense: "水电费用明细",
  ota_statement: "OTA平台账单",
  operating_expenses: "运营支出明细",
};

export const MONTHLY_CLOSE_STATUS_LABELS: Record<MonthlyCloseStepStatus, string> = {
  ready: "可以确认",
  blocked: "有待处理项",
  locked: "等待上一步",
  confirmed: "已确认",
  stale: "数据变化，需重查",
};

const MONTHLY_CLOSE_EARLY_PROCESS_STEP_KEYS = new Set([
  "service_fees",
  "utilities",
  "ota_statements",
]);

export function canProcessMonthlyCloseStepEarly(stepKey: string) {
  return MONTHLY_CLOSE_EARLY_PROCESS_STEP_KEYS.has(stepKey);
}

export function parseMonthlyCloseReviewParams(search: string) {
  const params = new URLSearchParams(search);
  const requestedMonth = params.get("month");
  const requestedTab = params.get("tab");
  return {
    month: requestedMonth && /^\d{4}-(0[1-9]|1[0-2])$/.test(requestedMonth)
      ? requestedMonth
      : null,
    tab: requestedTab && ["summary", "by-room", "expenses", "settings"].includes(requestedTab)
      ? requestedTab
      : null,
    search: params.get("search") || "",
  };
}

export function safeMonthlyCloseReturnTarget(
  search: string,
  origin = "http://localhost",
): string | null {
  const value = new URLSearchParams(search).get("returnTo");
  if (!value || !value.startsWith("/") || value.startsWith("//") || value.includes("\\")) {
    return null;
  }
  try {
    const target = new URL(value, origin);
    const expectedOrigin = new URL(origin).origin;
    if (target.origin !== expectedOrigin || target.pathname !== "/finance/monthly-close") {
      return null;
    }
    return `${target.pathname}${target.search}${target.hash}`;
  } catch {
    return null;
  }
}
