"use client";

import { useEffect, useRef, useState } from "react";
import { Button, Modal } from "antd";
import { monthlyCloseApi } from "@/lib/api";
import { extractErrorMessage } from "@/lib/api-errors";
import { getPersistedAuthSessionId, useAuthStore } from "@/lib/auth";
import { confirmAndApplyMonthlyCloseProposal, monthlyCloseProposalScopeGuard } from "@/lib/monthly-close-confirm-and-apply";
import type { MonthlyCloseProjection, MonthlyCloseProposalView, MonthlyCloseStep } from "@/lib/monthly-close";
import { SettlementDelivery } from "./SettlementDelivery";
import { CloseReadinessSummary } from "./CloseReadinessSummary";

export function FinishMonthReview({ projection, steps, onFinished, onOpenStep, onClose }: {
  projection: MonthlyCloseProjection; steps: MonthlyCloseStep[];
  onFinished: () => Promise<unknown>; onOpenStep: (key: string) => void;
  onClose?: () => void;
}) {
  const [proposal, setProposal] = useState<MonthlyCloseProposalView | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [verified, setVerified] = useState(false);
  const [showCompanyClose, setShowCompanyClose] = useState(() => !(projection.final_review.settlement_count > 0 && projection.final_review.confirmed_settlement_count === projection.final_review.settlement_count));
  const controller = useRef<AbortController | null>(null);
  const inflight = useRef(false);
  const preparedId = useRef<string | null>(null);
  const callbacks = useRef({ projection, onFinished });
  callbacks.current = { projection, onFinished };
  const review = projection.final_review;
  const complete = review.state === "verified" || verified;
  const pendingSteps = steps.filter((step) => step.status !== "confirmed");
  const hasBlockers = projection.final_close_blockers.length > 0 || pendingSteps.length > 0;
  const allowed = projection.actor_role === "admin" && projection.can_advance_final_review;

  const run = async (confirm: boolean) => {
    const signal = controller.current?.signal;
    if (!signal || signal.aborted || inflight.current || !allowed || complete) return;
    inflight.current = true;
    setBusy(true); setError(null);
    const assertCurrent = monthlyCloseProposalScopeGuard(signal);
    try {
      assertCurrent();
      const current = callbacks.current.projection;
      let selected = proposal;
      if (!confirm) {
        const state = current.final_review.state;
        const existing = preparedId.current ?? current.final_review.proposal_id;
        selected = (existing && (preparedId.current || !["not_started", "stale", "failed", "reopened"].includes(state))
          ? await monthlyCloseApi.getProposal(current.billing_month, existing, signal)
          : await monthlyCloseApi.createFinalizationProposal(current.billing_month, `finish:${crypto.randomUUID()}`, signal)).data;
        assertCurrent();
        if (selected && ["stale", "superseded", "rejected"].includes(selected.status)) {
          selected = (await monthlyCloseApi.createFinalizationProposal(current.billing_month, `finish:${crypto.randomUUID()}`, signal)).data;
        }
      }
      assertCurrent();
      if (!selected || selected.cycle_id !== current.cycle_id || selected.proposal_type !== "finalize_monthly_close"
        || selected.canonical_payload.commands.length !== 1 || selected.canonical_payload.commands[0].command_type !== "finalize_monthly_close") {
        throw new Error("结束方案与当前月份不一致，请重新检查。");
      }
      setProposal(selected);
      preparedId.current = selected.proposal_id;
      const savedAttempt = selected.attempts?.at(-1);
      const savedVerification = selected.verifications?.filter((item) => item.attempt_id === savedAttempt?.attempt_id).at(-1);
      if (!confirm && ["verified", "passed"].includes(savedVerification?.status ?? "")) {
        setVerified(true);
        await callbacks.current.onFinished();
      }
      if (confirm) {
        const attempt = selected.attempts?.at(-1);
        if (attempt?.status === "unknown") {
          await monthlyCloseApi.verifyAttempt(current.billing_month, attempt.attempt_id, `confirm:${selected.proposal_id}:verify:${attempt.attempt_id}`, signal);
        } else {
          await confirmAndApplyMonthlyCloseProposal(current.billing_month, selected, signal);
        }
        assertCurrent();
        const latest = (await monthlyCloseApi.getProposal(current.billing_month, selected.proposal_id, signal)).data;
        assertCurrent();
        setProposal(latest);
        const latestAttempt = latest.attempts?.at(-1);
        const result = latest.verifications?.filter((item) => item.attempt_id === latestAttempt?.attempt_id).at(-1);
        if (!["verified", "passed"].includes(result?.status ?? "")) throw new Error("结束结果尚未核验通过。已完成的操作会保留，请重新检查当前状态。");
        setVerified(true);
        await callbacks.current.onFinished();
      }
    } catch (cause) {
      if (!signal.aborted) { setError(cause instanceof Error && !("isAxiosError" in cause) ? cause.message : extractErrorMessage(cause, "暂时无法结束，请重新检查。已完成的操作会保留。")); setProposal(null); }
    } finally {
      inflight.current = false;
      if (!signal.aborted) setBusy(false);
    }
  };
  const initialRun = useRef(run);
  const initialConditions = useRef({ hasBlockers, complete });
  useEffect(() => {
    const abort = new AbortController(); controller.current = abort;
    const session = getPersistedAuthSessionId();
    const actor = useAuthStore.getState();
    const unsubscribe = useAuthStore.subscribe((next) => {
      if (next.session_id !== actor.session_id || next.user?.user_id !== actor.user?.user_id || next.user?.role !== actor.user?.role) abort.abort();
    });
    const storage = () => { if (getPersistedAuthSessionId() !== session) abort.abort(); };
    window.addEventListener("storage", storage);
    // Deferring also avoids duplicate preparation during React's effect replay.
    const timer = window.setTimeout(() => { if (!initialConditions.current.hasBlockers && !initialConditions.current.complete) void initialRun.current(false); }, 0);
    return () => { clearTimeout(timer); unsubscribe(); window.removeEventListener("storage", storage); abort.abort(); };
  }, []);

  const rows = proposal?.canonical_payload.commands[0]?.after.settlements;
  const snapshots = Array.isArray(rows) ? rows as Array<Record<string, unknown>> : [];
  const total = snapshots.length ? snapshots.reduce((sum, row) => sum + Number(row.actual_owner_amount ?? 0), 0) : Number(review.settlement_total_amount);
  const amount = Number.isFinite(total) ? total.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : "待核实";
  const attempt = proposal?.attempts?.at(-1);
  const canConfirm = proposal && (["pending_approval", "approved"].includes(proposal.status) && !attempt || ["succeeded_unverified", "unknown"].includes(attempt?.status ?? ""));
  const ownerConfirmed = review.settlement_count > 0 && (review.confirmed_settlement_count ?? 0) === review.settlement_count;
  const showActions = !hasBlockers || showCompanyClose || Boolean(proposal) || Boolean(error);
  return <Modal open title={`${projection.billing_month} · 结算与收尾`} footer={null} onCancel={onClose} maskClosable={!busy} keyboard={!busy} closable={!busy}
    width={640} style={{ top: 32 }} styles={{ body: { maxHeight: "calc(100dvh - 160px)", overflowY: "auto" } }}>
    <section aria-label="结束本月对账" style={{ paddingTop: 20, display: "grid", gap: 16, minWidth: 0 }}>
    <h2 style={{ fontSize: 22, fontWeight: 500, margin: 0 }}>{complete ? `${projection.billing_month} 对账已结束` : ownerConfirmed ? "本月业主结算已确认" : "本月结算进度"}</h2>
    {complete ? <p role="status">本月账目已核验并归档。结算单和订单明细仍可查看；后续有调整时，请通过重新打开月结处理。实际付款请按打款情况另行登记。</p> : <>
      <p style={{ margin: 0 }}>本月 {snapshots.length || review.settlement_count} 份业主结算，应付合计 ¥{amount}。</p>
      <p style={{ margin: 0 }}>{ownerConfirmed ? "业主账单已保存，无需为结束对账再生成或重复确认。" : `系统已确认 ${review.confirmed_settlement_count ?? 0} 份；生成账单不代表业主已确认。`}</p>
      <Button style={{ minHeight: 44 }} type={ownerConfirmed && hasBlockers && !showCompanyClose ? "primary" : "default"} href={`/settlements?month=${encodeURIComponent(projection.billing_month)}`}>查看结算单与订单明细</Button>
      <p style={{ margin: 0, color: "var(--stone)" }}>已登记付款 {review.paid_settlement_count ?? 0} 份；付款状态以实际打款记录为准。</p>
      <SettlementDelivery key={`${projection.cycle_id}:${projection.actor_role}`} projection={projection} />
      {hasBlockers && !proposal ? <div style={{ borderTop: "1px solid var(--linen)", paddingTop: 16, display: "grid", gap: 12 }}>
        <div style={{ fontWeight: 500 }}>公司经营账尚未完成</div>
        <p style={{ margin: 0 }}>公司整月关账还会核对银行收支、费用凭据和流程状态。这里的待办不等于业主账单全部要重做。</p>
        <Button style={{ minHeight: 44 }} aria-expanded={showCompanyClose} onClick={() => setShowCompanyClose(!showCompanyClose)}>{showCompanyClose ? "收起公司月结待办" : "查看公司月结还差什么"}</Button>
        {showCompanyClose && <CloseReadinessSummary month={projection.billing_month} steps={steps} onOpenStep={onOpenStep} />}
      </div> : <p style={{ margin: 0 }}>本次结束的是公司整月对账。确认后会保存并核验关账结果，不会登记付款。</p>}
      {busy && <div role="status">{proposal ? "正在结束并核验本月对账，请稍候…" : "正在检查结束条件…"}</div>}
      {error && <div role="alert" style={{ color: "var(--clay)", overflowWrap: "anywhere" }}>{error}</div>}
      {proposal && !busy && <p>{canConfirm ? "已准备好结束方案。确认后，我会保存本月结束状态并检查结果。" : "本次结束尚未完成，请检查当前结果。已完成的操作会保留。"}</p>}
      {attempt && ["failed_safe", "failed_confirmed", "remediation_required"].includes(attempt.status) && <Button onClick={() => onOpenStep("exception_clearance")}>处理结束时的异常</Button>}
      {allowed && !busy && showActions && (canConfirm ? <Button type="primary" onClick={() => void run(true)}>确认结束本月对账</Button>
        : <Button onClick={() => void run(false)}>{error || hasBlockers ? "重新检查结束条件" : "检查当前结果"}</Button>)}
      {!allowed && <p>请由管理员完成本月对账。</p>}
    </>}
    {complete && <SettlementDelivery key={`${projection.cycle_id}:${projection.actor_role}`} projection={projection} />}
  </section></Modal>;
}
