"use client";

import React from "react";
import { Drawer, Skeleton, Button } from "antd";
import { ArrowRightOutlined } from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { ordersApi, tasksApi } from "@/lib/api";
import { tokens } from "@/lib/design-tokens";
import { CHANNEL_LABELS } from "@/lib/channels";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { EmptyState } from "@/components/ui/EmptyState";
import { useIsMobile } from "@/lib/responsive";
import type { PeekPreset } from "@/lib/dashboard-peek";

// 抽屉里只列少量做速览；要看全量走底部「查看全部」。
const PEEK_LIMIT = 50;

function roomLabel(o: any): string {
  const ids: string[] =
    Array.isArray(o.room_ids) && o.room_ids.length > 0
      ? o.room_ids
      : o.room_id
      ? [o.room_id]
      : [];
  if (ids.length === 0) return "待排房";
  if (ids.length === 1) return ids[0];
  return `${ids.slice(0, 2).join(", ")}${ids.length > 2 ? ` (+${ids.length - 2})` : ""}`;
}

function OrderRow({ o, onClick }: { o: any; onClick: () => void }) {
  return (
    <div
      className="card-hoverable"
      onClick={onClick}
      style={{
        display: "flex",
        alignItems: "center",
        gap: 12,
        padding: "12px 4px",
        borderBottom: `1px solid ${tokens.color.bg.borderSubtle}`,
        cursor: "pointer",
      }}
    >
      <span
        style={{
          width: 32,
          height: 32,
          borderRadius: "50%",
          background: tokens.color.brand.primarySoft,
          color: tokens.color.brand.primary,
          display: "inline-flex",
          alignItems: "center",
          justifyContent: "center",
          fontSize: 13,
          fontWeight: 600,
          flex: "0 0 32px",
        }}
      >
        {o.guest_name?.charAt(0) ?? "?"}
      </span>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: 14, fontWeight: 500, lineHeight: 1.3 }}>
          {o.guest_name}
          <span style={{ color: tokens.color.text.tertiary, marginLeft: 8, fontWeight: 400 }}>
            {roomLabel(o)}
          </span>
        </div>
        <div style={{ fontSize: 12, color: tokens.color.text.tertiary, marginTop: 2 }}>
          {CHANNEL_LABELS[o.channel] || o.channel} · {o.check_in_date} → {o.check_out_date} · {o.nights}晚
        </div>
      </div>
      <StatusBadge status={o.order_status} size="sm" />
    </div>
  );
}

function TaskRow({ t }: { t: any }) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 12,
        padding: "12px 4px",
        borderBottom: `1px solid ${tokens.color.bg.borderSubtle}`,
      }}
    >
      <span
        style={{
          width: 8,
          height: 8,
          borderRadius: "50%",
          background: tokens.color.status.warn,
          flex: "0 0 8px",
        }}
      />
      <div style={{ flex: 1, minWidth: 0 }}>
        <div
          style={{
            fontSize: 14,
            fontWeight: 500,
            lineHeight: 1.3,
            whiteSpace: "nowrap",
            overflow: "hidden",
            textOverflow: "ellipsis",
          }}
        >
          {t.title ?? t.task_type}
        </div>
        <div style={{ fontSize: 12, color: tokens.color.text.tertiary, marginTop: 2 }}>
          {t.room_id ?? t.order_id ?? "全局任务"}
        </div>
      </div>
      <StatusBadge status={t.status ?? t.task_status ?? "pending"} size="sm" />
    </div>
  );
}

function RoomRow({ r }: { r: any }) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 12,
        padding: "12px 4px",
        borderBottom: `1px solid ${tokens.color.bg.borderSubtle}`,
      }}
    >
      <span
        style={{
          minWidth: 56,
          padding: "4px 8px",
          borderRadius: tokens.radius.sm,
          background: tokens.color.bg.subtle,
          color: tokens.color.text.primary,
          fontSize: 13,
          fontWeight: 600,
          textAlign: "center",
          flex: "0 0 auto",
        }}
      >
        {r.room_name ?? r.room_id}
      </span>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: 14, fontWeight: 500, lineHeight: 1.3 }}>
          {r.guest_name}
          {r.trial_tag && (
            <span style={{ color: tokens.color.text.tertiary, marginLeft: 8, fontWeight: 400 }}>
              {r.trial_tag}
            </span>
          )}
        </div>
        <div style={{ fontSize: 12, color: tokens.color.text.tertiary, marginTop: 2 }}>
          {r.check_in_date} → {r.check_out_date} 退房
        </div>
      </div>
    </div>
  );
}

export function KpiPeekDrawer({
  preset,
  open,
  onClose,
  onOpenOrder,
  rooms,
}: {
  preset: PeekPreset | null;
  open: boolean;
  onClose: () => void;
  /** 点某一单时的回调（在 dashboard 里可直接跳到该订单详情） */
  onOpenOrder?: (order: any) => void;
  /**
   * kind === "rooms" 的预设直接渲染这份数据（`/dashboard/today` 的 cleaning_list），
   * 不再另发请求——卡上的数字和抽屉里的名单同一份来源，结构上不可能对不上。
   */
  rooms?: any[];
}) {
  const router = useRouter();
  const isMobile = useIsMobile();
  const isInline = preset?.kind === "rooms";

  const query = useQuery({
    // preset 为 null 时（首帧）用占位 key，enabled=false 不真正请求
    queryKey: ["kpi-peek", preset?.key, preset?.apiParams],
    queryFn: async () => {
      if (!preset) return [];
      if (preset.kind === "orders") {
        const r = await ordersApi.list({ ...preset.apiParams, page: 1, page_size: PEEK_LIMIT });
        return r.data.items as any[];
      }
      const r = await tasksApi.list(preset.apiParams as any);
      return r.data as any[];
    },
    enabled: open && !!preset && !isInline,
    staleTime: 60 * 1000,
  });

  const items = isInline ? rooms ?? [] : query.data ?? [];

  return (
    <Drawer
      title={preset?.title ?? ""}
      placement="right"
      width={isMobile ? "100%" : 420}
      open={open}
      onClose={onClose}
      styles={{ body: { padding: "8px 20px 20px" } }}
      // rooms 名单本身就是全量（不截断），没有「查看全部」可去。
      footer={
        preset && !isInline && (
          <Button
            block
            type="primary"
            onClick={() => {
              onClose();
              router.push(preset.href);
            }}
          >
            查看全部{items.length ? ` ${items.length} 条` : ""} <ArrowRightOutlined />
          </Button>
        )
      }
    >
      {!isInline && query.isLoading ? (
        <div style={{ paddingTop: 8 }}>
          <Skeleton active paragraph={{ rows: 4 }} />
        </div>
      ) : items.length === 0 ? (
        <EmptyState title={preset?.emptyText ?? "暂无数据"} size="sm" />
      ) : (
        <div style={{ display: "flex", flexDirection: "column" }}>
          {preset?.kind === "orders" ? (
            items.map((o) => (
              <OrderRow
                key={o.order_id}
                o={o}
                onClick={() => {
                  if (onOpenOrder) {
                    onOpenOrder(o);
                  } else {
                    onClose();
                    router.push(`/orders?order_id=${o.order_id}`);
                  }
                }}
              />
            ))
          ) : preset?.kind === "rooms" ? (
            items.map((r) => <RoomRow key={`${r.room_id}-${r.order_id}`} r={r} />)
          ) : (
            items.map((t) => <TaskRow key={t.task_id} t={t} />)
          )}
        </div>
      )}
    </Drawer>
  );
}
