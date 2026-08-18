"use client";

import { tokens } from "@/lib/design-tokens";
import type { CSSProperties } from "react";

type Variant = "active" | "pending" | "warn" | "info" | "neutral";

const STATUS_MAP: Record<string, { label: string; variant: Variant }> = {
  // Order — 2026-06-05 流程调整：确认收款挪到退房后
  pending_confirm: { label: "待确认", variant: "pending" },
  paid_pending_room: { label: "待排房", variant: "info" },
  roomed_pending_checkin: { label: "待入住", variant: "info" },
  checked_in: { label: "在住", variant: "active" },
  pending_checkout: { label: "已退房待收款", variant: "pending" },
  pending_payment: { label: "待完成", variant: "info" },
  completed: { label: "已完成", variant: "neutral" },
  rescheduled: { label: "已改期", variant: "info" },
  abnormal: { label: "异常", variant: "warn" },
  cancelled: { label: "已取消", variant: "neutral" },

  // Room
  available: { label: "可入住", variant: "active" },
  occupied: { label: "在住", variant: "info" },
  maintenance: { label: "维修中", variant: "warn" },
  locked: { label: "锁房", variant: "neutral" },
  reserved: { label: "已预订", variant: "pending" },

  // Payment
  unpaid: { label: "未付款", variant: "warn" },
  partial: { label: "部分付款", variant: "pending" },
  paid: { label: "已付款", variant: "active" },
  partial_refund: { label: "部分退款", variant: "pending" },
  full_refund: { label: "全额退款", variant: "neutral" },

  // Deposit
  not_collected: { label: "未收", variant: "neutral" },
  collected: { label: "已收", variant: "active" },
  returned: { label: "已退", variant: "neutral" },
  withheld: { label: "已扣", variant: "warn" },

  // Task
  pending: { label: "待处理", variant: "pending" },
  in_progress: { label: "进行中", variant: "info" },
  done: { label: "已完成", variant: "active" },
  pending_review: { label: "待审核", variant: "pending" },
  approved: { label: "已通过", variant: "active" },
  rejected: { label: "已驳回", variant: "warn" },

  // Channel (used as tag too)
  ctrip: { label: "携程", variant: "info" },
  meituan: { label: "美团", variant: "info" },
  tujia: { label: "途家", variant: "info" },
  private: { label: "私域", variant: "active" },
  walk_in: { label: "到店", variant: "neutral" },
};

const VARIANT_STYLE: Record<Variant, { dot: string; text: string; bg: string; border: string }> = {
  active: {
    dot: tokens.color.status.active,
    text: "#065F46",
    bg: tokens.color.status.activeSoft,
    border: "rgba(16,185,129,.2)",
  },
  pending: {
    dot: tokens.color.status.pending,
    text: "#92400E",
    bg: tokens.color.status.pendingSoft,
    border: "rgba(245,158,11,.24)",
  },
  warn: {
    dot: tokens.color.status.warn,
    text: "#991B1B",
    bg: tokens.color.status.warnSoft,
    border: "rgba(239,68,68,.22)",
  },
  info: {
    dot: tokens.color.status.info,
    text: "#1E40AF",
    bg: tokens.color.status.infoSoft,
    border: "rgba(59,130,246,.22)",
  },
  neutral: {
    dot: tokens.color.status.neutral,
    text: tokens.color.gray[700],
    bg: tokens.color.status.neutralSoft,
    border: tokens.color.bg.border,
  },
};

export interface StatusBadgeProps {
  status: string;
  label?: string;
  variant?: Variant;
  size?: "sm" | "md";
  dot?: boolean;
  solid?: boolean;
  style?: CSSProperties;
}

export function StatusBadge({
  status,
  label,
  variant,
  size = "md",
  dot = true,
  solid = false,
  style,
}: StatusBadgeProps) {
  const mapped = STATUS_MAP[status];
  const v: Variant = variant ?? mapped?.variant ?? "neutral";
  const text = label ?? mapped?.label ?? status;
  const s = VARIANT_STYLE[v];

  const height = size === "sm" ? 20 : 24;
  const fontSize = size === "sm" ? tokens.font.size.xs : tokens.font.size.sm;
  const paddingX = size === "sm" ? 8 : 10;

  if (solid) {
    return (
      <span
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 6,
          height,
          padding: `0 ${paddingX}px`,
          borderRadius: 999,
          background: s.dot,
          color: "#fff",
          fontSize,
          fontWeight: 500,
          lineHeight: 1,
          letterSpacing: ".01em",
          ...style,
        }}
      >
        {text}
      </span>
    );
  }

  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        height,
        padding: `0 ${paddingX}px`,
        borderRadius: 999,
        background: s.bg,
        color: s.text,
        fontSize,
        fontWeight: 500,
        lineHeight: 1,
        letterSpacing: ".01em",
        border: `1px solid ${s.border}`,
        whiteSpace: "nowrap",
        ...style,
      }}
    >
      {dot && (
        <span
          style={{
            width: 6,
            height: 6,
            borderRadius: "50%",
            background: s.dot,
            flex: "0 0 6px",
          }}
        />
      )}
      {text}
    </span>
  );
}
