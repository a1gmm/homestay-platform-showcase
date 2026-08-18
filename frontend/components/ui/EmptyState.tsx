"use client";

import { Button } from "antd";
import { tokens } from "@/lib/design-tokens";
import type { ReactNode, CSSProperties } from "react";

export interface EmptyStateProps {
  title?: string;
  description?: string;
  icon?: ReactNode;
  actionLabel?: string;
  onAction?: () => void;
  secondaryAction?: { label: string; onClick: () => void };
  size?: "sm" | "md" | "lg";
  style?: CSSProperties;
}

function DefaultArt() {
  return (
    <svg width="112" height="84" viewBox="0 0 112 84" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden>
      <defs>
        <linearGradient id="es-bg" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#F5F1EA" />
          <stop offset="100%" stopColor="#FBF8F1" />
        </linearGradient>
      </defs>
      <ellipse cx="56" cy="74" rx="44" ry="6" fill="#E5DDCB" opacity=".6" />
      <rect x="20" y="20" width="72" height="48" rx="10" fill="url(#es-bg)" stroke="#E5DDCB" strokeWidth="0.5" />
      <rect x="30" y="32" width="36" height="6" rx="3" fill="#A89680" opacity=".6" />
      <rect x="30" y="44" width="52" height="4" rx="2" fill="#E5DDCB" />
      <rect x="30" y="54" width="28" height="4" rx="2" fill="#E5DDCB" />
      <circle cx="84" cy="26" r="10" fill="#2B2721" opacity=".9" />
      <path d="M80 26l3 3 6-6" stroke="#FBF8F1" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function EmptyState({
  title = "还没有数据",
  description,
  icon,
  actionLabel,
  onAction,
  secondaryAction,
  size = "md",
  style,
}: EmptyStateProps) {
  const pad = size === "sm" ? 24 : size === "lg" ? 64 : 48;

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        padding: `${pad}px 16px`,
        textAlign: "center",
        color: tokens.color.text.secondary,
        ...style,
      }}
    >
      <div style={{ marginBottom: 16, opacity: 0.95 }}>{icon ?? <DefaultArt />}</div>
      <div
        style={{
          fontSize: tokens.font.size.lg,
          fontWeight: tokens.font.weight.semibold,
          color: tokens.color.text.primary,
          marginBottom: 4,
        }}
      >
        {title}
      </div>
      {description && (
        <div
          style={{
            fontSize: tokens.font.size.base,
            color: tokens.color.text.secondary,
            maxWidth: 360,
            marginBottom: actionLabel ? 20 : 0,
          }}
        >
          {description}
        </div>
      )}
      {(actionLabel || secondaryAction) && (
        <div style={{ display: "flex", gap: 8, marginTop: description ? 0 : 20 }}>
          {actionLabel && onAction && (
            <Button type="primary" onClick={onAction}>
              {actionLabel}
            </Button>
          )}
          {secondaryAction && <Button onClick={secondaryAction.onClick}>{secondaryAction.label}</Button>}
        </div>
      )}
    </div>
  );
}
