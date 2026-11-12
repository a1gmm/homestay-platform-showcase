export interface ReviewContext { question_id: string; evidence_hash: string }
export interface ReviewQuestion extends ReviewContext {
  code: string; group: string; title: string; subject: string; message: string;
  amount: string | null; source_id: string | null; source_name: string | null;
  document_id: string | null; order_id: string | null; fact_key: string | null;
  source_row_label?: string | null;
  focus: string; suggestions: string[];
}
export interface ReviewBatch { label: string; command: string; attachment_ids: string[]; member_ids: string[] }
/** One step (button) or one decision. Members stay the answerable, evidence-bound cards. */
export interface ReviewGroup {
  group_id: string; kind: "action" | "question"; code: string; title: string; message: string;
  member_ids: string[]; batch: ReviewBatch | null;
  /** Answer the whole group through this member's evidence-bound card. */
  answer_via?: string;
  flags?: { question_id: string; messages: string[] }[];
  /** Actions only. */
  target?: "document" | "command" | "refresh" | "workflow"; document_id?: string | null; focus?: string | null;
  subject?: string; command?: string; count?: number;
}
export interface ReviewQueue {
  billing_month: string; cycle_id: string; questions: ReviewQuestion[];
  groups?: ReviewGroup[]; question_count?: number; action_count?: number; item_count?: number;
  pending_count: number; company_notes: { code: string; message: string }[];
  snapshot_hash: string; checked_at: string; scope_note: string;
}
