"use client";

import React, { useState } from "react";
import { Tag } from "antd";
import { DownOutlined, RightOutlined } from "@ant-design/icons";
import { tokens } from "@/lib/design-tokens";
import { CHANNEL_LABELS } from "@/lib/channels";
import { formatExpectedRevenue } from "@/lib/order-display";
import type { StayGroup, StaySegment } from "@/lib/types";
import { FreeRoomBadge } from "@/components/ui/FreeRoomBadge";

interface Props {
  group: StayGroup;
  /** 点某一段 → 宿主决定段内操作（改价 / 改日期 / 取消本段）怎么走 */
  onSegmentClick?: (orderId: string) => void;
}

// 续住段的「分段明细」：默认收起，前台不展开就完全看不到底下是多张单。
// 展开是给做账用的——各段渠道/平台单号/金额分开列，因为佣金和对账各段独立。
export function StaySegmentBreakdown({ group, onSegmentClick }: Props) {
  const [open, setOpen] = useState(false);
  const segments = group.segments ?? [];

  // 单段组（非续住单）不渲染：没有「分段」可言
  if (segments.length <= 1) return null;

  return (
    <div style={{ borderTop: `1px solid ${tokens.color.bg.borderSubtle}`, paddingTop: 12 }}>
      <div
        role="button"
        tabIndex={0}
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") setOpen((v) => !v);
        }}
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          cursor: "pointer",
          minHeight: 44,
          fontSize: 13,
          color: tokens.color.text.tertiary,
        }}
      >
        {open ? <DownOutlined /> : <RightOutlined />}
        <span>分段明细（{segments.length} 段）</span>
      </div>

      {open && (
        <div style={{ display: "flex", flexDirection: "column", gap: 8, marginTop: 8 }}>
          {segments.map((s) => (
            <SegmentRow key={s.order_id} seg={s} onClick={onSegmentClick} />
          ))}
        </div>
      )}
    </div>
  );
}

function SegmentRow({ seg, onClick }: { seg: StaySegment; onClick?: (id: string) => void }) {
  const cancelled = seg.order_status === "cancelled";
  const netRevenue = formatExpectedRevenue(seg.expected_revenue);
  return (
    <div
      onClick={() => onClick?.(seg.order_id)}
      style={{
        display: "flex",
        alignItems: "center",
        gap: 10,
        flexWrap: "wrap",
        padding: "8px 10px",
        minHeight: 44,
        borderRadius: 8,
        background: tokens.color.bg.subtle,
        cursor: onClick ? "pointer" : "default",
        opacity: cancelled ? 0.55 : 1,
      }}
    >
      {/* 房号：跨房续住组 1605→1606 各段要分得清。段就是完整 OrderOut，房号从
          seg.rooms（order_rooms 行）推导——别读 orders.room_id（DEPRECATED 不可靠）；
          room_ids 是后端 segment_details 的同义后备。两者都缺才不渲染。 */}
      {(() => {
        const roomIds =
          seg.room_ids ??
          Array.from(
            new Set((seg.rooms ?? []).map((r) => r.room_id).filter((x): x is string => !!x)),
          );
        if (roomIds.length === 0) return null;
        return (
          <span style={{ fontSize: 13, fontWeight: 600, color: tokens.color.text.primary }}>
            {roomIds.join("/")}
          </span>
        );
      })()}
      <span style={{ fontSize: 13, color: tokens.color.text.secondary }}>
        {seg.check_in_date.slice(5)} → {seg.check_out_date.slice(5)}
      </span>
      <Tag style={{ marginInlineEnd: 0 }}>{CHANNEL_LABELS[seg.channel] ?? seg.channel}</Tag>
      <FreeRoomBadge kind={seg.is_ota_free_room ? "all" : "none"} />
      {/* 押金标在实收那一段（deposit_holder 可能是续段）；¥0 不标避免噪音，字段缺失不渲染 */}
      {seg.deposit != null && Number(seg.deposit) > 0 && (
        <Tag style={{ marginInlineEnd: 0 }} color="gold">
          押金 ¥{Number(seg.deposit).toLocaleString()}
        </Tag>
      )}
      {seg.platform_order_id && (
        <span style={{ fontSize: 12, color: tokens.color.text.tertiary, wordBreak: "break-all" }}>
          {seg.platform_order_id}
        </span>
      )}
      <span
        style={{
          marginLeft: "auto",
          display: "flex",
          flexDirection: "column",
          alignItems: "flex-end",
          lineHeight: 1.35,
        }}
      >
        <span
          className="tabular"
          style={{
            fontSize: 13,
            fontWeight: 500,
            color: tokens.color.text.primary,
            textDecoration: cancelled ? "line-through" : "none",
          }}
        >
          ¥{seg.actual_price ?? "0.00"}
        </span>
        {/* 净房费（业主到手）只有按单口径 —— 整段头部不显示它，就在这里各段各看各的。
            OTA 搬单才有值，直收单为空不渲染。 */}
        {netRevenue && (
          <span
            className="tabular"
            style={{
              fontSize: 11,
              color: tokens.color.brand.primary,
              textDecoration: cancelled ? "line-through" : "none",
            }}
          >
            净 {netRevenue}
          </span>
        )}
      </span>
      {cancelled && <Tag color="default">已取消</Tag>}
    </div>
  );
}
