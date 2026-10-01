"use client";

import { useLayoutEffect, useRef } from "react";
import { getPersistedAuthSessionId, useAuthStore } from "@/lib/auth";
import { Alert } from "antd";

import { monthlyCloseApi } from "@/lib/api";
import { confirmAndApplyMonthlyCloseProposal, monthlyCloseProposalScopeGuard } from "@/lib/monthly-close-confirm-and-apply";
import type { MonthlyCloseProposalView } from "@/lib/monthly-close";
import { ProposalCard } from "./ProposalCard";

export type SourceProgressState = "正在分析" | "需要确认" | "已对完" | "发现差异";

export function SourceProgressAlert({ state }: { state: SourceProgressState }) {
  return (
    <Alert
      type={state === "已对完" ? "success" : state === "发现差异" ? "warning" : "info"}
      showIcon
      message={`已保存 → 正在分析 → 需要确认 → 已对完/发现差异 · 当前：${state}`}
    />
  );
}

export function SourceProposalCard({
  billingMonth,
  proposal,
  role,
  onProposal,
  onFinished,
}: {
  billingMonth: string;
  proposal: MonthlyCloseProposalView;
  role: string;
  onProposal: (proposal: MonthlyCloseProposalView) => void;
  onFinished: () => Promise<unknown>;
}) {
  const authScope = useAuthStore((state) => `${state.session_id}:${state.user?.user_id}:${state.user?.role}`);
  const operationController = useRef<AbortController | null>(null);
  useLayoutEffect(() => {
    const controller = new AbortController();
    operationController.current = controller;
    const initialSession = getPersistedAuthSessionId();
    // Cancel immediately on account changes, before any queued Axios interceptor
    // can attach the new account's credentials to an old operation.
    const unsubscribe = useAuthStore.subscribe((state) => {
      if (`${state.session_id}:${state.user?.user_id}:${state.user?.role}` !== authScope) controller.abort();
    });
    const onStorage = () => { if (getPersistedAuthSessionId() !== initialSession) controller.abort(); };
    window.addEventListener("storage", onStorage);
    return () => { unsubscribe(); window.removeEventListener("storage", onStorage); controller.abort(); };
  }, [authScope, billingMonth, proposal.proposal_id, role]);
  const refresh = async (signal: AbortSignal, assertCurrent: () => void) => {
    assertCurrent();
    const response = await monthlyCloseApi.getProposal(
      billingMonth,
      proposal.proposal_id,
      signal,
    );
    assertCurrent();
    onProposal(response.data);
    await onFinished();
    assertCurrent();
  };
  const scopedOperation = async (operation: (signal: AbortSignal) => Promise<unknown>) => {
    const controller = operationController.current;
    if (!controller) return;
    const assertCurrent = monthlyCloseProposalScopeGuard(controller.signal);
    assertCurrent();
    await operation(controller.signal);
    assertCurrent();
    await refresh(controller.signal, assertCurrent);
  };
  const attempt = proposal.attempts?.at(-1);
  const verification = proposal.verifications
    ?.filter((item) => !attempt || item.attempt_id === attempt.attempt_id)
    .at(-1);
  const projectedState = proposal.source_state;
  const state: SourceProgressState = projectedState === "completed"
    ? "已对完"
    : projectedState === "needs_confirmation" || projectedState === "ready"
      ? "需要确认"
      : projectedState === "blocked"
        ? "发现差异"
        : verification?.status === "verified" || verification?.status === "passed"
          ? "已对完"
    : verification?.status === "failed" || verification?.status === "inconclusive"
      ? "发现差异"
      : proposal.status === "pending_approval"
        ? "需要确认"
        : "正在分析";
  const nextAction = state === "需要确认"
    ? "请先处理当前来源仍未关闭的差异，再完成本来源核对。"
    : state === "发现差异"
      ? "请按当前证据重新检查并生成新方案。"
      : null;

  return (
    <div style={{ display: "grid", gap: 12, width: "100%" }}>
      <SourceProgressAlert state={state} />
      {nextAction && <Alert type="info" showIcon message={nextAction} />}
      <ProposalCard
        proposal={proposal}
        role={role}
        sourceCompletionState={
          state === "已对完"
            ? "completed"
            : state === "发现差异"
              ? "difference"
              : "needs_confirmation"
        }
        onApprove={role === "admin" ? (requestId) => scopedOperation((signal) =>
          monthlyCloseApi.approveProposal(billingMonth, proposal.proposal_id, requestId, signal)) : undefined}
        onReject={role === "admin" ? (reason, requestId) => scopedOperation((signal) =>
          monthlyCloseApi.rejectProposal(billingMonth, proposal.proposal_id, reason, requestId, signal)) : undefined}
        onExecute={role === "admin" ? (requestId) => scopedOperation((signal) =>
          monthlyCloseApi.executeProposal(billingMonth, proposal.proposal_id, requestId, signal)) : undefined}
        onVerify={role === "admin" ? (attemptId, requestId) => scopedOperation((signal) =>
          monthlyCloseApi.verifyAttempt(billingMonth, attemptId, requestId, signal)) : undefined}
        onConfirmAndApply={role === "admin" ? () => scopedOperation((signal) =>
          confirmAndApplyMonthlyCloseProposal(billingMonth, proposal, signal)) : undefined}
      />
    </div>
  );
}
