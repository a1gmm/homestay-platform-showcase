import { useEffect, useRef, useState } from "react";
import { Alert, Button, Card, Checkbox, Input, Space, Upload, message } from "antd";

import { MappingReviewDrawer } from "@/components/billing-recon/MappingReviewDrawer";
import { ProposalCard } from "./ProposalCard";
import { monthlyCloseApi } from "@/lib/api";
import type { ReconBatchDetail } from "@/lib/api";
import type { BillingReconConfirmInput, BillingReconFieldError, MappingCoordinates, ReconDiffOut, WorkbookAnalysis } from "@/lib/billing-recon";
import type { MonthlyCloseDocument, MonthlyCloseProposalView, OtaAppealSettlementCandidate, OtaOrderCandidate, OtaProposalDecision } from "@/lib/monthly-close";
import { extractErrorMessage } from "@/lib/api-errors";
import { confirmAndApplyMonthlyCloseProposal } from "@/lib/monthly-close-confirm-and-apply";

function mappingVersion(mapping: MappingCoordinates) {
  return JSON.stringify(mapping);
}

function selectionKey(values: string[]) {
  let first = 0x811c9dc5;
  let second = 0x9e3779b9;
  for (const character of values.join("\u001f")) {
    const code = character.codePointAt(0) ?? 0;
    first = Math.imul(first ^ code, 0x01000193);
    second = Math.imul(second ^ (code + 0x9e37), 0x85ebca6b);
  }
  return `${(first >>> 0).toString(16).padStart(8, "0")}${(second >>> 0).toString(16).padStart(8, "0")}`;
}

function ManualDecisionCard({
  billingMonth,
  batchId,
  diff,
  disabled,
  onDecision,
}: {
  billingMonth: string;
  batchId: string;
  diff: ReconDiffOut;
  disabled?: boolean;
  onDecision: (decision: OtaProposalDecision) => Promise<unknown>;
}) {
  const [query, setQuery] = useState("");
  const [candidates, setCandidates] = useState<OtaOrderCandidate[]>([]);
  const [selectedOrderId, setSelectedOrderId] = useState<string | null>(null);
  const [dismissReason, setDismissReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<string | null>(null);

  const search = async () => {
    const value = query.trim();
    if (!value || pending) return;
    setPending("search");
    setError(null);
    try {
      const response = await monthlyCloseApi.searchOtaOrderCandidates(
        billingMonth, batchId, diff.diff_id, value,
      );
      setCandidates(response.data.items);
      setSelectedOrderId(null);
    } catch (cause) {
      setError(extractErrorMessage(cause, "订单候选读取失败"));
    } finally {
      setPending(null);
    }
  };

  const submit = async (kind: "claim" | "dismiss") => {
    if (pending) return;
    if (kind === "claim" && !selectedOrderId) {
      setError("请先选择准确订单");
      return;
    }
    const reason = dismissReason.trim();
    if (kind === "dismiss" && !reason) {
      setError("请填写关闭原因");
      return;
    }
    setPending(kind);
    setError(null);
    try {
      await onDecision(kind === "claim" ? {
        diff_id: diff.diff_id,
        decision: "claim",
        order_id: selectedOrderId ?? undefined,
      } : {
        diff_id: diff.diff_id,
        decision: "dismiss",
        reason,
      });
    } catch (cause) {
      setError(extractErrorMessage(cause, "人工处置方案生成失败"));
    } finally {
      setPending(null);
    }
  };

  return (
    <Card size="small" title="需人工确认的 OTA 差异" style={{ borderRadius: 12 }}>
      <Space direction="vertical" style={{ width: "100%" }}>
        <div>账单金额：¥{diff.bill_amount ?? "—"}。可关联准确订单，或说明原因后关闭。</div>
        <Space.Compact style={{ width: "100%" }}>
          <Input
            aria-label="订单候选搜索"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="输入订单号或平台订单号"
            disabled={disabled || pending !== null}
          />
          <Button loading={pending === "search"} disabled={disabled || pending !== null} onClick={() => void search()}>
            搜索订单
          </Button>
        </Space.Compact>
        {candidates.map((candidate) => (
          <Button
            key={candidate.order_id}
            type={selectedOrderId === candidate.order_id ? "primary" : "default"}
            disabled={disabled || pending !== null}
            onClick={() => setSelectedOrderId(candidate.order_id)}
          >
            选择 {candidate.order_id} · {candidate.channel} · {candidate.check_in_date} 至 {candidate.check_out_date}
          </Button>
        ))}
        <Button disabled={disabled || pending !== null} loading={pending === "claim"} onClick={() => void submit("claim")}>
          提交认领方案
        </Button>
        <Input.TextArea
          aria-label="关闭原因"
          value={dismissReason}
          onChange={(event) => setDismissReason(event.target.value)}
          placeholder="关闭时必须填写核对原因"
          rows={2}
          disabled={disabled || pending !== null}
        />
        <Button danger disabled={disabled || pending !== null} loading={pending === "dismiss"} onClick={() => void submit("dismiss")}>
          提交关闭方案
        </Button>
        {error && <Alert type="error" showIcon message={error} />}
      </Space>
    </Card>
  );
}

const defaultAction: Record<string, string> = {
  fix_amount: "adopt",
  compensation: "adopt",
  appeal: "appeal",
  broken_link: "acknowledge",
  manual_review: "dismiss",
};

const diffClassLabel: Record<string, string> = {
  fix_amount: "到账金额调整",
  compensation: "赔款费用",
  appeal: "平台申诉",
  broken_link: "订单关联",
  manual_review: "人工关联",
};

function RejectedRecipeEditor({
  diffs,
  initialRecipe,
  disabled,
  onSubmit,
}: {
  diffs: ReconDiffOut[];
  initialRecipe?: { selected_diff_ids: string[]; decisions?: OtaProposalDecision[] } | null;
  disabled?: boolean;
  onSubmit: (recipe: { selected_diff_ids: string[]; decisions: OtaProposalDecision[] }) => Promise<unknown>;
}) {
  const [selected, setSelected] = useState(() => new Set(
    initialRecipe
      ? [
          ...initialRecipe.selected_diff_ids,
          ...(initialRecipe.decisions ?? []).map((decision) => decision.diff_id),
        ]
      : diffs.filter((diff) => diff.diff_class !== "manual_review").map((diff) => diff.diff_id),
  ));
  const [actions, setActions] = useState<Record<string, string>>(() => ({
    ...Object.fromEntries(diffs.map((diff) => [diff.diff_id, defaultAction[diff.diff_class]])),
    ...Object.fromEntries((initialRecipe?.decisions ?? []).map((decision) => [
      decision.diff_id,
      decision.decision === "claim"
        ? "claim"
        : decision.decision === "dismiss"
          ? "dismiss"
          : decision.action ?? defaultAction[
              diffs.find((diff) => diff.diff_id === decision.diff_id)?.diff_class ?? ""
            ] ?? "dismiss",
    ])),
  }));
  const [reasons, setReasons] = useState<Record<string, string>>(() => Object.fromEntries(
    (initialRecipe?.decisions ?? []).map((decision) => [decision.diff_id, decision.reason ?? ""]),
  ));
  const [claimTargets, setClaimTargets] = useState<Record<string, string>>(() => Object.fromEntries(
    (initialRecipe?.decisions ?? []).map((decision) => [decision.diff_id, decision.order_id ?? ""]),
  ));
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    if (!dirty) {
      setError("请先修改处置、关闭原因或选择范围，再生成新方案");
      return;
    }
    const chosen = diffs.filter((diff) => selected.has(diff.diff_id));
    if (chosen.length === 0) {
      setError("请至少选择一条差异");
      return;
    }
    const decisions: OtaProposalDecision[] = [];
    for (const diff of chosen) {
      const action = actions[diff.diff_id];
      if (action === "dismiss") {
        const reason = (reasons[diff.diff_id] ?? "").trim();
        if (!reason) {
          setError(`请填写 ${diff.diff_id} 的关闭原因`);
          return;
        }
        decisions.push({ diff_id: diff.diff_id, decision: "dismiss", reason });
      } else if (action === "claim") {
        const orderId = (claimTargets[diff.diff_id] ?? "").trim();
        if (!orderId) {
          setError(`请填写 ${diff.diff_id} 的准确订单号`);
          return;
        }
        decisions.push({ diff_id: diff.diff_id, decision: "claim", order_id: orderId });
      } else {
        decisions.push({ diff_id: diff.diff_id, decision: "action", action });
      }
    }
    setError(null);
    await onSubmit({ selected_diff_ids: [], decisions });
  };

  return (
    <Card size="small" title="修改被拒绝方案的处置" style={{ borderRadius: 12 }}>
      <Space direction="vertical" style={{ width: "100%" }}>
        <div>调整选择或处置后会创建新的方案和请求编号，不会重放已拒绝方案。</div>
        {diffs.map((diff) => {
          const action = actions[diff.diff_id];
          return (
            <div key={diff.diff_id} style={{ border: "1px solid #eee", borderRadius: 12, padding: 12 }}>
              <Checkbox
                aria-label={`选择 ${diff.diff_id}`}
                checked={selected.has(diff.diff_id)}
                disabled={disabled}
                onChange={(event) => {
                  setSelected((current) => {
                    const next = new Set(current);
                    if (event.target.checked) next.add(diff.diff_id); else next.delete(diff.diff_id);
                    return next;
                  });
                  setDirty(true);
                }}
              >
                {diffClassLabel[diff.diff_class] ?? "OTA差异"} · 账单金额 ¥{diff.bill_amount ?? "—"}
              </Checkbox>
              <select
                aria-label={`处置 ${diff.diff_id}`}
                value={action}
                disabled={disabled || !selected.has(diff.diff_id)}
                onChange={(event) => {
                  setActions((current) => ({ ...current, [diff.diff_id]: event.target.value }));
                  setDirty(true);
                }}
                style={{ minHeight: 44, width: "100%", marginTop: 8, borderRadius: 12 }}
              >
                {diff.diff_class === "fix_amount" || diff.diff_class === "compensation" ? <option value="adopt">采纳</option> : null}
                {diff.diff_class === "appeal" ? <option value="appeal">发起申诉</option> : null}
                {diff.diff_class === "broken_link" ? <option value="acknowledge">确认关联</option> : null}
                {diff.diff_class === "manual_review" ? <option value="claim">认领准确订单</option> : null}
                <option value="dismiss">关闭差异</option>
              </select>
              {action === "claim" && (
                <Input
                  aria-label={`${diff.diff_id} 认领订单`}
                  value={claimTargets[diff.diff_id] ?? ""}
                  disabled={disabled || !selected.has(diff.diff_id)}
                  onChange={(event) => {
                    setClaimTargets((current) => ({ ...current, [diff.diff_id]: event.target.value }));
                    setDirty(true);
                  }}
                  placeholder="填写上方搜索确认的准确订单号"
                  style={{ marginTop: 8 }}
                />
              )}
              {action === "dismiss" && (
                <Input.TextArea
                  aria-label={`${diff.diff_id} 关闭原因`}
                  value={reasons[diff.diff_id] ?? ""}
                  disabled={disabled || !selected.has(diff.diff_id)}
                  onChange={(event) => {
                    setReasons((current) => ({ ...current, [diff.diff_id]: event.target.value }));
                    setDirty(true);
                  }}
                  placeholder="关闭时必须填写核对原因"
                  rows={2}
                  style={{ marginTop: 8 }}
                />
              )}
            </div>
          );
        })}
        <div>需人工关联的差异可在上方改为认领准确订单或填写原因关闭。</div>
        {error && <Alert type="error" showIcon message={error} />}
        <Button type="primary" style={{ minHeight: 44, borderRadius: 12, fontWeight: 500 }} disabled={disabled} onClick={() => void submit()}>
          生成修改后的新方案
        </Button>
      </Space>
    </Card>
  );
}

function AppealAdjudicationControls({
  candidate,
  disabled,
  initialReason = "",
  initialChoiceId,
  titleOverride,
  onCreate,
}: {
  candidate: OtaAppealSettlementCandidate;
  disabled?: boolean;
  initialReason?: string;
  initialChoiceId?: string;
  titleOverride?: string;
  onCreate: (
    candidate: OtaAppealSettlementCandidate,
    action: "withdraw" | "confirm_identity",
    reason: string,
    identityChoiceId?: string,
  ) => Promise<unknown>;
}) {
  const currentChoice = candidate.identity_choices.find((item) => item.is_current_appeal);
  const [choiceId, setChoiceId] = useState(initialChoiceId ?? currentChoice?.choice_id ?? "");
  const [reason, setReason] = useState(initialReason);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<string | null>(null);
  const pendingRef = useRef<string | null>(null);

  const submit = async (action: "withdraw" | "confirm_identity") => {
    if (pendingRef.current) return;
    const checkedReason = reason.trim();
    if (!checkedReason) {
      setError("请填写申诉裁决原因");
      return;
    }
    if (action === "confirm_identity" && !choiceId) {
      setError("请选择准确订单和渠道");
      return;
    }
    pendingRef.current = action;
    setPending(action);
    setError(null);
    try {
      await onCreate(
        candidate,
        action,
        checkedReason,
        action === "confirm_identity" ? choiceId : undefined,
      );
    } catch (cause) {
      setError(extractErrorMessage(cause, "申诉裁决方案生成失败"));
    } finally {
      pendingRef.current = null;
      setPending(null);
    }
  };

  if (candidate.recovery_kind === "retain_appeal") {
    return <Alert type="warning" showIcon message="保留申诉，核对少付金额" description={candidate.action} />;
  }
  if (candidate.recovery_kind === "refresh_history") {
    return <Alert type="info" showIcon message="刷新并查看历史" description={candidate.action} />;
  }
  if (!["adjudicate_appeals", "confirm_identity"].includes(candidate.recovery_kind)) {
    return null;
  }
  const title = titleOverride ?? (candidate.recovery_kind === "adjudicate_appeals"
    ? "处理重复申诉"
    : "确认订单和渠道");
  return (
    <Card size="small" title={title} style={{ borderRadius: 12 }}>
      <Space direction="vertical" size="small" style={{ width: "100%" }}>
        <div>{candidate.action}</div>
        <select
          aria-label={`订单渠道选择 ${candidate.issue_id}`}
          value={choiceId}
          disabled={disabled || pending !== null}
          onChange={(event) => setChoiceId(event.target.value)}
          style={{ minHeight: 44, width: "100%", borderRadius: 12 }}
        >
          <option value="">请选择准确订单和渠道</option>
          {candidate.identity_choices.map((choice) => (
            <option key={choice.choice_id} value={choice.choice_id}>
              {choice.channel} · {choice.order_ref}
            </option>
          ))}
        </select>
        <Input.TextArea
          aria-label={`申诉裁决原因 ${candidate.issue_id}`}
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          placeholder="填写人工核对依据"
          rows={2}
          disabled={disabled || pending !== null}
        />
        <Button
          type="primary"
          loading={pending === "confirm_identity"}
          disabled={disabled || pending !== null}
          onClick={() => void submit("confirm_identity")}
        >
          {candidate.recovery_kind === "adjudicate_appeals" ? "保留此申诉" : "生成身份确认方案"}
        </Button>
        {candidate.recovery_kind === "adjudicate_appeals" && (
          <Button
            danger
            loading={pending === "withdraw"}
            disabled={disabled || pending !== null}
            onClick={() => void submit("withdraw")}
          >
            撤销错误申诉
          </Button>
        )}
        {error && <Alert type="error" showIcon message={error} />}
      </Space>
    </Card>
  );
}

type AdjudicationQueueBucket = "pending" | "rejected" | "verified";

function adjudicationBefore(proposal: MonthlyCloseProposalView) {
  const before = proposal.canonical_payload.commands[0]?.before;
  return before && typeof before === "object" ? before : null;
}

function adjudicationBucket(proposal: MonthlyCloseProposalView): AdjudicationQueueBucket {
  const verified = proposal.verifications?.some((item) => item.status === "passed")
    || proposal.attempts?.some((item) => item.status === "verified");
  if (verified) return "verified";
  if (
    ["rejected", "stale", "superseded"].includes(proposal.status)
    || proposal.attempts?.some((item) => [
      "failed_confirmed", "remediation_required", "unknown",
    ].includes(item.status))
  ) return "rejected";
  return "pending";
}

function normalizeAdjudicationQueue(items: MonthlyCloseProposalView[]) {
  const unique = new Map<string, MonthlyCloseProposalView>();
  for (const item of items) unique.set(item.proposal_id, item);
  const priority: Record<AdjudicationQueueBucket, number> = {
    pending: 0,
    rejected: 1,
    verified: 2,
  };
  return Array.from(unique.values()).sort((left, right) => {
    const bucket = priority[adjudicationBucket(left)] - priority[adjudicationBucket(right)];
    if (bucket !== 0) return bucket;
    const leftCreated = String((left as MonthlyCloseProposalView & { created_at?: string }).created_at ?? "");
    const rightCreated = String((right as MonthlyCloseProposalView & { created_at?: string }).created_at ?? "");
    return leftCreated.localeCompare(rightCreated) || left.proposal_id.localeCompare(right.proposal_id);
  });
}

export function OtaStatementPanel({
  billingMonth,
  documents,
  role = "admin",
  disabled,
  onFinished,
}: {
  billingMonth: string;
  documents: MonthlyCloseDocument[];
  role?: string;
  disabled?: boolean;
  onFinished: () => Promise<unknown>;
}) {
  const target = documents.find((document) => !document.engine_id) ?? null;
  const processed = documents.find((document) => document.engine_id) ?? null;
  const [analysis, setAnalysis] = useState<WorkbookAnalysis | null>(null);
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<BillingReconFieldError | null>(null);
  const [currentVersion, setCurrentVersion] = useState<string | null>(null);
  const [verifiedVersion, setVerifiedVersion] = useState<string | null>(null);
  const [recon, setRecon] = useState<ReconBatchDetail | null>(null);
  const [proposal, setProposal] = useState<MonthlyCloseProposalView | null>(null);
  const [proposalBillingMonth, setProposalBillingMonth] = useState(billingMonth);
  const [proposalLoading, setProposalLoading] = useState(false);
  const [proposalError, setProposalError] = useState<string | null>(null);
  const [editingRejected, setEditingRejected] = useState(false);
  const [adjudicationProposals, setAdjudicationProposals] = useState<MonthlyCloseProposalView[]>([]);
  const [editingRejectedAdjudicationId, setEditingRejectedAdjudicationId] = useState<string | null>(null);
  const [proposalRecipe, setProposalRecipe] = useState<{
    selected_diff_ids: string[];
    decisions?: OtaProposalDecision[];
  } | null>(null);
  const [settlementCandidates, setSettlementCandidates] = useState<OtaAppealSettlementCandidate[]>([]);
  const [settlementPending, setSettlementPending] = useState<string | null>(null);
  const [replacementPending, setReplacementPending] = useState(false);
  const settlementPendingRef = useRef<string | null>(null);
  const replacementPendingRef = useRef(false);
  const settlementRequestNo = useRef(0);
  const requestRef = useRef(0);
  const proposalRequestRef = useRef<string | null>(null);
  const proposalRequestRecipeRef = useRef<string | null>(null);
  const proposalGenerationRef = useRef(0);

  useEffect(() => {
    const batchId = processed?.engine_id;
    if (!batchId) return;
    let active = true;
    setRecon(null);
    setProposal(null);
    setProposalBillingMonth(billingMonth);
    proposalRequestRef.current = null;
    proposalRequestRecipeRef.current = null;
    setProposalRecipe(null);
    setProposalError(null);
    setEditingRejected(false);
    setAdjudicationProposals([]);
    setEditingRejectedAdjudicationId(null);
    setSettlementCandidates([]);
    void monthlyCloseApi.getOtaBatch(billingMonth, batchId).then((response) => {
      if (active) setRecon(response.data);
    }).catch((cause) => {
      if (active) setProposalError(extractErrorMessage(cause, "OTA对账批次读取失败"));
    });
    void monthlyCloseApi.getOtaAppealSettlementCandidates(billingMonth).then((response) => {
      if (active) setSettlementCandidates(response.data.items);
    }).catch((cause) => {
      if (active) setProposalError(extractErrorMessage(cause, "历史申诉到账证据读取失败"));
    });
    void monthlyCloseApi.listOtaAppealAdjudicationProposals(billingMonth).then((response) => {
      if (!active) return;
      setAdjudicationProposals(normalizeAdjudicationQueue(response.data.items));
    }).catch((cause) => {
      if (active) setProposalError(extractErrorMessage(cause, "申诉裁决方案读取失败"));
    });
    return () => { active = false; };
  }, [billingMonth, processed?.engine_id]);

  const actionable = (recon?.diffs ?? []).filter((diff: ReconDiffOut) => (
    diff.status === "pending"
    && ["fix_amount", "compensation", "appeal", "broken_link"].includes(diff.diff_class)
  ));
  const manual = (recon?.diffs ?? []).filter((diff: ReconDiffOut) => (
    diff.status === "pending" && diff.diff_class === "manual_review"
  ));
  const editableDiffs = [...actionable, ...manual];

  const refreshProposal = async (proposalId: string) => {
    const response = await monthlyCloseApi.getProposal(proposalBillingMonth, proposalId);
    setProposal(response.data);
    return response.data;
  };

  const refreshAdjudicationProposal = async (item: MonthlyCloseProposalView) => {
    const proposalBillingMonth = item.proposal_billing_month ?? billingMonth;
    const response = await monthlyCloseApi.getProposal(
      proposalBillingMonth,
      item.proposal_id,
    );
    setAdjudicationProposals((current) => normalizeAdjudicationQueue([
      ...current.filter((proposalItem) => proposalItem.proposal_id !== item.proposal_id),
      {
        ...response.data,
        proposal_billing_month: response.data.proposal_billing_month ?? proposalBillingMonth,
      },
    ]));
    return response.data;
  };

  const createProposal = async (
    recipe?: { selected_diff_ids: string[]; decisions?: OtaProposalDecision[] },
    regenerate = false,
  ) => {
    if (!recon || proposalLoading) return;
    const selected = actionable.map((diff) => diff.diff_id).sort();
    const nextRecipe = recipe ?? { selected_diff_ids: selected };
    if (nextRecipe.selected_diff_ids.length === 0 && !nextRecipe.decisions?.length) return;
    const recipeKey = selectionKey([
      ...nextRecipe.selected_diff_ids,
      JSON.stringify(nextRecipe.decisions ?? []),
    ]);
    if (regenerate || proposalRequestRecipeRef.current !== recipeKey) {
      proposalRequestRef.current = null;
      proposalRequestRecipeRef.current = recipeKey;
    }
    if (!proposalRequestRef.current) {
      proposalGenerationRef.current += 1;
      proposalRequestRef.current = `ota:${recon.batch.batch_id}:propose:${recipeKey}:${proposalGenerationRef.current}`;
    }
    setProposalLoading(true);
    setProposalError(null);
    try {
      const response = await monthlyCloseApi.createOtaProposal(billingMonth, {
        batch_id: recon.batch.batch_id,
        ...nextRecipe,
        request_id: proposalRequestRef.current,
      });
      setProposalRecipe(nextRecipe);
      setProposal(response.data);
      setProposalBillingMonth(billingMonth);
      setEditingRejected(false);
    } catch (cause) {
      setProposalError(extractErrorMessage(cause, "OTA调账方案生成失败"));
    } finally {
      setProposalLoading(false);
    }
  };

  const reconcileAppeal = async (candidate: OtaAppealSettlementCandidate) => {
    if (settlementPendingRef.current || !candidate.actionable || role !== "admin") return;
    settlementPendingRef.current = candidate.candidate_id;
    setSettlementPending(candidate.candidate_id);
    setProposalError(null);
    settlementRequestNo.current += 1;
    const requestId = `ota:${candidate.issue_id}:settle:${settlementRequestNo.current}:${Date.now()}`;
    try {
      await monthlyCloseApi.reconcileSettledOtaAppeal(
        billingMonth, candidate.issue_id, requestId,
      );
      const response = await monthlyCloseApi.getOtaAppealSettlementCandidates(billingMonth);
      setSettlementCandidates(response.data.items);
      await onFinished();
    } catch (cause) {
      setProposalError(extractErrorMessage(cause, "历史申诉到账核销失败"));
    } finally {
      settlementPendingRef.current = null;
      setSettlementPending(null);
    }
  };

  const createAppealAdjudication = async (
    candidate: OtaAppealSettlementCandidate,
    action: "withdraw" | "confirm_identity",
    reason: string,
    identityChoiceId?: string,
  ) => {
    settlementRequestNo.current += 1;
    const requestId = `ota:${candidate.issue_id}:adjudicate:${selectionKey([
      candidate.candidate_id,
      action,
      identityChoiceId ?? "",
      reason,
    ])}:${settlementRequestNo.current}`;
    const response = await monthlyCloseApi.createOtaAppealAdjudicationProposal(
      billingMonth,
      {
        candidate_id: candidate.candidate_id,
        action,
        ...(identityChoiceId ? { identity_choice_id: identityChoiceId } : {}),
        reason,
        request_id: requestId,
      },
    );
    setAdjudicationProposals((current) => normalizeAdjudicationQueue([
      ...current.filter((item) => item.proposal_id !== response.data.proposal_id),
      {
        ...response.data,
        proposal_billing_month: response.data.proposal_billing_month
          ?? candidate.original.billing_month,
      },
    ]));
    setEditingRejectedAdjudicationId(null);
  };

  const replaceAmbiguousStatement = async (
    candidate: OtaAppealSettlementCandidate,
    file: File,
  ) => {
    if (!candidate.later_document_id || replacementPendingRef.current || role !== "admin") return;
    replacementPendingRef.current = true;
    setReplacementPending(true);
    setProposalError(null);
    try {
      await monthlyCloseApi.archiveOtaAppealCandidateDocument(
        billingMonth,
        candidate.later_document_id,
        candidate.candidate_id,
      );
      await monthlyCloseApi.uploadDocument(billingMonth, "ota_statement", file);
      message.success("修正版已上传，请重新分析并确认字段");
      await onFinished();
    } catch (cause) {
      setProposalError(extractErrorMessage(cause, "归档或上传修正版失败"));
    } finally {
      replacementPendingRef.current = false;
      setReplacementPending(false);
    }
  };

  const analyze = async (coordinates?: MappingCoordinates) => {
    if (!target) return;
    const request = requestRef.current + 1;
    requestRef.current = request;
    setLoading(true);
    setError(null);
    try {
      const response = await monthlyCloseApi.analyzeOta(billingMonth, target.document_id, coordinates);
      if (request !== requestRef.current) return;
      setAnalysis(response.data);
      const version = mappingVersion(response.data.coordinates);
      setCurrentVersion(version);
      setVerifiedVersion(version);
      setOpen(true);
    } catch (cause) {
      if (request !== requestRef.current) return;
      setError({ code: "AI_UNAVAILABLE", message: extractErrorMessage(cause, "账单分析失败，请稍后重试"), field: "file" });
      setOpen(true);
    } finally {
      if (request === requestRef.current) setLoading(false);
    }
  };

  const adjudicationQueue = normalizeAdjudicationQueue(adjudicationProposals);
  const adjudicationGroups = Array.from(adjudicationQueue.reduce((groups, item) => {
    const before = adjudicationBefore(item);
    const issueId = typeof before?.issue_id === "string" ? before.issue_id : "unknown-issue";
    const candidateId = typeof before?.candidate_id === "string" ? before.candidate_id : "unknown-candidate";
    const key = `${issueId}:${candidateId}`;
    const group = groups.get(key) ?? { key, issueId, candidateId, items: [] as MonthlyCloseProposalView[] };
    group.items.push(item);
    groups.set(key, group);
    return groups;
  }, new Map<string, {
    key: string;
    issueId: string;
    candidateId: string;
    items: MonthlyCloseProposalView[];
  }>()).values());
  const queuedAdjudicationCandidateIds = new Set(
    adjudicationQueue
      .map((item) => adjudicationBefore(item)?.candidate_id)
      .filter((item): item is string => typeof item === "string"),
  );
  const editingAdjudicationProposal = adjudicationQueue.find(
    (item) => item.proposal_id === editingRejectedAdjudicationId,
  ) ?? null;
  const editingAdjudicationBefore = editingAdjudicationProposal
    ? adjudicationBefore(editingAdjudicationProposal)
    : null;
  const editingAdjudicationCandidate = settlementCandidates.find(
    (candidate) => candidate.candidate_id === editingAdjudicationBefore?.candidate_id,
  ) ?? null;

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      {target && (
        <Button aria-label={`查看 ${target.filename} 的识别结果`} style={{ minHeight: 44, borderRadius: 12 }} disabled={disabled} loading={loading} onClick={() => void analyze()}>
          查看识别结果
        </Button>
      )}
      {!target && documents.length > 0 && !recon && !proposalError && (
        <Alert type="info" showIcon message="正在读取已生成的OTA对账批次" />
      )}
      {settlementCandidates.length > 0 && (
        <Card
          size="small"
          title={`发现 ${settlementCandidates.length} 笔历史申诉到账证据`}
          style={{ borderRadius: 12 }}
        >
          <Space direction="vertical" size="small" style={{ width: "100%" }}>
            {settlementCandidates.map((candidate) => (
              <section
                key={candidate.candidate_id}
                aria-label={`历史申诉 ${candidate.original.billing_month}`}
                style={{ border: "1px solid #eee", borderRadius: 12, padding: 12 }}
              >
                <Space direction="vertical" size={6} style={{ width: "100%" }}>
                  <div>原申诉月份：{candidate.original.billing_month} · 本期到账：¥{candidate.settled_amount ?? "—"}</div>
                  {!candidate.actionable && candidate.recovery_kind === "archive_reupload" && (
                    <>
                      <Alert type="warning" showIcon message={candidate.action} />
                      {role === "admin" && (
                        <Card size="small" title="修正到账证据" style={{ borderRadius: 12 }}>
                          <Space direction="vertical" size="small" style={{ width: "100%" }}>
                            <div>处理方法：只归档这条候选绑定的到账账单，再上传修正版并重新确认字段。</div>
                            <Upload
                              accept=".xls,.xlsx"
                              showUploadList={false}
                              disabled={disabled || replacementPending}
                              beforeUpload={(file) => {
                                void replaceAmbiguousStatement(candidate, file as File);
                                return false;
                              }}
                            >
                              <Button
                                loading={replacementPending}
                                disabled={disabled || replacementPending}
                                style={{ minHeight: 44, borderRadius: 12 }}
                              >
                                归档此到账账单并上传修正版
                              </Button>
                            </Upload>
                          </Space>
                        </Card>
                      )}
                    </>
                  )}
                  {candidate.actionable && role === "admin" && (
                    <Button
                      type="primary"
                      style={{ minHeight: 44, borderRadius: 12, fontWeight: 500 }}
                      disabled={disabled || settlementPending !== null}
                      aria-busy={settlementPending === candidate.candidate_id}
                      onClick={() => void reconcileAppeal(candidate)}
                    >
                      {candidate.action}
                    </Button>
                  )}
                  {candidate.actionable && role !== "admin" && (
                    <Alert type="info" showIcon message="到账证据已就绪，请管理员确认核销" />
                  )}
                  {!candidate.actionable && !queuedAdjudicationCandidateIds.has(candidate.candidate_id) && (
                    <AppealAdjudicationControls
                      candidate={candidate}
                      disabled={disabled}
                      onCreate={createAppealAdjudication}
                    />
                  )}
                </Space>
              </section>
            ))}
          </Space>
        </Card>
      )}
      {manual.length > 0 && (
        <Alert
          type="warning"
          showIcon
          message={`${manual.length} 条需先人工关联或选择处置方式`}
          description="这些差异不会被隐藏或自动忽略，也不会进入本次调账方案。"
        />
      )}
      {recon && manual.map((diff) => (
        <ManualDecisionCard
          key={diff.diff_id}
          billingMonth={billingMonth}
          batchId={recon.batch.batch_id}
          diff={diff}
          disabled={disabled || proposalLoading}
          onDecision={(decision) => createProposal({ selected_diff_ids: [], decisions: [decision] })}
        />
      ))}
      {recon && actionable.length > 0 && !proposal && (
        <Button style={{ minHeight: 44, borderRadius: 12, fontWeight: 500 }} disabled={disabled} loading={proposalLoading} onClick={() => void createProposal()}>
          生成 {actionable.length} 条 OTA 调账方案
        </Button>
      )}
      {recon && actionable.length === 0 && manual.length === 0 && !proposal && (
        <Alert type="info" showIcon message="当前批次没有可生成方案的待处理差异" />
      )}
      {proposalError && <Alert type="error" showIcon message={proposalError} />}
      {proposal?.status === "rejected" && proposal.proposal_type !== "ota_appeal_adjudication" && editingRejected && editableDiffs.length > 0 && (
        <RejectedRecipeEditor
          diffs={editableDiffs}
          initialRecipe={proposalRecipe}
          disabled={disabled || proposalLoading}
          onSubmit={(recipe) => createProposal(recipe, true)}
        />
      )}
      {editingAdjudicationProposal?.status === "rejected" && editingAdjudicationCandidate && (
        <AppealAdjudicationControls
          key={`${editingAdjudicationProposal.proposal_id}:edit`}
          candidate={editingAdjudicationCandidate}
          disabled={disabled}
          initialReason={typeof editingAdjudicationBefore?.reason === "string" ? editingAdjudicationBefore.reason : ""}
          initialChoiceId={
            editingAdjudicationBefore?.selected_identity
            && typeof editingAdjudicationBefore.selected_identity === "object"
            && typeof (editingAdjudicationBefore.selected_identity as Record<string, unknown>).choice_id === "string"
              ? (editingAdjudicationBefore.selected_identity as Record<string, unknown>).choice_id as string
              : undefined
          }
          titleOverride="修改被拒绝的申诉裁决"
          onCreate={createAppealAdjudication}
        />
      )}
      {adjudicationQueue.length > 0 && (
        <Card size="small" title="申诉裁决方案队列" style={{ borderRadius: 12 }}>
          <Space direction="vertical" size="middle" style={{ width: "100%" }}>
            {([
              ["pending", "待审批方案"],
              ["rejected", "被拒绝方案"],
              ["verified", "已复核历史"],
            ] as const).map(([bucket, label]) => {
              const bucketGroups = adjudicationGroups
                .map((group) => ({
                  ...group,
                  items: group.items.filter((item) => adjudicationBucket(item) === bucket),
                }))
                .filter((group) => group.items.length > 0);
              if (bucketGroups.length === 0) return null;
              return (
                <section key={bucket} aria-label={label}>
                  <div style={{ fontWeight: 500, marginBottom: 8 }}>{label}</div>
                  <Space direction="vertical" size="middle" style={{ width: "100%" }}>
                    {bucketGroups.map((group) => (
                      <section
                        key={`${bucket}:${group.key}`}
                        aria-label={`申诉问题 ${group.issueId}`}
                        style={{ border: "1px solid #eee", borderRadius: 12, padding: 12 }}
                      >
                        <div style={{ marginBottom: 8 }}>
                          问题 {group.issueId} · 候选 {group.candidateId}
                        </div>
                        <Space direction="vertical" size="middle" style={{ width: "100%" }}>
                          {group.items.map((item) => {
                            const itemMonth = item.proposal_billing_month ?? billingMonth;
                            return (
                              <ProposalCard
                                key={item.proposal_id}
                                proposal={item}
                                role={role}
                                onApprove={async (requestId) => {
                                  await monthlyCloseApi.approveProposal(itemMonth, item.proposal_id, requestId);
                                  await refreshAdjudicationProposal(item);
                                }}
                                onReject={async (reason, requestId) => {
                                  await monthlyCloseApi.rejectProposal(itemMonth, item.proposal_id, reason, requestId);
                                  await refreshAdjudicationProposal(item);
                                }}
                                onExecute={async (requestId) => {
                                  await monthlyCloseApi.executeProposal(itemMonth, item.proposal_id, requestId);
                                  await refreshAdjudicationProposal(item);
                                }}
                                onVerify={async (attemptId, requestId) => {
                                  await monthlyCloseApi.verifyAttempt(itemMonth, attemptId, requestId);
                                  await refreshAdjudicationProposal(item);
                                  await onFinished();
                                }}
                                onConfirmAndApply={async () => {
                                  await confirmAndApplyMonthlyCloseProposal(itemMonth, item);
                                  await refreshAdjudicationProposal(item);
                                  await onFinished();
                                }}
                                onRegenerate={async () => setEditingRejectedAdjudicationId(item.proposal_id)}
                                onEditRecipe={() => setEditingRejectedAdjudicationId(item.proposal_id)}
                              />
                            );
                          })}
                        </Space>
                      </section>
                    ))}
                  </Space>
                </section>
              );
            })}
          </Space>
        </Card>
      )}
      {proposal && (
        <ProposalCard
          proposal={proposal}
          role={role}
          onApprove={async (requestId) => {
            await monthlyCloseApi.approveProposal(proposalBillingMonth, proposal.proposal_id, requestId);
            await refreshProposal(proposal.proposal_id);
          }}
          onReject={async (reason, requestId) => {
            await monthlyCloseApi.rejectProposal(proposalBillingMonth, proposal.proposal_id, reason, requestId);
            await refreshProposal(proposal.proposal_id);
          }}
          onExecute={async (requestId) => {
            await monthlyCloseApi.executeProposal(proposalBillingMonth, proposal.proposal_id, requestId);
            await refreshProposal(proposal.proposal_id);
          }}
          onVerify={async (attemptId, requestId) => {
            await monthlyCloseApi.verifyAttempt(proposalBillingMonth, attemptId, requestId);
            await refreshProposal(proposal.proposal_id);
            await onFinished();
          }}
          onConfirmAndApply={async () => {
            await confirmAndApplyMonthlyCloseProposal(proposalBillingMonth, proposal);
            await refreshProposal(proposal.proposal_id);
            await onFinished();
          }}
          onRegenerate={async () => {
            if (!proposalRecipe) return;
            await createProposal(proposalRecipe, true);
          }}
          onEditRecipe={() => setEditingRejected(true)}
        />
      )}
      <MappingReviewDrawer
        open={open}
        analysis={analysis}
        reviewMode="assistant"
        documentName={target?.filename ?? "OTA 账单"}
        role={role}
        analysisCurrent={currentVersion !== null && currentVersion === verifiedVersion}
        loading={loading}
        error={error}
        onClose={() => setOpen(false)}
        onMappingChange={(coordinates) => setCurrentVersion(mappingVersion(coordinates))}
        onReanalyze={(coordinates) => void analyze(coordinates)}
        onConfirm={async (input: Omit<BillingReconConfirmInput, "file" | "file_fingerprint">) => {
          if (!target) return;
          setLoading(true);
          setError(null);
          try {
            const response = await monthlyCloseApi.confirmOta(billingMonth, target.document_id, input);
            message.success(`已生成 ${response.data.batch.bill_month} OTA对账批次`);
            setRecon(response.data);
            setOpen(false);
            await onFinished();
          } catch (cause) {
            setError({ code: "MAPPING_INCOMPLETE", message: extractErrorMessage(cause, "确认对账失败"), field: "mapping_json" });
          } finally {
            setLoading(false);
          }
        }}
      />
    </Space>
  );
}
