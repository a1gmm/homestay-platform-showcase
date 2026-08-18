"use client";

import React from "react";
import { AutoComplete, Input, Spin, Empty } from "antd";
import { SearchOutlined } from "@ant-design/icons";
import { useQuery, keepPreviousData } from "@tanstack/react-query";
import { ordersApi } from "@/lib/api";
import type { OrderListItem } from "@/lib/types";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { CHANNEL_LABELS } from "@/lib/channels";
import { tokens } from "@/lib/design-tokens";

interface Props {
  /** 选中某单 → 抛订单号，宿主页面用它打开订单详情抽屉 */
  onSelectOrder: (orderId: string) => void;
  style?: React.CSSProperties;
}

const DEBOUNCE_MS = 300;
const PAGE_SIZE = 8;

// 单条搜索结果行：客人名 + 状态角标 + 渠道 / 日期 + 手机号。
// 前台按姓名或手机号搜，日期+状态帮她在同名/多单里挑对那一张。
function ResultRow({ o }: { o: OrderListItem }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 2, padding: "2px 0" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <span style={{ fontWeight: 600, color: tokens.color.text.primary }}>{o.guest_name}</span>
        <StatusBadge status={o.order_status} size="sm" />
        <span style={{ fontSize: tokens.font.size.xs, color: tokens.color.text.tertiary }}>
          {CHANNEL_LABELS[o.channel] ?? o.channel}
        </span>
      </div>
      <div style={{ fontSize: tokens.font.size.xs, color: tokens.color.text.tertiary }}>
        {o.check_in_date} → {o.check_out_date}
        {o.guest_phone ? ` · ${o.guest_phone}` : ""}
      </div>
    </div>
  );
}

/**
 * 房态页顶栏常驻的「订单快搜」。
 *
 * 边搜即显：输入客人姓名 / 手机号 / 订单号，防抖 300ms 后按 keyword 拉最多 8 条，
 * 下拉列表里点一条 → 抛订单号给宿主页面打开订单详情抽屉，人始终停在甘特图上。
 * 取代「筛选/批量」抽屉里被收起的搜索框（前台反馈：搜索常用，别收起来）。
 *
 * 后端 GET /orders?keyword= 按 guest_name / guest_phone / order_id 模糊匹配。
 */
export function OrderQuickSearch({ onSelectOrder, style }: Props) {
  const [keyword, setKeyword] = React.useState("");
  const [debounced, setDebounced] = React.useState("");

  React.useEffect(() => {
    const t = setTimeout(() => setDebounced(keyword.trim()), DEBOUNCE_MS);
    return () => clearTimeout(t);
  }, [keyword]);

  const enabled = debounced.length >= 1;
  const { data, isFetching } = useQuery({
    queryKey: ["order-quick-search", debounced],
    queryFn: () =>
      ordersApi.list({ keyword: debounced, page_size: PAGE_SIZE }).then((r) => r.data),
    enabled,
    placeholderData: keepPreviousData,
  });

  const items = data?.items ?? [];
  const options = items.map((o) => ({ value: o.order_id, label: <ResultRow o={o} /> }));

  const notFound = !enabled
    ? null
    : isFetching
    ? (
      <div style={{ padding: 12, textAlign: "center" }}>
        <Spin size="small" />
      </div>
    )
    : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有匹配的订单" />;

  return (
    <AutoComplete
      // 服务端已按 keyword 过滤，禁用客户端二次过滤（否则会拿 order_id 去比对客人名，全被过滤掉）
      filterOption={false}
      value={keyword}
      options={enabled ? options : []}
      onChange={(v) => setKeyword(typeof v === "string" ? v : "")}
      onSelect={(orderId: string) => {
        onSelectOrder(orderId);
        // 清空，方便连续搜下一单
        setKeyword("");
        setDebounced("");
      }}
      notFoundContent={notFound}
      style={{ width: 260, ...style }}
      popupMatchSelectWidth={320}
    >
      <Input
        allowClear
        prefix={<SearchOutlined style={{ color: tokens.color.text.tertiary }} />}
        placeholder="搜索客人姓名 / 手机号 / 订单号"
      />
    </AutoComplete>
  );
}
