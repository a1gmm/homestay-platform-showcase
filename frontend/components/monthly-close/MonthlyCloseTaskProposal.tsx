"use client";

import { useEffect, useState } from "react";
import { monthlyCloseApi } from "@/lib/api";
import type { MonthlyCloseProposalView } from "@/lib/monthly-close";
import { SourceProposalCard } from "./SourceProposalCard";

/** Remount by month/auth/proposal identity so late results cannot cross scopes. */
export function MonthlyCloseTaskProposal({ month, proposalId, onClose, onFinished }: {
  month: string; proposalId: string; onClose: () => void; onFinished: () => Promise<unknown>;
}) {
  const [proposal, setProposal] = useState<MonthlyCloseProposalView | null>(null);
  const [error, setError] = useState(false);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setError(false);
    void monthlyCloseApi.getProposal(month, proposalId, controller.signal).then((response) => {
      if (!controller.signal.aborted) setProposal(response.data);
    }).catch(() => { if (!controller.signal.aborted) setError(true); });
    return () => controller.abort();
  }, [month, proposalId, retry]);
  return <section aria-label="任务准备的核对方案" style={{ minWidth: 0, marginTop: 16, paddingTop: 16, borderTop: "1px solid var(--linen)" }}>
    <button type="button" className="mcw-secondary-button" style={{ minHeight: 44, borderRadius: 999, marginBottom: 12 }} onClick={onClose}>收起方案</button>
    {error ? <div role="alert">方案暂时没有读取成功。<button type="button" style={{ minHeight: 44, borderRadius: 999 }} onClick={() => setRetry((value) => value + 1)}>重新读取方案</button></div>
      : proposal ? <SourceProposalCard billingMonth={month} proposal={proposal} role="admin" onProposal={setProposal} onFinished={onFinished} />
        : <div role="status">正在读取已准备的方案</div>}
  </section>;
}
