import type { MonthlyCloseProjection, MonthlyCloseRecommendedAction, MonthlyCloseRecommendedActionKind } from "./monthly-close";

export type MonthlyCloseActionDestination =
  | { kind: "no_op"; status: "complete" | "waiting" | "escalate" }
  | { kind: "reopen"; cycleId: string }
  | { kind: "upload_source"; sourceId: string; sourceType: string }
  | { kind: "source_review"; sourceId: string; sourceType: string; documentIds: string[]; mode: "review" | "read_only" }
  | { kind: "source_workflow"; sourceId: string; sourceType: string }
  | { kind: "inbox_detail"; inboxId: string }
  | { kind: "workflow_step"; stepKey: string; mode: "confirm" | "read_only" }
  | { kind: "remediation_detail"; remediationId: string }
  | { kind: "final_review"; cycleId: string }
  | { kind: "invalid"; message: string };

export function resolveMonthlyCloseAction(
  projection: MonthlyCloseProjection,
  action: MonthlyCloseRecommendedAction,
): MonthlyCloseActionDestination {
  const cycle = () => action.target_type === "cycle" && action.target_id === projection.cycle_id;
  const source = () => action.target_type === "source"
    ? projection.sources.find((item) => item.source_id === action.target_id)
    : undefined;
  const invalid = (message = "当前操作目标已变化，请刷新后重试。") => ({ kind: "invalid", message } as const);
  const sourceDestination = (kind: "upload_source" | "source_review", mode: "review" | "read_only" = "review") => {
    const target = source();
    if (!target) return invalid();
    return kind === "upload_source"
      ? { kind, sourceId: target.source_id, sourceType: target.source_type } as const
      : { kind, sourceId: target.source_id, sourceType: target.source_type, documentIds: target.documents.map((document) => document.document_id), mode } as const;
  };

  switch (action.kind) {
    case "confirm_analysis":
    case "continue_analysis":
      return sourceDestination("source_review");
    case "view_blocker":
      return sourceDestination("source_review", "read_only");
    case "review_source_reconciliation": {
      const target = source();
      return target
        ? { kind: "source_workflow", sourceId: target.source_id, sourceType: target.source_type }
        : invalid();
    }
    case "provide_source":
    case "upload_own_source":
      return sourceDestination("upload_source");
    case "confirm_inbox":
      return action.target_type === "inbox" && action.target_id ? { kind: "inbox_detail", inboxId: action.target_id } : invalid();
    case "confirm_workflow_step":
      return action.target_type === "workflow_step" && action.target_id ? { kind: "workflow_step", stepKey: action.target_id, mode: "confirm" } : invalid();
    case "view_workflow_blocker":
      return action.target_type === "workflow_step" && action.target_id ? { kind: "workflow_step", stepKey: action.target_id, mode: "read_only" } : invalid();
    case "view_remediation":
      return action.target_type === "remediation" && action.target_id ? { kind: "remediation_detail", remediationId: action.target_id } : invalid();
    case "reopen_cycle":
      return cycle() ? { kind: "reopen", cycleId: action.target_id } : invalid();
    case "start_final_review":
      return cycle() ? { kind: "final_review", cycleId: action.target_id } : invalid();
    case "none":
      return cycle() ? { kind: "no_op", status: "complete" } : invalid();
    case "wait":
    case "wait_for_assignment":
    case "wait_for_review":
      if (action.target_type === "source" && source()) return { kind: "no_op", status: "waiting" };
      return cycle() ? { kind: "no_op", status: "waiting" } : invalid();
    case "escalate":
      if (action.target_type === "source" && source()) return { kind: "no_op", status: "escalate" };
      return cycle() ? { kind: "no_op", status: "escalate" } : invalid();
    default:
      return invalid("当前操作暂不可用，请刷新后重试或联系管理员。");
  }
}

export const MONTHLY_CLOSE_ACTION_KINDS: readonly MonthlyCloseRecommendedActionKind[] = [
  "confirm_analysis", "confirm_inbox", "confirm_workflow_step", "continue_analysis", "escalate", "none",
  "provide_source", "review_source_reconciliation", "reopen_cycle", "start_final_review", "view_blocker", "view_remediation",
  "view_workflow_blocker", "upload_own_source", "wait", "wait_for_assignment", "wait_for_review",
];
