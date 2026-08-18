"use client";

import { tokens } from "@/lib/design-tokens";
import { useIsMobile } from "@/lib/responsive";
import type { ReactNode, CSSProperties } from "react";

export interface PageHeaderProps {
  title: ReactNode;
  subtitle?: ReactNode;
  breadcrumbs?: ReactNode;
  extra?: ReactNode;
  children?: ReactNode;
  bordered?: boolean;
  style?: CSSProperties;
}

export function PageHeader({
  title,
  subtitle,
  breadcrumbs,
  extra,
  children,
  bordered = false,
  style,
}: PageHeaderProps) {
  const isMobile = useIsMobile();
  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 12,
        paddingBottom: bordered ? 16 : 0,
        borderBottom: bordered ? `1px solid ${tokens.color.bg.border}` : undefined,
        marginBottom: 20,
        ...style,
      }}
    >
      {breadcrumbs && (
        <div
          style={{
            fontSize: tokens.font.size.xs,
            color: tokens.color.text.tertiary,
          }}
        >
          {breadcrumbs}
        </div>
      )}
      {/*
        移动端：标题独占一行、操作区移到下方整行（避免定宽控件把标题挤成竖排单字）。
        桌面端：标题与操作左右分列。根因详见 lib/text.ts。
      */}
      <div
        style={{
          display: "flex",
          flexDirection: isMobile ? "column" : "row",
          alignItems: isMobile ? "stretch" : "flex-start",
          justifyContent: "space-between",
          gap: isMobile ? 12 : 16,
        }}
      >
        <div style={{ flex: 1, minWidth: 0 }}>
          <h1
            style={{
              margin: 0,
              fontSize: tokens.font.size["2xl"],
              fontWeight: tokens.font.weight.semibold,
              color: tokens.color.text.primary,
              letterSpacing: "-.01em",
              lineHeight: 1.2,
              // 标题永不竖排：内容超宽时正常换行，而不是被压成一列单字
              overflowWrap: "anywhere",
            }}
          >
            {title}
          </h1>
          {subtitle && (
            <div
              style={{
                marginTop: 4,
                fontSize: tokens.font.size.base,
                color: tokens.color.text.secondary,
                lineHeight: 1.5,
                overflowWrap: "anywhere",
              }}
            >
              {subtitle}
            </div>
          )}
        </div>
        {extra && (
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 8,
              flexWrap: "wrap",
              flexShrink: 0,
            }}
          >
            {extra}
          </div>
        )}
      </div>
      {children}
    </div>
  );
}
