export interface ReviewContext { question_id: string; evidence_hash: string }
export interface ReviewQuestion extends ReviewContext {
  code: string; group: string; title: string; subject: string; message: string;
  amount: string | null; source_id: string | null; source_name: string | null;
  document_id: string | null; order_id: string | null; fact_key: string | null;
  source_row_label?: string | null;
  focus: string; suggestions: string[];
}
export interface ReviewQueue {
  billing_month: string; cycle_id: string; questions: ReviewQuestion[];
  pending_count: number; company_notes: { code: string; message: string }[];
  snapshot_hash: string; checked_at: string; scope_note: string;
}
