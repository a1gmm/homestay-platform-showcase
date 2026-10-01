import type { MonthlyCloseProjection, MonthlyCloseSourceProposalQueue, MonthlyCloseWorkspaceSourceState } from "./monthly-close";

const CONTRACT_SOURCES = {
  operating_expense_import: ["operating_expenses"],
} as const;

function workspaceState(state: string): MonthlyCloseWorkspaceSourceState {
  if (state === "completed") return "completed";
  if (state === "blocked") return "blocked";
  if (state === "missing") return "missing";
  return "needs_action";
}

export function applySourceProposalQueue(
  projection: MonthlyCloseProjection,
  queue?: MonthlyCloseSourceProposalQueue,
): MonthlyCloseProjection {
  const states = queue?.source_states;
  if (!states) return projection;
  const sourceTypesByContract = new Map<string, string>();
  Object.entries(CONTRACT_SOURCES).forEach(([proposalType, sourceTypes]) => {
    sourceTypes.forEach((sourceType) => sourceTypesByContract.set(sourceType, proposalType));
  });
  const sources = projection.sources.map((source) => {
    const proposalType = sourceTypesByContract.get(source.source_type);
    const state = proposalType ? states[proposalType] : undefined;
    return state && source.state !== "not_applicable"
      ? { ...source, state: workspaceState(state.state) }
      : source;
  });
  const service = states.service_fee_reconciliation;
  if (service) {
    sources.push({
      source_id: "source:system_service_fees",
      source_type: "system_service_fees",
      state: workspaceState(service.state),
      documents: [],
    });
  }
  const workflowTargets = [
    ["service_fee_reconciliation", "system_service_fees", "系统服务费"],
  ] as const;
  const next = workflowTargets.find(([proposalType]) =>
    ["ready", "needs_confirmation", "blocked"].includes(states[proposalType]?.state ?? ""),
  );
  const pendingDocumentAction = ["confirm_analysis", "continue_analysis"].includes(
    projection.recommended_action.kind,
  ) && projection.recommended_action.target_type === "source"
    && sources.some((source) =>
      source.source_id === projection.recommended_action.target_id
      && source.state === "needs_action",
    );
  let recommendedAction = projection.recommended_action;
  if (next && !pendingDocumentAction) {
    const [proposalType, sourceType, label] = next;
    const hasProposal = Boolean(queue?.active[proposalType]);
    recommendedAction = {
      kind: "review_source_reconciliation",
      label: proposalType === "service_fee_reconciliation" && !hasProposal
        ? "生成系统服务费方案"
        : `继续${label}核对`,
      reason_code: `${proposalType}_requires_review`,
      target_type: "source",
      target_id: `source:${sourceType}`,
    };
  } else if (
    projection.recommended_action.target_type === "source"
    && sources.some((source) =>
      source.source_id === projection.recommended_action.target_id
      && source.state === "completed",
    )
  ) {
    recommendedAction = {
      kind: "none",
      label: "当前来源已对完",
      reason_code: "source_verified_current",
      target_type: "cycle",
      target_id: projection.cycle_id,
    };
  }
  return { ...projection, sources, recommended_action: recommendedAction };
}

