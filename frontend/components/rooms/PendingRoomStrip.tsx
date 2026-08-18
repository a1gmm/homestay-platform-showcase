"use client";

import React from "react";
import { Button, Empty, Skeleton } from "antd";
import { HomeOutlined, EyeOutlined } from "@ant-design/icons";
import type { OrderOut } from "@/lib/types";
import { tokens } from "@/lib/design-tokens";
import { StatusBadge } from "@/components/ui/StatusBadge";

import { CHANNEL_LABELS } from "@/lib/channels";

interface Props {
  orders: OrderOut[] | undefined;
  isLoading?: boolean;
  onAssign: (order: OrderOut) => void;
  onDetail: (order: OrderOut) => void;
  /** Phase 2: 拖拽排房入口；为 true 时卡片 draggable */
  draggable?: boolean;
  onDragStart?: (order: OrderOut, e: React.DragEvent<HTMLDivElement>) => void;
  onDragEnd?: () => void;
  /** 专注模式用：默认折叠成一个细条，点击展开（省纵向空间） */
  collapsible?: boolean;
}

export function PendingRoomStrip({
  orders,
  isLoading,
  onAssign,
  onDetail,
  draggable = false,
  onDragStart,
  onDragEnd,
  collapsible = false,
}: Props) {
  const count = orders?.length ?? 0;
  const [expanded, setExpanded] = React.useState(false);

  if (!isLoading && count === 0) {
    return null;
  }

  if (collapsible && !expanded) {
    return (
      <div
        role="button"
        tabIndex={0}
        onClick={() => setExpanded(true)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") setExpanded(true);
        }}
        style={{
          background: tokens.color.bg.container,
          border: `1px solid ${tokens.color.bg.border}`,
          borderRadius: tokens.radius.lg,
          padding: "8px 14px",
          display: "flex",
          alignItems: "center",
          gap: 8,
          cursor: "pointer",
          fontSize: 13,
          color: tokens.color.text.secondary,
        }}
      >
        <HomeOutlined />
        <span>{`▸ 待排房 ${count} 单`}</span>
        <span style={{ fontSize: 12, color: tokens.color.text.tertiary }}>（点击展开拖拽排房）</span>
      </div>
    );
  }

  return (
    <div
      style={{
        background: tokens.color.bg.container,
        border: `1px solid ${tokens.color.bg.border}`,
        borderRadius: tokens.radius.lg,
        padding: 14,
        display: "flex",
        flexDirection: "column",
        gap: 10,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span
            style={{
              width: 4,
              height: 14,
              borderRadius: 2,
              background: "#6366F1",
              display: "inline-block",
            }}
          />
          <span style={{ fontSize: 14, fontWeight: 600, color: tokens.color.text.primary }}>
            待排房订单
          </span>
          <span
            className="tabular"
            style={{
              fontSize: 12,
              color: "#6366F1",
              background: "#6366F11A",
              padding: "1px 8px",
              borderRadius: 999,
              fontWeight: 600,
            }}
          >
            {count}
          </span>
        </div>
        {draggable && count > 0 && (
          <span style={{ fontSize: 11, color: tokens.color.text.tertiary }}>
            可拖拽到甘特图房间×日期完成排房
          </span>
        )}
      </div>

      {isLoading ? (
        <Skeleton active paragraph={{ rows: 1 }} />
      ) : (
        <div
          style={{
            display: "flex",
            gap: 10,
            overflowX: "auto",
            paddingBottom: 4,
            scrollSnapType: "x proximity",
          }}
        >
          {orders!.map((o) => (
            // 收紧为「一行一单」的横条：把原本 5 行高的大卡片压成单行，
            // 待排房区不再占大半屏、把甘特图挤下去（客户 2026-07-07 反馈）。
            <div
              key={o.order_id}
              draggable={draggable}
              onDragStart={(e) => {
                if (!draggable) return;
                e.dataTransfer.setData("text/order-id", o.order_id);
                e.dataTransfer.effectAllowed = "move";
                onDragStart?.(o, e);
              }}
              onDragEnd={() => draggable && onDragEnd?.()}
              style={{
                flex: "0 0 auto",
                background: tokens.color.bg.page,
                border: `1px solid ${tokens.color.bg.border}`,
                borderRadius: tokens.radius.md,
                padding: "6px 8px 6px 12px",
                display: "flex",
                alignItems: "center",
                gap: 10,
                cursor: draggable ? "grab" : "default",
                scrollSnapAlign: "start",
                transition: "border-color 120ms ease, transform 120ms ease",
              }}
              onMouseEnter={(e) =>
                ((e.currentTarget as HTMLDivElement).style.borderColor = tokens.color.brand.primary)
              }
              onMouseLeave={(e) =>
                ((e.currentTarget as HTMLDivElement).style.borderColor = tokens.color.bg.border)
              }
            >
              <StatusBadge status={o.order_status} size="sm" />
              <div style={{ minWidth: 0, display: "flex", flexDirection: "column", gap: 2 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 6, minWidth: 0 }}>
                  <span
                    style={{
                      fontSize: 14,
                      fontWeight: 600,
                      color: tokens.color.text.primary,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}
                  >
                    {o.guest_name}
                  </span>
                  <span style={{ flex: "0 0 auto", fontSize: 11, color: tokens.color.text.tertiary }}>
                    {CHANNEL_LABELS[o.channel] || o.channel}
                  </span>
                </div>
                <div
                  className="tabular"
                  style={{ fontSize: 12, color: tokens.color.text.secondary, whiteSpace: "nowrap" }}
                >
                  {o.check_in_date?.slice(5)} → {o.check_out_date?.slice(5)} · {o.nights} 晚
                  {o.actual_price != null && (
                    <span style={{ marginLeft: 6, fontWeight: 600, color: tokens.color.brand.primary }}>
                      ¥{Number(o.actual_price).toLocaleString()}
                    </span>
                  )}
                </div>
              </div>
              <div style={{ display: "flex", gap: 6, marginLeft: "auto", flex: "0 0 auto" }}>
                <Button size="small" type="primary" icon={<HomeOutlined />} onClick={() => onAssign(o)}>
                  排房
                </Button>
                <Button
                  size="small"
                  icon={<EyeOutlined />}
                  onClick={() => onDetail(o)}
                  aria-label="查看详情"
                />
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
