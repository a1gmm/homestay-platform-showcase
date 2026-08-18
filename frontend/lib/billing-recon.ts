// 账单对账（billing-recon）：类型 + 展示常量。
// 与后端 app/models/recon.py 的两个枚举（ReconDiffClass/ReconDiffStatus）一一对应，
// 与 app/api/v1/billing_recon.py 的响应形状一一对应——改动务必先读那两个文件，别猜。

export type ReconDiffClass =
  | "fix_amount" | "appeal" | "broken_link" | "compensation" | "manual_review";

export type ReconDiffStatus =
  | "pending" | "adopted" | "already_consistent" | "dismissed"
  | "appeal_pending" | "appeal_settled" | "acknowledged";

export interface ReconBatchStats {
  out_of_window: number;
  in_window_ratio: number;
}

export interface ReconBatchOut {
  batch_id: string;
  platform: string;
  bill_month: string;
  summary_total: string;
  row_count: number;
  status: "parsed" | "rejected";
  error: string | null;
  stats: ReconBatchStats;
  created_at: string | null;
  summary?: ReconSummary;
  diagnosis?: BatchDiagnosis;
  ai_status?: "pending" | "ready" | "failed";
  reviewed_at?: string | null;
  reviewed_by?: string | null;
  filename?: string | null;
  archived_at?: string | null;
}

export interface ReconDiffOut {
  diff_id: string;
  batch_id: string;
  order_id: string | null;
  platform_order_id: string | null;
  guest_name: string | null;
  diff_class: ReconDiffClass;
  status: ReconDiffStatus;
  bill_amount: string | null;
  system_amount: string | null;
  detail: Record<string, unknown>;
}

export interface DiffActionResult {
  status: ReconDiffStatus;
  settlement_warnings: string[];
}

export const CLAIM_CONFIDENCE_SINGLE = 0.85;

export interface ClaimCandidate {
  order_id: string;
  confidence: number;
  reason?: string;
  reason_codes?: ClaimReasonCode[];
}

export type ClaimReasonCode =
  | "same_guest" | "same_dates" | "similar_amount"
  | "same_room_type" | "same_platform_id_fragment";

const CLAIM_REASON_LABELS: Record<ClaimReasonCode, string> = {
  same_guest: "客人姓名一致",
  same_dates: "入住和离店日期一致",
  similar_amount: "金额接近",
  same_room_type: "房型一致",
  same_platform_id_fragment: "平台单号片段一致",
};

export function claimReason(candidate: ClaimCandidate): string {
  if (candidate.reason_codes?.length) {
    return candidate.reason_codes.map((code) => CLAIM_REASON_LABELS[code]).join("、");
  }
  return candidate.reason ?? "AI 找到一个可能对应的系统订单";
}

export interface ReconSummary {
  fix_amount: number;
  appeal: number;
  broken_link: number;
  compensation: number;
  manual_review: number;
  appeal_total: string;
  total_actionable_count?: number;
  resolved_actionable_count?: number;
  pending_count?: number;
  pending_impact_total?: string;
}

export interface BatchDiagnosis {
  summary?: string;
  theme_codes?: string[];
  per_row?: Record<string, string>;
  per_row_codes?: Record<string, string[]>;
}

export function amountExplanation(diff: ReconDiffOut): string {
  if (diff.diff_class === "fix_amount") {
    const system = Number(diff.system_amount);
    const bill = Number(diff.bill_amount);
    if (Number.isFinite(system) && Number.isFinite(bill)) {
      const delta = bill - system;
      return delta >= 0
        ? `平台账单比系统多 ¥${Math.abs(delta).toFixed(2)}`
        : `平台账单比系统少 ¥${Math.abs(delta).toFixed(2)}`;
    }
  }
  if (diff.diff_class === "appeal") return "系统有订单，但平台本月账单未结算";
  if (diff.diff_class === "compensation") return "平台账单包含一笔赔款或扣款";
  if (diff.diff_class === "broken_link") return "金额一致，但平台单号没有关联到系统订单";
  return "系统无法自动确认这条账单对应哪笔订单";
}

export interface ClaimResult {
  status: ReconDiffStatus;
  diff_class: ReconDiffClass;
}

export const DIFF_CLASS_META: Record<ReconDiffClass, { label: string; color: string; hint: string }> = {
  fix_amount: { label: "需修数", color: "red", hint: "平台结算额与系统到手不一致，采纳=改到手价并锁价" },
  appeal: { label: "需申诉", color: "orange", hint: "请先在平台后台提交申诉，再在这里标记，系统会跨月追踪补结" },
  broken_link: { label: "断链", color: "blue", hint: "客人名和金额已对上，确认后会把平台单号写入系统订单" },
  compensation: { label: "赔款", color: "purple", hint: "账单负向扣款，采纳=记一笔支出" },
  manual_review: { label: "人工核对", color: "gold", hint: "同名多候选或完全匹配不上，人工判断后忽略或线下处理" },
};

export const DIFF_STATUS_META: Record<ReconDiffStatus, { label: string; color: string }> = {
  pending: { label: "待处理", color: "processing" },
  adopted: { label: "已采纳", color: "success" },
  already_consistent: { label: "已一致", color: "default" },
  dismissed: { label: "已忽略", color: "default" },
  appeal_pending: { label: "申诉中", color: "warning" },
  appeal_settled: { label: "申诉已补结", color: "success" },
  acknowledged: { label: "已确认", color: "default" },
};

export interface DiffActionDef {
  action: string;
  label: string;
  danger?: boolean;
}

// 按 diff_class 各自的合法动作渲染按钮组——与后端 engine.py 的 _ALLOWED 一一对应：
//   fix_amount/compensation: adopt/dismiss
//   appeal: appeal（"确认申诉"，进入 appeal_pending）/dismiss
//   broken_link: acknowledge/dismiss
//   manual_review: dismiss（人工核对不自动写库，只能忽略）
// appeal_settled 行的 detail.short_paid=true 时算少补差额（追钱唯一入口——
// appeal_settled 之后系统不会再自动申诉，人得看这行红字才知道钱没补够）。
// 返回格式化后的差额字符串（两位小数），无法判定时返回 null（调用方不渲染红字）。
export function shortPaidAmount(detail: Record<string, unknown> | null | undefined): string | null {
  if (!detail || detail.short_paid !== true) return null;
  const settled = Number(detail.settled_amount);
  const sys = Number(detail.system_amount);
  if (!Number.isFinite(settled) || !Number.isFinite(sys)) return null;
  const diff = sys - settled;
  return diff > 0 ? diff.toFixed(2) : null;
}

// detail.has_compensation 命中时格式化「含赔款 ¥金额」文案——单条确认框（confirmTitle）、
// 全部采纳弹窗列表、金额列三处共用同一份格式化+金额兜底逻辑（终稿 R5 去重）。
// 金额缺失时仍返回「含赔款 ¥」（保持与去重前逐处写法一致，调用方不用额外判空金额）。
export function compensationLabel(detail: Record<string, unknown> | null | undefined): string | null {
  if (!detail || detail.has_compensation !== true) return null;
  return `含赔款 ¥${String(detail.compensation_amount ?? "")}`;
}

export const ACTIONS_BY_CLASS: Record<ReconDiffClass, DiffActionDef[]> = {
  fix_amount: [
    { action: "adopt", label: "采纳修数", danger: true },
    { action: "dismiss", label: "忽略" },
  ],
  appeal: [
    { action: "appeal", label: "标记为已提交申诉" },
    { action: "dismiss", label: "忽略" },
  ],
  broken_link: [
    { action: "acknowledge", label: "确认并修复关联" },
    { action: "dismiss", label: "忽略" },
  ],
  compensation: [
    { action: "adopt", label: "记入支出", danger: true },
    { action: "dismiss", label: "忽略" },
  ],
  manual_review: [{ action: "dismiss", label: "忽略" }],
};
