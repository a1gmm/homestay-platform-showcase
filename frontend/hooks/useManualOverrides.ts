"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";

import { ordersApi } from "@/lib/api";
import { invalidateOrderWithAudit } from "@/lib/order-cache";
import type {
  ManualOverrideField,
  SourcePriceSnapshotOverridePayload,
} from "@/lib/types";

export function useManualOverrides(orderId: string) {
  const queryClient = useQueryClient();
  const refresh = () => {
    invalidateOrderWithAudit(queryClient, orderId);
    // A managed child is read under its clicked id but mutations target the
    // canonical source id. Refresh every active projection so the open drawer
    // cannot retain the child's stale conflict/ownership view.
    queryClient.invalidateQueries({ queryKey: ["manual-control"] });
  };

  const unlockMutation = useMutation({
    mutationFn: (payload: {
      action: "unlock";
      fields: ManualOverrideField[];
      reason: string;
    }) => ordersApi.unlockOrderFields(orderId, payload),
    onSuccess: refresh,
  });

  const conflictMutation = useMutation({
    mutationFn: ({
      conflictId,
      action,
      reason,
    }: {
      conflictId: string;
      action: "preserve" | "ignore";
      reason: string;
    }) => ordersApi.decideSyncConflict(orderId, conflictId, { action, reason }),
    onSuccess: refresh,
  });

  const sourcePriceMutation = useMutation({
    mutationFn: (payload: SourcePriceSnapshotOverridePayload) =>
      ordersApi.createSourcePriceSnapshotOverride(orderId, payload),
    onSuccess: refresh,
  });

  return { unlockMutation, conflictMutation, sourcePriceMutation };
}
