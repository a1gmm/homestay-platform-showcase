"use client";

import { useEffect, useRef, useState } from "react";
import { Button, Modal } from "antd";
import { monthlyCloseApi } from "@/lib/api";
import { extractErrorMessage } from "@/lib/api-errors";
import { getPersistedAuthSessionId, useAuthStore } from "@/lib/auth";
import { monthlyCloseProposalScopeGuard } from "@/lib/monthly-close-confirm-and-apply";
import type { MonthlyCloseProjection, MonthlyCloseStep, OwnerCompletion } from "@/lib/monthly-close";
import { SettlementDelivery } from "./SettlementDelivery";

export function FinishMonthReview({ projection, onFinished, onOpenStep, onClose }: {
  projection: MonthlyCloseProjection; steps: MonthlyCloseStep[];
  onFinished: () => Promise<unknown>; onOpenStep: (key: string) => void; onClose?: () => void;
}) {
  const [result, setResult] = useState<OwnerCompletion | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const controller = useRef<AbortController | null>(null);
  const inflight = useRef(false);
  const latest = useRef({ projection, onFinished });
  useEffect(() => { latest.current = { projection, onFinished }; }, [projection, onFinished]);
  const allowed = projection.actor_role === "admin" && projection.can_advance_final_review;
  const complete = result?.completed === true;

  async function run(confirm: boolean) {
    const signal = controller.current?.signal;
    if (!signal || signal.aborted || inflight.current || !allowed) return;
    inflight.current = true; setBusy(true); setError(null);
    const assertCurrent = monthlyCloseProposalScopeGuard(signal);
    const current = latest.current.projection;
    try {
      assertCurrent();
      if (confirm && (!result?.ready || result.cycle_id !== current.cycle_id || result.billing_month !== current.billing_month)) throw new Error("请重新检查当前月份后确认。");
      const response = confirm
        ? await monthlyCloseApi.completeOwnerReconciliation(current.billing_month, result!.evidence_hash, signal)
        : await monthlyCloseApi.previewOwnerCompletion(current.billing_month, signal);
      assertCurrent();
      if (response.data.cycle_id !== current.cycle_id || response.data.billing_month !== current.billing_month || response.data.scope !== "owner_reconciliation_v1") throw new Error("返回结果与当前月份不一致，请重新检查。");
      if (!confirm) setResult(response.data);
      if (confirm) {
        // Read back persisted state; a successful write response alone is not
        // enough after a timeout, month switch or intervening evidence change.
        const checked = (await monthlyCloseApi.previewOwnerCompletion(current.billing_month, signal)).data;
        assertCurrent();
        if (!checked.completed || checked.cycle_id !== current.cycle_id) throw new Error("完成状态尚未核验，请重新检查；不要重复修改账目。");
        setResult(checked);
        await latest.current.onFinished();
      }
    } catch (cause) {
      if (!signal.aborted) { setResult(null); setError(extractErrorMessage(cause, "暂时无法完成，请重新检查。已有账目会保留。")); }
    } finally { inflight.current = false; if (!signal.aborted) setBusy(false); }
  }
  const initialRun = useRef(run);
  useEffect(() => {
    const abort = new AbortController(); controller.current = abort;
    const session = getPersistedAuthSessionId();
    const actor = useAuthStore.getState();
    const unsubscribe = useAuthStore.subscribe(next => {
      if (next.session_id !== actor.session_id || next.user?.user_id !== actor.user?.user_id || next.user?.role !== actor.user?.role) abort.abort();
    });
    const storage = () => { if (getPersistedAuthSessionId() !== session) abort.abort(); };
    window.addEventListener("storage", storage);
    const timer = window.setTimeout(() => void initialRun.current(false), 0);
    return () => { clearTimeout(timer); unsubscribe(); window.removeEventListener("storage", storage); abort.abort(); };
  }, []);

  const review = projection.final_review;
  return <Modal open title={`${projection.billing_month} · 业主对账收尾`} footer={null} onCancel={onClose} maskClosable={!busy} keyboard={!busy} closable={!busy}
    width={640} style={{ top: 32 }} styles={{ body: { maxHeight: "calc(100dvh - 160px)", overflowY: "auto" } }}>
    <section aria-label="完成本月业主对账" style={{ paddingTop: 20, display: "grid", gap: 16, minWidth: 0 }}>
      <h2 style={{ fontSize: 22, fontWeight: 500, margin: 0 }}>{complete ? `${projection.billing_month} 业主对账已完成` : "完成本月业主对账"}</h2>
      <p>本月 {review.settlement_count} 份业主账单，已确认 {review.confirmed_settlement_count ?? 0} 份，应付合计 ¥{result?.total_amount ?? review.settlement_total_amount}。</p>
      <Button href={`/settlements?month=${encodeURIComponent(projection.billing_month)}`}>{review.settlement_count ? "查看结算单与订单明细" : "检查并生成本月业主账单"}</Button>
      <SettlementDelivery key={`${projection.cycle_id}:${projection.actor_role}`} projection={projection} />
      {complete ? <p role="status">本月业主对账结果已保存，账单仍可随时查看和下载。如需调整，请填写原因重新打开本月。</p> : <>
        <p>确认后保存本月业主对账完成状态。实际打款请按实际情况另行登记。</p>
        {!!result?.blockers.length && <div role="alert"><p>还需要处理：</p><ul>{result.blockers.map(text => <li key={text}>{text}</li>)}</ul><Button onClick={() => onOpenStep("preflight")}>查看待核问题</Button></div>}
        {result?.ready && !busy && <p>平台收入、线下实收、费用及业主账单已通过当前检查，可以完成本月业主对账。</p>}
        {allowed && result?.ready && !busy && <Button type="primary" onClick={() => void run(true)}>确认完成本月业主对账</Button>}
      </>}
      {busy && <p role="status">正在检查并核验业主对账结果…</p>}
      {error && <p role="alert">{error}</p>}
      {allowed && !busy && !complete && <Button onClick={() => void run(false)}>重新检查完成条件</Button>}
      {!allowed && <p>请由管理员完成本月业主对账。</p>}
    </section>
  </Modal>;
}
