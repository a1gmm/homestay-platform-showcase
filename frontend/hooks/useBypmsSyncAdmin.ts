"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  bypmsSyncApi,
  normalizeBypmsConflictsFilters,
  normalizeBypmsCyclesFilters,
} from "@/lib/api";
import type {
  BypmsAdminConflictsFilters,
  BypmsAdminCyclesFilters,
} from "@/lib/types";

const BYPMS_SYNC_ROOT_KEY = ["bypms-sync"] as const;

export const bypmsSyncQueryKeys = {
  overview: () => [...BYPMS_SYNC_ROOT_KEY, "overview"] as const,
  cyclesRoot: () => [...BYPMS_SYNC_ROOT_KEY, "cycles"] as const,
  cycles: (filters: BypmsAdminCyclesFilters = {}) =>
    [...BYPMS_SYNC_ROOT_KEY, "cycles", normalizeBypmsCyclesFilters(filters)] as const,
  conflictsRoot: () => [...BYPMS_SYNC_ROOT_KEY, "conflicts"] as const,
  conflicts: (filters: BypmsAdminConflictsFilters = {}) =>
    [...BYPMS_SYNC_ROOT_KEY, "conflicts", normalizeBypmsConflictsFilters(filters)] as const,
};

export function useBypmsOverview() {
  return useQuery({
    queryKey: bypmsSyncQueryKeys.overview(),
    queryFn: () => bypmsSyncApi.overview().then((response) => response.data),
    refetchInterval: 30_000,
  });
}

export function useBypmsCycles(filters: BypmsAdminCyclesFilters = {}) {
  return useQuery({
    queryKey: bypmsSyncQueryKeys.cycles(filters),
    queryFn: () => bypmsSyncApi.cycles(filters).then((response) => response.data),
    refetchInterval: 30_000,
  });
}

export function useBypmsConflicts(filters: BypmsAdminConflictsFilters = {}) {
  return useQuery({
    queryKey: bypmsSyncQueryKeys.conflicts(filters),
    queryFn: () => bypmsSyncApi.conflicts(filters).then((response) => response.data),
  });
}

function safeRetryError(error: unknown): Error {
  const responseStatus = (error as { response?: { status?: unknown } })?.response?.status;
  if (responseStatus === 409) {
    return new Error("已有重试正在排队或执行");
  }
  if (responseStatus === 503) {
    return new Error("宝寓同步重试暂不可用");
  }
  if (responseStatus === 422) {
    return new Error("宝寓同步重试请求参数无效");
  }
  return new Error("宝寓同步重试请求失败");
}

export function useRequestBypmsRetry() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: async (idempotencyKey: string) => {
      try {
        return (await bypmsSyncApi.retry(idempotencyKey)).data;
      } catch (error) {
        throw safeRetryError(error);
      }
    },
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: bypmsSyncQueryKeys.overview(),
          exact: true,
        }),
        queryClient.invalidateQueries({ queryKey: bypmsSyncQueryKeys.cyclesRoot() }),
        queryClient.invalidateQueries({ queryKey: bypmsSyncQueryKeys.conflictsRoot() }),
      ]);
    },
    // The invoking UI owns the single contextual toast. The global
    // MutationCache would otherwise reinterpret our fixed Error as a network failure.
    meta: { silent: true },
  });
}


export function useBypmsOrderDiagnosis(platformOrderId: string) {
  return useQuery({
    queryKey: [...BYPMS_SYNC_ROOT_KEY, "order-diagnosis", platformOrderId],
    queryFn: () => bypmsSyncApi.diagnose(platformOrderId).then((response) => response.data),
    enabled: Boolean(platformOrderId),
    refetchInterval: 30_000,
  });
}
