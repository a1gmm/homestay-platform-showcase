"use client";

import { useCallback, useEffect, useState } from "react";

import { monthlyCloseApi } from "@/lib/api";
import type { MonthlyCloseProposalView, MonthlyCloseSourceProposalState } from "@/lib/monthly-close";

export function useSourceProposalRecovery(
  billingMonth: string,
  proposalType: string,
) {
  const [proposal, setProposal] = useState<MonthlyCloseProposalView | null>(null);
  const [items, setItems] = useState<MonthlyCloseProposalView[]>([]);
  const [sourceState, setSourceState] = useState<MonthlyCloseSourceProposalState | null>(null);

  const reload = useCallback(async () => {
    const response = await monthlyCloseApi.listSourceProposals(billingMonth);
    setProposal(response.data.active[proposalType] ?? null);
    setItems(response.data.items.filter((item) => item.proposal_type === proposalType));
    setSourceState(response.data.source_states?.[proposalType] ?? null);
    return response.data;
  }, [billingMonth, proposalType]);

  useEffect(() => {
    let active = true;
    void monthlyCloseApi.listSourceProposals(billingMonth).then((response) => {
        if (!active) return;
        setProposal(response.data.active[proposalType] ?? null);
        setItems(response.data.items.filter((item) => item.proposal_type === proposalType));
        setSourceState(response.data.source_states?.[proposalType] ?? null);
      })
      .catch(() => {
        // Recovery is additive. The source analysis controls remain usable if
        // the queue request is temporarily unavailable.
      });
    return () => {
      active = false;
    };
  }, [billingMonth, proposalType]);

  return [proposal, setProposal, items, reload, sourceState] as const;
}
