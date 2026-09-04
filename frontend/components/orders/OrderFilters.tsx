"use client";

import React from "react";
import { Input, Select, DatePicker, Button, Segmented } from "antd";
import { SearchOutlined, ReloadOutlined, FilterOutlined } from "@ant-design/icons";
import type { OrderFilters as OrderFiltersType } from "@/hooks/useOrders";
import { tokens } from "@/lib/design-tokens";
import { ACTIVE_CHANNELS } from "@/lib/channels";
import dayjs from "dayjs";

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
  onFiltersChange: (filters: OrderFiltersType) => void;
  onRefresh: () => void;
}

export default function OrderFilters({ filters, onFiltersChange, onRefresh }: Props) {
  const currentStatus = filters.status ?? "";
  const [selectedDateBasis, setSelectedDateBasis] = React.useState<
    "final_checkout" | "first_checkin"
  >(filters.date_basis ?? "final_checkout");

  React.useEffect(() => {
    if (filters.date_basis) setSelectedDateBasis(filters.date_basis);
  }, [filters.date_basis]);

  const update = (patch: Partial<OrderFiltersType>) =>
    onFiltersChange({ ...filters, ...patch });

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
          onChange={(v) => update({ status: (v as string) || undefined })}
        />
      </div>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "minmax(220px, 1fr) 180px minmax(360px, 1.35fr) auto",
          gap: 10,
          alignItems: "center",
        }}
      >
        <Input
          placeholder="搜索订单号 / 客人姓名 / 手机号"
          prefix={<SearchOutlined style={{ color: tokens.color.text.tertiary }} />}
          allowClear
          value={filters.keyword ?? ""}
          onChange={(e) => update({ keyword: e.target.value || undefined })}
        />
        <Select
          placeholder="渠道"
          allowClear
          value={filters.channel as any}
          onChange={(v) => update({ channel: v })}
          options={CHANNEL_OPTIONS}
          suffixIcon={<FilterOutlined />}
        />
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <div style={{ display: "flex", gap: 6 }}>
            <Select
              aria-label="日期口径"
              value={selectedDateBasis}
              style={{ width: 116 }}
              options={[
                { value: "final_checkout", label: "最终退房日" },
                { value: "first_checkin", label: "首次入住日" },
              ]}
              onChange={(value) => {
                setSelectedDateBasis(value);
                if (filters.date_from && filters.date_to) update({ date_basis: value });
              }}
            />
            <RangePicker
              aria-label="订单日期范围"
              value={
                filters.date_from && filters.date_to
                  ? [dayjs(filters.date_from), dayjs(filters.date_to)]
                  : null
              }
              placeholder={["开始日期", "结束日期"]}
              onChange={(dates) => {
                if (dates?.[0] && dates?.[1]) {
                  update({
                    date_basis: selectedDateBasis,
                    date_from: dates[0].format("YYYY-MM-DD"),
                    date_to: dates[1].format("YYYY-MM-DD"),
                  });
                } else {
                  const { date_basis, date_from, date_to, ...rest } = filters;
                  onFiltersChange(rest);
                }
              }}
              style={{ flex: 1, minWidth: 0 }}
            />
          </div>
          <span style={{ color: tokens.color.text.tertiary, fontSize: 11, lineHeight: 1.3 }}>
            默认按整段订单的最终退房日筛选
          </span>
        </div>
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
