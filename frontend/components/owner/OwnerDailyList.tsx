"use client";

import React, { useMemo, useState } from "react";
import { tokens } from "@/lib/design-tokens";
import { todayCNString } from "@/lib/utils";
import type { OwnerCalendarRoom } from "@/lib/owner-api";
import { CHANNEL_LABEL } from "@/lib/channels";
import { getChannelBarColors } from "@/lib/channel-bar-palette";

const WEEKDAY = ["日", "一", "二", "三", "四", "五", "六"];

const STATUS_LABEL: Record<string, string> = {
  pending_confirm: "待确认",
  paid_pending_room: "待排房",
  roomed_pending_checkin: "待入住",
  checked_in: "在住",
  pending_checkout: "已退房待收款",
  pending_payment: "待完成",
  rescheduled: "改期中",
  abnormal: "异常",
  "block:maintenance": "维修",
  "block:owner_use": "业主自用",
  "block:reserved": "预留",
  "block:other": "屏蔽",
};

function ymdLocal(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function statusDot(status: string): string {
  if (status === "block:maintenance") return tokens.anyu.color.clay;
  if (status.startsWith("block:")) return tokens.anyu.color.stone;
  return tokens.anyu.color.sage;
}

// 圆点颜色：优先按渠道（与甘特图统一色源），锁房/维修/无渠道回退状态色。
function dotColor(status: string, channel: string | null): string {
  if (status.startsWith("block:")) return statusDot(status);
  if (channel) return getChannelBarColors(channel).cap;
  return statusDot(status);
}

export interface OwnerDailyListProps {
  rooms: OwnerCalendarRoom[];
  /** 提供给 chip 条的可选日期范围（默认今天起 14 天）*/
  rangeDays?: number;
}

export function OwnerDailyList({ rooms, rangeDays = 14 }: OwnerDailyListProps) {
  const todayStr = todayCNString();

  const days = useMemo(() => {
    const start = new Date();
    return Array.from({ length: rangeDays }, (_, i) => {
      const d = new Date(start.getFullYear(), start.getMonth(), start.getDate() + i);
      return ymdLocal(d);
    });
  }, [rangeDays]);

  const [selectedDate, setSelectedDate] = useState<string>(todayStr);

  // 该日各房状态分桶：在住 / 空闲 / 维修 / 屏蔽
  const summary = useMemo(() => {
    let occupied = 0;
    let free = 0;
    let blocked = 0;
    rooms.forEach((r) => {
      const c = r.days?.[selectedDate];
      if (!c) free += 1;
      else if (c.status.startsWith("block:")) blocked += 1;
      else occupied += 1;
    });
    return { occupied, free, blocked, total: rooms.length };
  }, [rooms, selectedDate]);

  if (rooms.length === 0) {
    return (
      <div style={{ padding: 32, textAlign: "center", color: tokens.anyu.color.driftwood }}>
        暂无房间
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      {/* 日期 chip 横向滚动条 */}
      <div
        style={{
          display: "flex",
          gap: 8,
          overflowX: "auto",
          WebkitOverflowScrolling: "touch",
          paddingBottom: 4,
        }}
      >
        {days.map((d) => {
          const day = parseInt(d.split("-")[2]);
          const month = parseInt(d.split("-")[1]);
          const isToday = d === todayStr;
          const isSelected = d === selectedDate;
          const weekday = new Date(d).getDay();
          return (
            <button
              key={d}
              onClick={() => setSelectedDate(d)}
              style={{
                flex: "0 0 auto",
                minWidth: 56,
                padding: "8px 6px",
                borderRadius: tokens.anyu.radius.md,
                border: `0.5px solid ${
                  isSelected ? tokens.anyu.color.ink.default : tokens.anyu.color.linen
                }`,
                background: isSelected
                  ? tokens.anyu.color.ink.default
                  : isToday
                  ? "#FEF2F2"
                  : tokens.anyu.color.shell,
                color: isSelected
                  ? tokens.anyu.color.shell
                  : isToday
                  ? "#EF4444"
                  : tokens.anyu.color.ink.default,
                cursor: "pointer",
                fontSize: 12,
                lineHeight: 1.3,
                display: "flex",
                flexDirection: "column",
                alignItems: "center",
                gap: 2,
              }}
            >
              <span style={{ fontSize: 10, opacity: 0.7 }}>
                {isToday ? "今天" : `周${WEEKDAY[weekday]}`}
              </span>
              <span style={{ fontWeight: 600 }}>
                {String(month).padStart(2, "0")}/{String(day).padStart(2, "0")}
              </span>
            </button>
          );
        })}
      </div>

      {/* 该日小结 */}
      <div
        style={{
          display: "flex",
          gap: 14,
          fontSize: 12,
          color: tokens.anyu.color.stone,
          padding: "0 4px",
        }}
      >
        <span>
          在住 <strong style={{ color: tokens.anyu.color.ink.default }}>{summary.occupied}</strong>
        </span>
        <span>
          空闲 <strong style={{ color: tokens.anyu.color.ink.default }}>{summary.free}</strong>
        </span>
        {summary.blocked > 0 && (
          <span>
            屏蔽 <strong style={{ color: tokens.anyu.color.ink.default }}>{summary.blocked}</strong>
          </span>
        )}
        <span style={{ marginLeft: "auto", color: tokens.anyu.color.driftwood }}>
          共 {summary.total} 间
        </span>
      </div>

      {/* 房间列表 */}
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          gap: 8,
        }}
      >
        {rooms.map((room) => {
          const cell = room.days?.[selectedDate];
          const dot = cell ? dotColor(cell.status, cell.channel) : "transparent";
          const statusText = cell
            ? STATUS_LABEL[cell.status] || cell.status
            : "空闲";
          const channelText =
            cell && cell.channel ? CHANNEL_LABEL[cell.channel] || cell.channel : null;

          return (
            <div
              key={room.room_id}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 10,
                padding: "12px 14px",
                background: tokens.anyu.color.shell,
                border: `0.5px solid ${tokens.anyu.color.linen}`,
                borderRadius: tokens.anyu.radius.md,
              }}
            >
              <span
                style={{
                  width: 10,
                  height: 10,
                  borderRadius: "50%",
                  background: cell ? dot : "transparent",
                  border: cell ? "none" : `1px dashed ${tokens.anyu.color.linen}`,
                  flex: "0 0 10px",
                }}
              />
              <div style={{ flex: 1, minWidth: 0 }}>
                <div
                  style={{
                    fontSize: 13,
                    fontWeight: 500,
                    color: tokens.anyu.color.ink.default,
                    lineHeight: 1.3,
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                  }}
                >
                  {room.room_id}
                  {room.room_name && (
                    <span
                      style={{
                        marginLeft: 6,
                        fontSize: 11,
                        color: tokens.anyu.color.driftwood,
                        fontWeight: 400,
                      }}
                    >
                      {room.room_name}
                    </span>
                  )}
                </div>
                <div
                  style={{
                    marginTop: 2,
                    fontSize: 11,
                    color: tokens.anyu.color.stone,
                    lineHeight: 1.3,
                  }}
                >
                  {statusText}
                  {channelText && (
                    <span style={{ marginLeft: 6, color: tokens.anyu.color.driftwood }}>
                      · {channelText}
                    </span>
                  )}
                  {cell?.is_check_in_day && (
                    <span style={{ marginLeft: 6, color: tokens.anyu.color.sage }}>
                      · 今日入住
                    </span>
                  )}
                  {cell?.is_check_out_day && (
                    <span style={{ marginLeft: 6, color: tokens.anyu.color.clay }}>
                      · 今日退房
                    </span>
                  )}
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
