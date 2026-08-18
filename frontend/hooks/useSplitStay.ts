"use client";

import { useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { ordersApi } from "@/lib/api";
import { invalidateOrderRelated } from "@/lib/order-cache";
import type { ZeroFeeSplitPayload, ZeroFeeSplitResult } from "@/lib/types";

export type SplitStayPhase =
  | "editing"
  | "previewing"
  | "preview_valid"
  | "submitting"
  | "refreshed";

function newIdempotencyKey(orderId: string): string {
  const suffix =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `zero-fee-split:${orderId}:${suffix}`;
}

function splitError(error: unknown, submitting: boolean): string {
  const response = (error as {
    response?: { status?: number; data?: { detail?: { code?: string; message?: string } } };
  })?.response;
  const detail = response?.data?.detail;
  if (detail?.code === "VERSION_CONFLICT") {
    return "数据已变化，请刷新后重新预览";
  }
  if (detail?.code === "ROOM_OCCUPANCY_CONFLICT") {
    return detail.message || "目标房间已有占用，请调整后重新预览";
  }
  if (detail?.code === "UNKNOWN_CHANNEL_RATIO") {
    return detail.message || "当前渠道没有已确认比例，无法拆分";
  }
  if (detail?.code === "SOURCE_PRICE_SNAPSHOT_MISSING") {
    return detail.message || "缺少可追溯的来源价格，无法拆分";
  }
  if (submitting) {
    return `保存失败，未保存任何拆分${detail?.message ? `：${detail.message}` : ""}`;
  }
  return detail?.message || "预览失败，请检查住宿日期和房间";
}

export function useSplitStay(orderId: string) {
  const queryClient = useQueryClient();
  const [phase, setPhase] = useState<SplitStayPhase>("editing");
  const [preview, setPreview] = useState<ZeroFeeSplitResult | null>(null);
  const [previewRequest, setPreviewRequest] = useState<ZeroFeeSplitPayload | null>(null);
  const [receipt, setReceipt] = useState<ZeroFeeSplitResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const keyRef = useRef(newIdempotencyKey(orderId));
  const submittingRef = useRef(false);
  const previewGenerationRef = useRef(0);

  const previewMutation = useMutation({
    mutationFn: (payload: ZeroFeeSplitPayload) =>
      ordersApi.previewZeroFeeSplit(orderId, payload),
  });
  const submitMutation = useMutation({
    mutationFn: (payload: ZeroFeeSplitPayload) =>
      ordersApi.splitStay(orderId, payload, keyRef.current),
  });

  const requestPreview = async (payload: ZeroFeeSplitPayload) => {
    const generation = ++previewGenerationRef.current;
    setPhase("previewing");
    setError(null);
    try {
      const result = await previewMutation.mutateAsync(payload);
      if (generation !== previewGenerationRef.current) return null;
      setPreview(result);
      setPreviewRequest({
        ...payload,
        segments: payload.segments.map((segment) => ({ ...segment })),
      });
      setPhase("preview_valid");
      return result;
    } catch (caught) {
      if (generation !== previewGenerationRef.current) return null;
      setPreview(null);
      setPreviewRequest(null);
      setError(splitError(caught, false));
      setPhase("editing");
      return null;
    }
  };

  const invalidatePreview = () => {
    previewGenerationRef.current += 1;
    setPreview(null);
    setPreviewRequest(null);
    setError(null);
    setPhase("editing");
    if (!submittingRef.current) keyRef.current = newIdempotencyKey(orderId);
  };

  const submit = async () => {
    if (submittingRef.current || phase !== "preview_valid" || !previewRequest) return null;
    submittingRef.current = true;
    setPhase("submitting");
    setError(null);
    try {
      const result = await submitMutation.mutateAsync(previewRequest);
      setReceipt(result);
      invalidateOrderRelated(queryClient);
      queryClient.invalidateQueries({ queryKey: ["manual-control", orderId] });
      setPhase("refreshed");
      return result;
    } catch (caught) {
      setError(splitError(caught, true));
      const code = (caught as {
        response?: { data?: { detail?: { code?: string } } };
      })?.response?.data?.detail?.code;
      if (code === "VERSION_CONFLICT") {
        setPreview(null);
        setPreviewRequest(null);
        keyRef.current = newIdempotencyKey(orderId);
        invalidateOrderRelated(queryClient);
        queryClient.invalidateQueries({ queryKey: ["manual-control", orderId] });
        setPhase("editing");
      } else {
        setPhase("preview_valid");
      }
      return null;
    } finally {
      submittingRef.current = false;
    }
  };

  return {
    phase,
    preview,
    receipt,
    error,
    requestPreview,
    invalidatePreview,
    submit,
    isPending: phase === "previewing" || phase === "submitting",
  };
}
