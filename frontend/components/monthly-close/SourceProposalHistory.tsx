import { Card } from "antd";

import type { MonthlyCloseProposalView } from "@/lib/monthly-close";

const STATUS_LABELS: Record<string, string> = {
  draft: "准备中",
  pending_approval: "等待批准",
  approved: "已批准，尚未写入",
  rejected: "已拒绝",
  stale: "事实已变化",
  superseded: "已被替代",
  executed: "已执行",
  verified: "已复核",
};

function lifecycleLabel(proposal: MonthlyCloseProposalView) {
  if (["rejected", "stale", "superseded"].includes(proposal.status)) {
    return STATUS_LABELS[proposal.status] ?? proposal.status;
  }
  const latestAttempt = [...(proposal.attempts ?? [])].sort((left, right) => (
    (right.attempt_no ?? -1) - (left.attempt_no ?? -1)
    || right.attempt_id.localeCompare(left.attempt_id)
  ))[0];
  if (!latestAttempt) return STATUS_LABELS[proposal.status] ?? proposal.status;

  const verification = proposal.verifications?.find(
    (item) => item.attempt_id === latestAttempt.attempt_id,
  );
  if (latestAttempt.status === "verified" && verification?.status === "passed") {
    return "已复核";
  }
  if (
    ["failed_safe", "failed_confirmed", "remediation_required"].includes(latestAttempt.status)
    || (verification && verification.status !== "passed")
  ) {
    return "失败，需要修复";
  }
  if (latestAttempt.status === "pending") return "执行请求已受理，等待处理";
  if (latestAttempt.status === "executing") return "正在执行";
  if (latestAttempt.status === "succeeded_unverified") return "执行已提交，等待确定性复核";
  if (latestAttempt.status === "unknown") return "执行结果不确定，需要确定性检查/修复";
  if (latestAttempt.status === "verified") return "复核状态待确认";
  return STATUS_LABELS[proposal.status] ?? proposal.status;
}

export function SourceProposalHistory({ items }: { items: MonthlyCloseProposalView[] }) {
  if (!items.length) return null;
  return (
    <section aria-label="来源方案记录" style={{ display: "grid", gap: 8 }}>
      <h3 style={{ margin: 0, fontSize: 16 }}>方案记录</h3>
      {[...items]
        .sort((left, right) => (right.proposal_version ?? 0) - (left.proposal_version ?? 0))
        .map((proposal) => {
          const rejectionReason = proposal.approvals
            ?.filter((approval) => approval.decision === "rejected" && approval.reason?.trim())
            .at(-1)?.reason?.trim();
          return (
            <Card key={proposal.proposal_id} size="small">
              <div>版本 {proposal.proposal_version ?? "—"} · {lifecycleLabel(proposal)}</div>
              <div style={{ fontSize: 12 }}>方案编号 {proposal.proposal_id}</div>
              {proposal.submission_id && <div style={{ fontSize: 12 }}>提交编号 {proposal.submission_id}</div>}
              {proposal.supersedes_proposal_id && <div style={{ fontSize: 12 }}>替代 {proposal.supersedes_proposal_id}</div>}
              {rejectionReason && <div style={{ fontSize: 12, color: "#9B4A43" }}>{rejectionReason}</div>}
            </Card>
          );
        })}
    </section>
  );
}
