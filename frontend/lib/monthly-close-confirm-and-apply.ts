import { getPersistedAuthSessionId } from "@/lib/auth";
import { monthlyCloseApi } from "@/lib/api";
import type { MonthlyCloseProposalView } from "@/lib/monthly-close";

export function monthlyCloseProposalScopeGuard(signal?: AbortSignal) {
  const sessionId = getPersistedAuthSessionId();
  return () => {
    if (signal?.aborted || getPersistedAuthSessionId() !== sessionId) {
      throw new Error("当前会话或方案已切换，请在当前账号重新查看方案；已完成的批准会保留。");
    }
  };
}

/**
 * Continue the server-controlled approval -> execution -> verification chain.
 *
 * The user confirms once, while every irreversible boundary remains a separate
 * audited backend command. If policy keeps the proposal pending, or execution
 * becomes asynchronous, this function stops at that boundary and the normal
 * event refresh resumes the flow later.
 */
export async function confirmAndApplyMonthlyCloseProposal(
  billingMonth: string,
  proposal: MonthlyCloseProposalView,
  signal?: AbortSignal,
) {
  const assertCurrent = monthlyCloseProposalScopeGuard(signal);
  const signalArgs: [] | [AbortSignal] = signal ? [signal] : [];
  assertCurrent();
  let current = proposal;

  if (current.status === "pending_approval") {
    await monthlyCloseApi.approveProposal(
      billingMonth,
      current.proposal_id,
      `confirm:${current.proposal_id}:approve`,
      ...signalArgs,
    );
    assertCurrent();
    current = (await monthlyCloseApi.getProposal(
      billingMonth,
      current.proposal_id,
      ...signalArgs,
    )).data;
    assertCurrent();
  }

  let attempt = current.attempts?.at(-1);
  if (current.status === "approved" && !attempt) {
    assertCurrent();
    attempt = (await monthlyCloseApi.executeProposal(
      billingMonth,
      current.proposal_id,
      `confirm:${current.proposal_id}:execute`,
      ...signalArgs,
    )).data;
    assertCurrent();
  }

  if (attempt?.status === "succeeded_unverified") {
    assertCurrent();
    await monthlyCloseApi.verifyAttempt(
      billingMonth,
      attempt.attempt_id,
      `confirm:${current.proposal_id}:verify:${attempt.attempt_id}`,
      ...signalArgs,
    );
    assertCurrent();
  }
}
