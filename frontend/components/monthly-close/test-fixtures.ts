import type { MonthlyCloseProjection } from "@/lib/monthly-close";

export const projectionFixture: MonthlyCloseProjection = {
  projection_version: "monthly-close-projection/v1",
  computed_at: "2026-08-31T20:00:00Z",
  input_hash: "fixture",
  snapshot_through_sequence: "mce2.opaque-snapshot-boundary",
  cycle_id: "MCL-2026-08",
  billing_month: "2026-08",
  cycle_status: "open",
  actor_role: "admin",
  features: { assistant_enabled: true, external_intake_enabled: true },
  can_advance_final_review: true,
  sources: [
    {
      source_id: "source:ota_statement",
      source_type: "ota_statement",
      state: "needs_action",
      documents: [{
        document_id: "DOC-OTA",
        source_type: "ota_statement",
        filename: "八月携程账单.xlsx",
        storage_state: "stored",
        classification_state: "confirmed",
        analysis_state: "failed",
        uploaded_at: "2026-08-31T19:00:00Z",
      }],
    },
    {
      source_id: "source:cleaning_statement",
      source_type: "cleaning_statement",
      state: "missing",
      documents: [],
    },
  ],
  workflow_evidence: [],
  final_close_blockers: ["source:cleaning_statement"],
  final_review: {
    state: "not_started",
    source_complete_count: 0,
    source_total_count: 2,
    unresolved_issue_count: 1,
    settlement_count: 0,
    settlement_total_amount: "0.00",
    approval_state: "not_started",
  },
  recommended_action: {
    kind: "confirm_analysis",
    label: "现在建议先确认 OTA 的金额列",
    reason_code: "analysis_confirmation_required",
    target_type: "source",
    target_id: "source:ota_statement",
  },
};
