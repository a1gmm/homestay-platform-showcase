"use client";

import type { AssistantReply, MonthlyCloseProjection } from "@/lib/monthly-close";
import { tokens } from "@/lib/design-tokens";

const WORKFLOWS: Record<string, { label: string; sourceTypes?: string[]; stepKey?: string }> = {
  all: { label: "本月资料与步骤", stepKey: "source_collection" },
  source_collection: { label: "本月资料", stepKey: "source_collection" },
  cleaning_statement: { label: "保洁费用依据", sourceTypes: ["cleaning_statement"] },
  linen_statement: { label: "布草洗涤费用", sourceTypes: ["linen_statement"] },
  utility_expense: { label: "水电支出", sourceTypes: ["utility_expense", "utility_receipt"] },
  operating_expenses: { label: "运营支出", sourceTypes: ["operating_expenses"] },
  ota_statement: { label: "平台账单", sourceTypes: ["ota_statement"] },
  service_fees: { label: "服务费核对", stepKey: "service_fees" },
  utilities: { label: "水电与运营支出", stepKey: "utilities" },
  ota_statements: { label: "平台对账", stepKey: "ota_statements" },
  order_integrity: { label: "订单完整性", stepKey: "order_integrity" },
  exception_clearance: { label: "异常处理", stepKey: "exception_clearance" },
  preflight: { label: "结算前检查", stepKey: "preflight" },
  settlement_review: { label: "业主结算复核", stepKey: "settlement_review" },
  owner_confirmation: { label: "业主确认", stepKey: "owner_confirmation" },
  final_review: { label: "最终复核" },
};

/** Resolve only a known focus against the current projection, never reply-supplied targets. */
export function chatWorkflowTarget(projection: MonthlyCloseProjection, focus: string) {
  const workflow = WORKFLOWS[focus];
  if (!workflow) return { label: "此项工作", reason: "当前事项没有可用的处理入口。" };
  if (projection.actor_role !== "admin") return { label: workflow.label, reason: "只有管理员可以处理此项工作。" };
  const source = workflow.sourceTypes
    ? projection.sources.find((item) => workflow.sourceTypes?.includes(item.source_type))
    : undefined;
  const step = workflow.stepKey
    ? projection.workflow_evidence.find((item) => item.step_key === workflow.stepKey)
    : undefined;
  const reason = workflow.sourceTypes && !source ? "本月尚无该来源，请刷新本月资料后再试。"
    : workflow.stepKey && !step ? "本月尚无该工作步骤，请刷新本月状态后再试。"
      : undefined;
  return { label: workflow.label, reason, source, stepKey: workflow.stepKey };
}

export function ChatWorkflowActions({ reply, projection, onOpenWorkflow }: {
  reply: AssistantReply;
  projection: MonthlyCloseProjection;
  onOpenWorkflow?: (focus: string) => void;
  onOpenOrder?: (orderId: string) => void;
}) {
  if (reply.tool !== "review_month" || ["cost_responsibility", "amount_summary"].includes(String(reply.facts.query_mode)) || projection.actor_role !== "admin"
    || reply.facts.billing_month !== projection.billing_month) return null;
  const focus = typeof reply.facts.focus === "string" ? reply.facts.focus : "all";
  const target = chatWorkflowTarget(projection, focus);
  const reason = target.reason ?? (!onOpenWorkflow ? "处理入口暂不可用，请刷新页面后再试。" : undefined);
  return <div style={{ marginTop: 12 }}>
    <button type="button" className="mcw-secondary-button" disabled={Boolean(reason)}
      style={{ minHeight: 44, borderRadius: 999, padding: "0 14px", font: "inherit", border: `1px solid ${tokens.anyu.color.linen}`, color: tokens.anyu.color.ink.default, background: tokens.anyu.color.shell }}
      onClick={() => { if (!reason) onOpenWorkflow?.(focus); }}>
      在这里{focus === "final_review" ? "查看" : "处理"}{target.label}
    </button>
    {reason && <div role="status" style={{ marginTop: 6, color: tokens.anyu.color.stone }}>{reason}</div>}
  </div>;
}
