"use client";

import { useMutation } from "@tanstack/react-query";
import { hostingLeadsApi } from "@/lib/api";
import type { HostingLeadCreate } from "@/lib/types";

export const PHONE_RE = /^1[3-9]\d{9}$/;

export function useSubmitHostingLead() {
  return useMutation({
    mutationFn: (payload: HostingLeadCreate) => hostingLeadsApi.create(payload),
    // 落地页表单自己展示错误文案（LeadForm 的 onError），
    // 跳过 providers.tsx 全局 MutationCache 的兜底 toast，避免双重报错。
    meta: { silent: true },
  });
}
