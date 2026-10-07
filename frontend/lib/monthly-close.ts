export type MonthlyCloseStepStatus = "ready" | "blocked" | "locked" | "confirmed" | "stale";
export type MonthlyCloseSourceState = "pending" | "uploaded" | "not_applicable";

export type MonthlyCloseWorkspaceSourceState = "missing" | "processing" | "needs_action" | "completed" | "blocked" | "not_applicable";

export type MonthlyCloseRecommendedActionKind =
  | "confirm_analysis"
  | "confirm_inbox"
  | "confirm_workflow_step"
  | "continue_analysis"
  | "escalate"
  | "none"
  | "provide_source"
  | "review_source_reconciliation"
  | "review_work_log"
  | "reopen_cycle"
  | "start_final_review"
  | "view_blocker"
  | "view_remediation"
  | "view_workflow_blocker"
  | "upload_own_source"
  | "wait"
  | "wait_for_assignment"
  | "wait_for_review";

export interface MonthlyCloseRecommendedAction {
  kind: MonthlyCloseRecommendedActionKind;
  label: string;
  reason_code: string;
  target_type: string;
  target_id: string;
}

export type MonthlyCloseFinalizationState =
  | "not_started"
  | "pending_approval"
  | "approved"
  | "executing"
  | "succeeded_unverified"
  | "verifying"
  | "unknown"
  | "failed"
  | "remediation"
  | "stale"
  | "reopened"
  | "verified";

export interface MonthlyCloseFinalReview {
  confirmed_settlement_count?: number;
  paid_settlement_count?: number;
  settlement_status_message?: string;
  settlements?: Array<{ settlement_id: string; billing_month: string; owner_name: string; amount: string; status: string }>;
  state: MonthlyCloseFinalizationState;
  source_complete_count: number;
  source_total_count: number;
  unresolved_issue_count: number;
  settlement_count: number;
  settlement_total_amount: string;
  approval_state: string;
  proposal_id?: string | null;
  attempt_id?: string | null;
}

export interface MonthlyCloseProjectedDocumentSummary {
  work_record_count?: number;
  document_id: string;
  receipt_id?: string;
  source_type: string;
  filename?: string;
  storage_state: "receiving" | "stored" | "rejected" | "quarantined";
  classification_state: "pending" | "needs_confirmation" | "confirmed" | "failed";
  analysis_state: "not_started" | "queued" | "running" | "ready" | "work_log_ready" | "needs_mapping" | "failed";
  analysis_kind?: string;
  analysis_message?: string;
  uploaded_at?: string | null;
}

export interface MonthlyCloseProjectedDocument extends MonthlyCloseProjectedDocumentSummary {
  byte_size?: number;
  sha256?: string;
  engine_type?: string;
  engine_id?: string;
  analysis_result?: Record<string, unknown>;
  analysis_error?: string;
}

export interface MonthlyCloseProjectionSource {
  source_id: string;
  source_type: string;
  state: MonthlyCloseWorkspaceSourceState;
  documents: MonthlyCloseProjectedDocumentSummary[];
  not_applicable_reason?: string;
}

export interface MonthlyCloseProjection {
  projection_version: "monthly-close-projection/v1";
  computed_at: string;
  input_hash: string;
  snapshot_through_sequence: string;
  cycle_id: string;
  billing_month: string;
  cycle_status: string;
  actor_role: "admin" | "finance" | "operator" | "cleaner" | "keeper";
  features: {
    assistant_enabled: boolean;
    external_intake_enabled: boolean;
    source_adapters?: boolean;
  };
  can_advance_final_review: boolean;
  sources: MonthlyCloseProjectionSource[];
  workflow_evidence: Array<Record<string, unknown>>;
  final_close_blockers: string[];
  final_review: MonthlyCloseFinalReview;
  recommended_action: MonthlyCloseRecommendedAction;
  financial_case?: {
    sources: Array<{ source_id: string; filename: string; kind: string; version: number; document_id: string | null; source_types: string[]; state: string; fact_count: number; reviewed_fact_count: number; posted_fact_count: number; matched_fact_count: number; pending_count: number }>;
    pending_issues: Array<{ issue_key: string; source_id: string | null; code: string; message: string; step_key: string }>;
    snapshot_hash: string | null;
  };
}

export interface MonthlyCloseEvent {
  event_id: string;
  kind: string;
  schema_version: string;
  payload: Record<string, unknown>;
  occurred_at: string;
}

export interface MonthlyCloseEventPage {
  events: MonthlyCloseEvent[];
  next_cursor: string;
}

function isMonthlyCloseEvent(value: unknown): value is MonthlyCloseEvent {
  if (!value || typeof value !== "object") return false;
  const event = value as Partial<MonthlyCloseEvent>;
  return typeof event.event_id === "string"
    && typeof event.kind === "string"
    && typeof event.schema_version === "string"
    && typeof event.occurred_at === "string"
    && !!event.payload
    && typeof event.payload === "object";
}

export function parseMonthlyCloseEventStream(body: string, currentCursor: string): MonthlyCloseEventPage {
  const trimmed = body.trim();
  if (!trimmed) return { events: [], next_cursor: currentCursor };
  if (trimmed.startsWith("{")) {
    const page = JSON.parse(trimmed) as Partial<MonthlyCloseEventPage>;
    return {
      events: Array.isArray(page.events) ? page.events.filter(isMonthlyCloseEvent) : [],
      next_cursor: typeof page.next_cursor === "string" && page.next_cursor ? page.next_cursor : currentCursor,
    };
  }

  const events: MonthlyCloseEvent[] = [];
  let nextCursor = currentCursor;
  for (const frame of trimmed.split(/\r?\n\r?\n+/)) {
    let id = "";
    let eventKind = "";
    const data: string[] = [];
    for (const line of frame.split(/\r?\n/)) {
      if (line.startsWith("id:")) id = line.slice(3).trim();
      else if (line.startsWith("event:")) eventKind = line.slice(6).trim();
      else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
    }
    if (eventKind === "cursor") {
      if (id) nextCursor = id;
      continue;
    }
    if (!data.length) continue;
    try {
      const event = JSON.parse(data.join("\n")) as unknown;
      if (isMonthlyCloseEvent(event)) events.push(event);
    } catch {
      // A partial frame never advances the cursor; the next replay returns it again.
    }
  }
  return { events, next_cursor: nextCursor };
}

export interface MonthlyCloseReceiptView {
  receipt_id: string;
  document_id?: string | null;
  filename: string;
  storage_state: "stored";
  classification_state: "pending" | "needs_confirmation" | "confirmed" | "failed";
  processing_job_state?: MonthlyCloseProcessingStatus;
  analysis_state: MonthlyCloseProjectedDocumentSummary["analysis_state"];
}

export interface ReconciliationOutput {
  document: { title: string; billing_month: string; queried_at: string; summary: string; caveats: string[];
    sections: Array<{ title: string; columns: string[]; rows: Array<Array<string | number | null>>; note: string }> };
  presentation: { style: "detail" | "summary" | "table" | "checklist"; formats: Array<"txt" | "md" | "csv" | "json" | "xlsx" | "docx" | "pdf" | "html"> };
}
export interface AssistantReply {
  output?: ReconciliationOutput;
  message: string;
  intent: "read_query" | "clarification" | "write_request" | "sensitive_input" | "action_plan" | "action_result";
  tool: string | null;
  facts: Record<string, unknown>;
  recommended_action: MonthlyCloseRecommendedAction | Record<string, never>;
  narration_degraded: boolean;
  conversation_id: string;
  run_id: string;
}

export interface FinancialCaseIssue {
  code: string;
  message: string;
  source_id?: string;
  fact_key?: string;
}

export interface FinancialCaseUploadReceipt {
  source_id: string;
  item_id: string;
  filename: string;
  kind: string;
  fact_count: number;
  issues_count: number;
  issues?: FinancialCaseIssue[];
}

export interface FinancialCaseFacts {
  projection_version: "financial-case-v1";
  billing_month: string;
  state: "result" | "proposal" | "completed" | "needs_information";
  message: string;
  sources: Array<{ source_id: string; filename: string; kind: string; fact_count: number }>;
  metrics: Array<{ label: string; value: string | number; detail?: string }>;
  issues: FinancialCaseIssue[];
  details: Array<{ label: string; value: string | number }>;
  proposal_id: string | null;
  proposal_kind?: "interpretation" | "posting" | "business_correction" | null;
  report_months: string[];
  export_ready: boolean;
  ledger_scope?: { months: string[]; categories: string[]; floors: number[]; rooms: string[]; exclude_reception: boolean; payer: string | null; cleaning_kind: string | null } | null;
  order_scope?: { version: "order-query-scope-v1"; run_id: string; actor_id: string; cycle_id: string; billing_month: string; control_version: number; cycle_status: string; order_ids: string[]; snapshot_hash: string; issue_hash: string } | null;
}

export interface CleaningInvestigationCase {
  case_id: string;
  ordinal: number;
  document_id: string;
  service_date: string | null;
  room_id: string | null;
  service_type: string;
  status: string;
  vendor_amount: string;
  system_amount: string | null;
  difference: string | null;
  summary: string;
  question: string;
  sources: Array<{ document_id: string; line_id: string; row_number: number; amount: string }>;
  related_sources: Array<{ document_id: string; line_id: string; row_number: number; amount: string }>;
  expenses: Array<{ expense_id: string; amount: string; expense_date: string; order_id: string | null }>;
  orders: Array<{ order_id: string; room_id: string | null; check_in: string; check_out: string; status: string; stay_group_id: string | null; relationship: string }>;
  tasks: Array<{ task_id: string; status: string; review_status: string | null; completed_at: string | null }>;
  evidence_hash: string;
  detail_limited: boolean;
}

export interface CleaningInvestigationFacts {
  kind: "cleaning_investigation";
  findings?: Array<{ id: string; text: string; evidence_refs: string[] }>;
  investigation_steps?: string[];
  reasoning_mode?: "not_needed" | "model" | "disabled" | "fallback";
  correction_draft?: { state: string; message: string; amount_impact: string; evidence_hash: string; case_related: boolean; changes: Array<{ order_id: string; room_id: string; category: string; before_amount: string | null; after_amount: string; before_date: string | null; after_date: string; before_payer: string | null; after_payer: string }> } | null;
  checked_at: string;
  state: string;
  billing_month: string;
  cases: CleaningInvestigationCase[];
  selected: CleaningInvestigationCase | null;
  evidence_changed: boolean;
  note_code: "extra_cleaning" | "replacement_statement" | "service_not_performed" | null;
  page: number;
  has_more: boolean;
}

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
  work_log?: CleaningWorkComparison | null;
}

export interface CleaningWorkComparison {
    recorded_count?: number;
    effective_record_count?: number;
    resolutions?: CleaningWorkResolution[];
    importable_count?: number;
    records?: Array<CleaningWorkRow & { record_id: string; document_id: string; quantity?: number }>;
    record_count: number;
    normal_count: number;
    instay_count: number;
    matched_count: number;
    basis: string;
    note: string;
    differences: Array<{
      service_date: string;
      room_ref: string;
      service_type: string;
      status: string;
      source_rows: number[];
      system_ids: string[];
      table_count: number;
      system_count: number;
      source_entries?: CleaningWorkRow[];
      system_evidence?: Array<{ record_id: string; completed: boolean; kind: string; quantity?: number }>;
    }>;
}

export interface CleaningWorkRow {
  service_date: string;
  room_ref: string;
  service_type: string;
  source_sheet: string;
  source_row: number;
}
export interface CleaningWorkPreview {
  preview_hash: string;
  document_id: string;
  billing_month: string;
  actions: CleaningWorkRow[];
  add_count: number;
  remaining_count: number;
  comparison: CleaningWorkComparison;
  effect: string;
}
export interface CleaningWorkImportResult {
  import_id: string;
  added_count: number;
  record_ids: string[];
  comparison: CleaningWorkComparison;
}

export interface OperatingExpenseMapping {
  sheet: string;
  header_row: number;
  columns: Record<string, number>;
  category_values?: Record<string, string>;
  payer_values?: Record<string, string>;
  predecessor_rows?: Record<string, {
    document_id: string;
    row_number: number;
  }>;
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
  replacement_context?: {
    current_unreferenced_rows: number[];
    predecessor_rows: Array<{
      document_id: string;
      row_number: number;
      source_sheet: string;
    }>;
  } | null;
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
  record_count: number;
  total_amount: string;
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

export interface MonthlyCloseProposalCommand {
  command_type: string;
  subject_id: string;
  before: Record<string, unknown>;
  after: Record<string, unknown>;
  amount_impact: string;
  evidence_refs: Array<Record<string, unknown>>;
  business_idempotency_key: string;
}

export interface MonthlyCloseExecutionAttemptView {
  attempt_id: string;
  proposal_id: string;
  status: string;
  request_id: string;
  attempt_no?: number;
  command_results?: Array<Record<string, unknown>>;
  audit_refs?: string[];
}

export interface MonthlyCloseVerificationView {
  verification_id: string;
  attempt_id: string;
  status: string;
  checks: Record<string, unknown>;
  failure_code?: string | null;
  evidence_hash?: string;
}

export interface MonthlyCloseApprovalView {
  approval_id: string;
  decision: string;
  decided_by: string;
  maker_is_checker: boolean;
  policy_version: string;
  request_id: string;
  reason?: string | null;
  decided_at: string;
}

export interface MonthlyCloseProposalView {
  proposal_id: string;
  cycle_id: string;
  proposal_type: string;
  proposal_billing_month?: string;
  status: string;
  created_at?: string | null;
  proposal_version?: number;
  supersedes_proposal_id?: string | null;
  submission_id?: string | null;
  canonical_payload: { commands: MonthlyCloseProposalCommand[] };
  impact_snapshot: {
    change_count?: number;
    total_amount?: string;
    required_approver?: string;
    risk?: string;
    verification?: string;
    changes?: Array<Record<string, unknown>>;
  };
  approval_policy_snapshot: {
    approval_roles?: string[];
    required_approvals?: number;
    policy_version?: string;
  };
  attempts?: MonthlyCloseExecutionAttemptView[];
  verifications?: MonthlyCloseVerificationView[];
  approvals?: MonthlyCloseApprovalView[];
  source_state?: "missing" | "needs_confirmation" | "ready" | "completed" | "blocked";
  source_issues?: string[];
}

export interface MonthlyCloseSourceProposalState {
  state: "missing" | "needs_confirmation" | "ready" | "completed" | "blocked";
  issue_codes: string[];
  change_count: number;
  proposal_id: string | null;
  upload_required: boolean;
}

export interface MonthlyCloseSourceProposalQueue {
  items: MonthlyCloseProposalView[];
  active: Record<string, MonthlyCloseProposalView>;
  source_states?: Record<string, MonthlyCloseSourceProposalState>;
}

export interface OtaAppealAdjudicationProposalGroup {
  issue_id: string;
  candidate_id: string;
  pending: MonthlyCloseProposalView[];
  rejected: MonthlyCloseProposalView[];
  verified: MonthlyCloseProposalView[];
}

export interface OtaProposalDecision {
  diff_id: string;
  decision: "action" | "claim" | "dismiss";
  action?: string;
  order_id?: string;
  reason?: string;
}

export interface OtaOrderCandidate {
  order_id: string;
  channel: string;
  check_in_date: string;
  check_out_date: string;
  expected_revenue: string | null;
  platform_linked: boolean;
}

export interface OtaAppealSettlementCandidate {
  candidate_id: string;
  candidate_hash: string;
  issue_id: string;
  issue_key: string;
  later_cycle_id: string;
  later_billing_month: string;
  later_document_id: string;
  later_document_sha256: string;
  later_batch_id: string;
  later_batch_bill_month: string;
  later_batch_status: string;
  later_batch_fingerprint: string;
  later_input_set_hash: string;
  source_row: {
    row_index: number;
    row_hash: string;
    row_type: string;
    amount: string | null;
    checkin: string | null;
    checkout: string | null;
    platform_order_ref: string | null;
  } | null;
  settled_amount: string | null;
  short_paid: boolean;
  actionable: boolean;
  reason_code: string;
  recovery_kind: string;
  action: string;
  identity_choices: Array<{
    choice_id: string;
    order_ref: string;
    channel: string;
    order_version: string;
    is_current_appeal: boolean;
  }>;
  original: {
    cycle_id: string;
    billing_month: string;
    batch_id: string;
    batch_status: string;
    diff_id: string;
    proposal_id: string;
    attempt_id: string;
    verification_id: string;
    approved_evidence_hash: string;
    verification_evidence_hash: string;
    active_input_set_hash: string;
    command_hash: string;
  };
}

export type MonthlyCloseInboxStatus = "received" | "classified" | "needs_review" | "confirmed" | "failed" | "dismissed";
export type MonthlyCloseProcessingStatus = "queued" | "processing" | "needs_review" | "completed" | "failed_safe";

export interface MonthlyCloseDurableReceipt {
  receipt_id: string;
  item_id: string;
  cycle_id: string;
  billing_month: string;
  status: "stored";
  processing_status: MonthlyCloseProcessingStatus;
  processing_job_id: string;
  document_id: string | null;
}

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
  storage_status?: "stored";
  processing_status?: MonthlyCloseProcessingStatus | null;
  processing_job_id?: string | null;
  classification_status?: MonthlyCloseProcessingStatus | null;
  classification_job_id?: string | null;
  classification_generation?: number;
  analysis_status?: MonthlyCloseProcessingStatus | null;
  analysis_job_id?: string | null;
  analysis_generation?: number | null;
  analysis_result?: Record<string, unknown> | null;
  analysis_error?: string | null;
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
  source_type: string;
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
  source_type: string;
  source_label: string;
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
  cleaning_statement: "保洁打扫记录",
  linen_statement: "布草／洗涤记录",
  utility_receipt: "水电支出（历史记录）",
  utility_expense: "水电支出",
  ota_statement: "OTA平台账单",
  operating_expenses: "其他运营支出",
};

export const MONTHLY_CLOSE_ACTIVE_SOURCE_TYPES = [
  "cleaning_statement",
  "linen_statement",
  "utility_expense",
  "ota_statement",
  "operating_expenses",
] as const;

export const MONTHLY_CLOSE_ACTIVE_SOURCE_OPTIONS = MONTHLY_CLOSE_ACTIVE_SOURCE_TYPES.map(
  (value) => ({ value, label: MONTHLY_CLOSE_SOURCE_LABELS[value] }),
);

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

export interface CleaningWorkResolutionSelection {
  service_date: string;
  room_ref: string;
  service_type: string;
  decision: "exclude_system" | "count_once" | "accept_table";
  reason: string;
}
export interface CleaningWorkResolutionPreview {
  preview_hash: string;
  selection: CleaningWorkResolutionSelection;
  item: CleaningWorkComparison["differences"][number];
  confirmed_count: number;
  comparison: CleaningWorkComparison;
}
export interface CleaningWorkResolution extends CleaningWorkResolutionSelection {
  resolution_id: string;
  confirmed_count: number;
  confirmed_by: string;
  created_at: string;
  state: "applied" | "stale";
}
