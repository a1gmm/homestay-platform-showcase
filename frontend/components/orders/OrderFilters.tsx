"use client";

import React from "react";
import { Input, Select, DatePicker, Button, Segmented } from "antd";
import { SearchOutlined, ReloadOutlined, FilterOutlined } from "@ant-design/icons";
import type { OrderFilters as OrderFiltersType } from "@/hooks/useOrders";
import { tokens } from "@/lib/design-tokens";
import { ACTIVE_CHANNELS } from "@/lib/channels";

const { RangePicker } = DatePicker;

// 筛选只暴露 10 个 active 渠道。老订单数据库里可能仍有 legacy 值，
// 但筛选不暴露——历史订单可以通过订单号/客人名/手机号搜到。
const CHANNEL_OPTIONS = ACTIVE_CHANNELS.map((c) => ({ value: c.code, label: c.label }));

// 2026-06-05 流程调整：确认收款挪到退房后，按新顺序排列
const STATUS_TABS = [
  { label: "全部", value: "" },
  { label: "待确认", value: "pending_confirm" },
  { label: "待排房", value: "paid_pending_room" },
  { label: "待入住", value: "roomed_pending_checkin" },
  { label: "在住", value: "checked_in" },
  { label: "已退房待收款", value: "pending_checkout" },
  { label: "待完成", value: "pending_payment" },
  { label: "已完成", value: "completed" },
  { label: "已取消", value: "cancelled" },
];

interface Props {
  filters: OrderFiltersType;
  onFiltersChange: React.Dispatch<React.SetStateAction<OrderFiltersType>>;
  onRefresh: () => void;
}

export default function OrderFilters({ filters, onFiltersChange, onRefresh }: Props) {
  const currentStatus = filters.status ?? "";

  return (
    <div
      style={{
        background: tokens.color.bg.container,
        border: `1px solid ${tokens.color.bg.border}`,
        borderRadius: tokens.radius.lg,
        padding: 12,
        display: "flex",
        flexDirection: "column",
        gap: 12,
      }}
    >
      <div style={{ overflowX: "auto" }}>
        <Segmented
          value={currentStatus}
          size="middle"
          options={STATUS_TABS}
          onChange={(v) =>
            onFiltersChange((f) => ({ ...f, status: (v as string) || undefined }))
          }
        />
      </div>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "minmax(220px, 1fr) 180px 240px auto",
          gap: 10,
          alignItems: "center",
        }}
      >
        <Input
          placeholder="搜索订单号 / 客人姓名 / 手机号"
          prefix={<SearchOutlined style={{ color: tokens.color.text.tertiary }} />}
          allowClear
          value={filters.keyword ?? ""}
          onChange={(e) => onFiltersChange((f) => ({ ...f, keyword: e.target.value || undefined }))}
        />
        <Select
          placeholder="渠道"
          allowClear
          value={filters.channel as any}
          onChange={(v) => onFiltersChange((f) => ({ ...f, channel: v }))}
          options={CHANNEL_OPTIONS}
          suffixIcon={<FilterOutlined />}
        />
        <RangePicker
          placeholder={["入住起", "入住止"]}
          onChange={(dates) => {
            if (dates) {
              onFiltersChange((f) => ({
                ...f,
                check_in_from: dates[0]?.format("YYYY-MM-DD"),
                check_in_to: dates[1]?.format("YYYY-MM-DD"),
              }));
            } else {
              onFiltersChange((f) => {
                const { check_in_from, check_in_to, ...rest } = f;
                return rest;
              });
            }
          }}
        />
        <Button icon={<ReloadOutlined />} onClick={onRefresh} />
      </div>
      <style jsx>{`
        @media (max-width: 1023px) {
          div[style*="grid-template-columns"] {
            grid-template-columns: 1fr !important;
          }
        }
      `}</style>
    </div>
  );
}
