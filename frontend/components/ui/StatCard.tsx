"use client";

import { Skeleton } from "antd";
import { tokens } from "@/lib/design-tokens";
import type { ReactNode, CSSProperties } from "react";

type Tone = "brand" | "success" | "warn" | "info" | "neutral";

const TONE: Record<Tone, { iconBg: string; iconColor: string; deltaUp: string; deltaDown: string }> = {
  brand: {
    iconBg: tokens.color.brand.primarySoft,
    iconColor: tokens.color.brand.primary,
    deltaUp: tokens.color.status.active,
    deltaDown: tokens.color.status.warn,
  },
  success: {
    iconBg: tokens.color.status.activeSoft,
    iconColor: "#065F46",
    deltaUp: tokens.color.status.active,
    deltaDown: tokens.color.status.warn,
  },
  warn: {
    iconBg: tokens.color.status.pendingSoft,
    iconColor: "#92400E",
    deltaUp: tokens.color.status.active,
    deltaDown: tokens.color.status.warn,
  },
  info: {
    iconBg: tokens.color.status.infoSoft,
    iconColor: "#1E40AF",
    deltaUp: tokens.color.status.active,
    deltaDown: tokens.color.status.warn,
  },
  neutral: {
    iconBg: tokens.color.bg.subtle,
    iconColor: tokens.color.gray[700],
    deltaUp: tokens.color.status.active,
    deltaDown: tokens.color.status.warn,
  },
};

export interface StatCardProps {
  title: ReactNode;
  value: ReactNode;
  suffix?: ReactNode;
  prefix?: ReactNode;
  icon?: ReactNode;
  tone?: Tone;
  delta?: { value: number; label?: string } | null;
  deltaPrefix?: string;
  footer?: ReactNode;
  loading?: boolean;
  onClick?: () => void;
  style?: CSSProperties;
}

export function StatCard({
  title,
  value,
  suffix,
  prefix,
  icon,
  tone = "brand",
  delta,
  deltaPrefix,
  footer,
  loading,
  onClick,
  style,
}: StatCardProps) {
  const t = TONE[tone];
  const up = delta && delta.value >= 0;
  const deltaColor = delta ? (up ? t.deltaUp : t.deltaDown) : undefined;

  return (
    <div
      onClick={onClick}
      className={onClick ? "card-hoverable" : undefined}
      style={{
        background: tokens.color.bg.container,
        borderRadius: tokens.radius.lg,
        border: `1px solid ${tokens.color.bg.border}`,
        padding: 20,
        cursor: onClick ? "pointer" : "default",
        boxShadow: tokens.shadow.sm,
        display: "flex",
        flexDirection: "column",
        gap: 14,
        minHeight: 124,
        ...style,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 }}>
        <span
          style={{
            fontSize: tokens.font.size.sm,
            color: tokens.color.text.secondary,
            fontWeight: tokens.font.weight.medium,
          }}
        >
          {title}
        </span>
        {icon && (
          <span
            style={{
              width: 32,
              height: 32,
              borderRadius: tokens.radius.md,
              background: t.iconBg,
              color: t.iconColor,
              display: "inline-flex",
              alignItems: "center",
              justifyContent: "center",
              flex: "0 0 32px",
              fontSize: 16,
            }}
          >
            {icon}
          </span>
        )}
      </div>

      {loading ? (
        <Skeleton active paragraph={false} title={{ width: "60%" }} style={{ marginTop: 4 }} />
      ) : (
        <div
          style={{
            display: "flex",
            alignItems: "baseline",
            gap: 6,
            fontFamily: tokens.font.family,
          }}
        >
          {prefix && (
            <span style={{ fontSize: tokens.font.size.lg, color: tokens.color.text.secondary }}>{prefix}</span>
          )}
          <span
            className="tabular"
            style={{
              fontSize: tokens.font.size["3xl"],
              fontWeight: tokens.font.weight.semibold,
              color: tokens.color.text.primary,
              letterSpacing: "-.01em",
              lineHeight: 1.15,
            }}
          >
            {value}
          </span>
          {suffix && (
            <span style={{ fontSize: tokens.font.size.base, color: tokens.color.text.secondary, marginLeft: 2 }}>
              {suffix}
            </span>
          )}
        </div>
      )}

      {(delta || footer) && (
        <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          {delta && (
            <span
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 3,
                padding: "2px 8px",
                borderRadius: 999,
                background: up ? tokens.color.status.activeSoft : tokens.color.status.warnSoft,
                color: deltaColor,
                fontSize: tokens.font.size.xs,
                fontWeight: tokens.font.weight.medium,
                lineHeight: 1.4,
              }}
            >
              <span style={{ fontSize: 10, lineHeight: 1 }}>{up ? "▲" : "▼"}</span>
              {deltaPrefix ?? ""}
              {Math.abs(delta.value).toFixed(1)}%
              {delta.label ? <span style={{ marginLeft: 2, opacity: 0.75 }}>{delta.label}</span> : null}
            </span>
          )}
          {footer && (
            <span
              style={{
                fontSize: tokens.font.size.xs,
                color: tokens.color.text.tertiary,
              }}
            >
              {footer}
            </span>
          )}
        </div>
      )}
    </div>
  );
}
