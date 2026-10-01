import type { MonthlyCloseFinalReview, MonthlyCloseProjectedDocument } from "@/lib/monthly-close";
import { tokens } from "@/lib/design-tokens";

const reviewStatus: Record<MonthlyCloseFinalReview["state"], string> = {
  not_started: "尚未提交最终复核",
  pending_approval: "等待管理员批准",
  approved: "已批准，等待执行",
  executing: "正在安全执行",
  succeeded_unverified: "写入完成，等待确定性复核",
  verifying: "正在进行确定性复核",
  unknown: "执行结果待确认",
  failed: "执行未完成",
  remediation: "需要先完成补救",
  stale: "依据已变化，需要重新复核",
  reopened: "月结已重新打开",
  verified: "月结已完成",
};

function FinalReview({ review, canAdvance, onStart, summaryOnly }: { review: MonthlyCloseFinalReview; canAdvance: boolean; onStart: () => void; summaryOnly: boolean }) {
  const amount = Number(review.settlement_total_amount || 0).toLocaleString("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  const actionDisabled = ["executing", "verifying", "remediation"].includes(review.state);
  return (
    <section aria-label="最终复核" aria-live="polite" style={{ display: "grid", gap: 14 }}>
      <div style={{ fontSize: 18, fontWeight: 500 }}>最终复核</div>
      <dl style={{ margin: 0, display: "grid", gap: 12 }}>
        <div><dt style={{ color: tokens.anyu.color.stone, fontSize: 12 }}>资料完整性</dt><dd style={{ margin: "3px 0 0" }}>资料 {review.source_complete_count}/{review.source_total_count} 类齐全</dd></div>
        <div><dt style={{ color: tokens.anyu.color.stone, fontSize: 12 }}>异常</dt><dd style={{ margin: "3px 0 0" }}>未解决异常 {review.unresolved_issue_count} 项</dd></div>
        <div><dt style={{ color: tokens.anyu.color.stone, fontSize: 12 }}>结算摘要</dt><dd style={{ margin: "3px 0 0" }}>业主结算 {review.settlement_count} 份 · ¥{amount}</dd></div>
        <div><dt style={{ color: tokens.anyu.color.stone, fontSize: 12 }}>批准状态</dt><dd style={{ margin: "3px 0 0" }}>{reviewStatus[review.state]}</dd></div>
      </dl>
      {!summaryOnly && review.state !== "verified" && canAdvance && (
        <button
          type="button"
          disabled={actionDisabled}
          onClick={onStart}
          style={{ minHeight: 44, borderRadius: 999, padding: "0 18px", font: "inherit", border: 0, background: tokens.anyu.color.ink.default, color: tokens.anyu.color.shell, opacity: actionDisabled ? 0.55 : 1 }}
        >
          开始最终复核
        </button>
      )}
      {!summaryOnly && review.state !== "verified" && !canAdvance && (
        <div role="status" style={{ color: tokens.anyu.color.stone }}>等待管理员继续</div>
      )}
    </section>
  );
}

export function EvidencePanel({ selected, loading, error, finalReview, canAdvanceFinalReview = false, summaryOnly = false, onStartFinalReview = () => undefined }: { selected: MonthlyCloseProjectedDocument | null; loading: boolean; error: boolean; finalReview?: MonthlyCloseFinalReview; canAdvanceFinalReview?: boolean; summaryOnly?: boolean; onStartFinalReview?: () => void }) {
  if (loading) return <div role="status">正在读取所选资料证据…</div>;
  if (error) return <div role="alert">所选对象已不可用，请重新选择。</div>;
  if (finalReview) return <FinalReview review={finalReview} canAdvance={canAdvanceFinalReview} onStart={onStartFinalReview} summaryOnly={summaryOnly} />;
  if (!selected) return <div style={{ color: tokens.anyu.color.stone }}>选择一份资料查看证据</div>;
  return (
    <dl style={{ margin: 0, display: "grid", gap: 14 }}>
      <div><dt style={{ color: tokens.anyu.color.stone, fontSize: 12 }}>文件</dt><dd style={{ margin: "3px 0 0", overflowWrap: "anywhere" }}>{selected.filename ?? "已选资料"}</dd></div>
      <div><dt style={{ color: tokens.anyu.color.stone, fontSize: 12 }}>保存状态</dt><dd style={{ margin: "3px 0 0" }}>{selected.storage_state === "stored" ? "文件已保存" : "保存尚未完成"}</dd></div>
      <div><dt style={{ color: tokens.anyu.color.stone, fontSize: 12 }}>分析状态</dt><dd style={{ margin: "3px 0 0" }}>{selected.analysis_state === "work_log_ready" ? selected.analysis_message || "已识别打扫记录，记录核对与费用核对分步进行。" : selected.analysis_state === "ready" ? "分析已完成" : selected.analysis_state === "failed" ? "自动分析没有完成，请调整读取方式；原文件已保存，无需重新上传" : selected.analysis_state === "needs_mapping" ? "等待确认读取方式" : selected.analysis_state === "not_started" ? "尚未开始分析" : "正在处理"}</dd></div>
    </dl>
  );
}
