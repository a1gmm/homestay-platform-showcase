"use client";

import React, { useMemo } from "react";
import { Table, Tag, Button, Empty } from "antd";
import { LockOutlined, PlusOutlined } from "@ant-design/icons";
import type { ColumnsType } from "antd/es/table";
import type { CalendarRoom, CalendarDay } from "@/lib/types";
import { tokens } from "@/lib/design-tokens";
import { ROOM_STATUS, TRIAL_BADGE } from "./constants";
import { todayCNString } from "@/lib/utils";

const STATUS_LABEL: Record<string, string> = {
  pending_confirm: "待确认",
  paid_pending_room: "待排房",
  roomed_pending_checkin: "待入住",
  checked_in: "在住",
  pending_checkout: "已退房待收款",
  pending_payment: "待完成",
  completed: "已完成",
  rescheduled: "重新排房",
  abnormal: "异常",
  cancelled: "已取消",
};

const STATUS_COLOR: Record<string, string> = {
  pending_confirm: "orange",
  pending_payment: "orange",
  paid_pending_room: "geekblue",
  roomed_pending_checkin: "blue",
  checked_in: "green",
  pending_checkout: "gold",
  completed: "default",
  cancelled: "default",
};

import { CHANNEL_LABEL } from "@/lib/channels";
import { getChannelBarColors } from "@/lib/channel-bar-palette";

const BLOCK_LABEL: Record<string, string> = {
  maintenance: "维修",
  owner_use: "业主自用",
  reserved: "预留",
  other: "屏蔽",
};

interface Row {
  room_id: string;
  room_name: string;
  room_status: string;
  today: CalendarDay | null;
  isBlock: boolean;
}

interface Props {
  calendar: CalendarRoom[] | undefined;
  isLoading?: boolean;
  /** 单日视图日期（默认今天） */
  date?: string;
  /** 按订单实时推算的有效房态 Map；房态徽章优先用它，避免读 calendar 快照的静态 room_status */
  effectiveStatusById?: Record<string, string>;
  onOrderClick: (orderId: string) => void;
  onCreateOrder: (roomId: string, date: string) => void;
}

/**
 * 单日视图：今日全部房间状态列表。竞品截图里「日历 / 单日」tab 的「单日」视图。
 * 数据复用甘特日历接口（rooms/availability/calendar），只取目标日期一天数据。
 * 移动端友好：纯 Table，无横向滚动。
 */
export function TodayRoomList({
  calendar,
  isLoading,
  date,
  effectiveStatusById,
  onOrderClick,
  onCreateOrder,
}: Props) {
  const targetDate = date || todayCNString();

  const rows: Row[] = useMemo(() => {
    return (calendar ?? []).map((r) => {
      const day = r.days?.[targetDate] ?? null;
      const isBlock = !!day && (day.order_id == null || String(day.status).startsWith("block:"));
      return {
        room_id: r.room_id,
        room_name: r.room_name,
        // 房态用实时推算值（跟着订单），fallback 到 calendar 快照的静态 room_status
        room_status: effectiveStatusById?.[r.room_id] ?? r.room_status,
        today: day,
        isBlock,
      };
    });
  }, [calendar, targetDate, effectiveStatusById]);

  const columns: ColumnsType<Row> = [
    {
      title: "房间",
      key: "room",
      width: 160,
      render: (_, r) => (
        <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
          <span
            style={{
              fontWeight: 600,
              color: tokens.color.text.primary,
              display: "inline-flex",
              alignItems: "center",
              gap: 4,
            }}
          >
            {(r.room_status === "maintenance" || r.room_status === "locked") && (
              <LockOutlined style={{ fontSize: 11, color: tokens.color.text.tertiary }} />
            )}
            {r.room_id}
          </span>
          <span style={{ fontSize: 11, color: tokens.color.text.tertiary }}>{r.room_name}</span>
        </div>
      ),
    },
    {
      title: "房态",
      key: "room_status",
      width: 100,
      render: (_, r) => {
        const meta = ROOM_STATUS[r.room_status];
        return (
          <Tag color={meta?.color ?? "default"} bordered={false}>
            {meta?.label ?? r.room_status}
          </Tag>
        );
      },
    },
    {
      title: `今日（${targetDate}）`,
      key: "today",
      render: (_, r) => {
        if (!r.today) {
          return (
            <span style={{ color: tokens.color.text.tertiary, fontSize: 12 }}>空闲</span>
          );
        }
        if (r.isBlock) {
          const t = String(r.today.status).replace(/^block:/, "");
          return (
            <Tag color="default" bordered={false}>
              {BLOCK_LABEL[t] ?? r.today.guest_name ?? "屏蔽"}
            </Tag>
          );
        }
        const channel = r.today.channel ? CHANNEL_LABEL[r.today.channel] || r.today.channel : "";
        return (
          <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <span style={{ fontWeight: 600, fontSize: 13 }}>{r.today.guest_name}</span>
              <Tag color={STATUS_COLOR[r.today.status as string] ?? "default"} bordered={false}>
                {STATUS_LABEL[r.today.status as string] ?? r.today.status}
              </Tag>
              {/* 试运营房型角标（备注含极海）：金色小标 */}
              {r.today.trial_tag && (
                <Tag
                  bordered={false}
                  style={{
                    color: TRIAL_BADGE.fg,
                    background: TRIAL_BADGE.bg,
                    fontWeight: 700,
                    marginInlineEnd: 0,
                  }}
                >
                  {r.today.trial_tag}
                </Tag>
              )}
            </div>
            {r.today.channel && (
              <span
                style={{
                  alignSelf: "flex-start",
                  display: "inline-flex",
                  alignItems: "center",
                  fontSize: 11,
                  fontWeight: 600,
                  padding: "1px 8px",
                  borderRadius: 5,
                  background: getChannelBarColors(r.today.channel).body,
                  color: getChannelBarColors(r.today.channel).text,
                  borderLeft: `3px solid ${getChannelBarColors(r.today.channel).cap}`,
                }}
              >
                {channel}
              </span>
            )}
          </div>
        );
      },
    },
    {
      title: "",
      key: "action",
      width: 110,
      render: (_, r) => {
        if (r.today?.order_id) {
          return (
            <Button size="small" onClick={() => onOrderClick(r.today!.order_id!)}>
              查看订单
            </Button>
          );
        }
        if (r.isBlock) {
          return (
            <span style={{ fontSize: 11, color: tokens.color.text.tertiary }}>—</span>
          );
        }
        return (
          <Button
            size="small"
            type="primary"
            icon={<PlusOutlined />}
            onClick={() => onCreateOrder(r.room_id, targetDate)}
          >
            新建订单
          </Button>
        );
      },
    },
  ];

  return (
    <div
      style={{
        background: tokens.color.bg.container,
        border: `1px solid ${tokens.color.bg.border}`,
        borderRadius: tokens.radius.lg,
        overflow: "hidden",
      }}
    >
      <Table
        columns={columns}
        dataSource={rows}
        rowKey="room_id"
        loading={isLoading}
        pagination={false}
        size="middle"
        locale={{
          emptyText: <Empty description="暂无房间" />,
        }}
        onRow={(r) => ({
          onClick: () => {
            if (r.today?.order_id) onOrderClick(r.today.order_id);
            else if (!r.isBlock) onCreateOrder(r.room_id, targetDate);
          },
          style: { cursor: "pointer" },
        })}
      />
    </div>
  );
}
