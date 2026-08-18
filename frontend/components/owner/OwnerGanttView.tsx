"use client";

import React, { useEffect, useMemo, useRef, useState } from "react";
import { Modal, Tag } from "antd";
import { tokens } from "@/lib/design-tokens";
import { todayCNString } from "@/lib/utils";
import type { OwnerCalendarDay, OwnerCalendarRoom } from "@/lib/owner-api";
import { CHANNEL_LABEL, getChannelMeta } from "@/lib/channels";
import { getChannelBarColors } from "@/lib/channel-bar-palette";
import { groupByRoomType, UNGROUPED_LABEL } from "@/lib/room-types";
import { useIsMobile } from "@/lib/responsive";
import { ORDER_STATUS_LABEL as STATUS_LABEL, orderStatusHex } from "@/lib/status-display";

const WEEKDAY = ["日", "一", "二", "三", "四", "五", "六"];

// 业主端对齐后台甘特的状态色系——业主能看到订单生命周期的颜色,
// 但仍然看不到客人姓名/手机号/价格(后端 owner_calendar 不下发 PII)。
function statusColor(s?: string): string {
  if (s === "block:maintenance") return "#F97316"; // 维修橙
  return orderStatusHex(s, tokens.anyu.color.sage);
}

function ymdLocal(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

// 订单条渠道分色（与后台/移动端 C2 对齐，复用同一色源）。
// 业主端仍不下发客人姓名 → 条上文字保留「状态」（在住/待入住…），颜色=渠道。
const CH_CANCEL = { body: "#E4E2DE", text: "#8A857C" };
function barColorsFor(cell: OwnerCalendarDay): { body: string; text: string; cap: string | null } {
  const s = cell.status;
  if (s?.startsWith("block:")) return { body: statusColor(s), text: "#fff", cap: null };
  if (s === "cancelled") return { body: CH_CANCEL.body, text: CH_CANCEL.text, cap: null };
  if (cell.channel) {
    const p = getChannelBarColors(cell.channel);
    return { body: p.body, text: p.text, cap: p.cap };
  }
  return { body: statusColor(s), text: "#fff", cap: null };
}

// 同订单/同 block 连续判定:业主端不下发 order_id, status+channel 相同即视为同段
function isSameBlock(a?: OwnerCalendarDay, b?: OwnerCalendarDay): boolean {
  if (!a || !b) return false;
  return a.status === b.status && (a.channel ?? "") === (b.channel ?? "");
}

interface BlockInfo {
  roomId: string;
  roomName: string;
  startDate: string;
  endDate: string;
  nights: number;
  status: string;
  channel: string | null;
}

export interface OwnerGanttViewProps {
  rooms: OwnerCalendarRoom[];
  /** 展示月份(整月视图,与后台对齐) */
  calMonth: { year: number; month: number };
  cellWidth?: number;
  rowHeight?: number;
}

const ROOM_COL_WIDTH_DESKTOP = 96;
const GROUP_COL_WIDTH_DESKTOP = 116;

export function OwnerGanttView({
  rooms,
  calMonth,
  cellWidth: cellWidthProp = 64,
  rowHeight = 44,
}: OwnerGanttViewProps) {
  // 手机屏窄，固定列(房型/房间)+日期列写死宽度会挤到只剩两三天可见。
  // 按设备收窄这三项，桌面维持原尺寸。用 useIsMobile(按 UA，跨窗口缩放稳定)。
  const isMobile = useIsMobile();
  const GROUP_COL_WIDTH = isMobile ? 84 : GROUP_COL_WIDTH_DESKTOP;
  const ROOM_COL_WIDTH = isMobile ? 64 : ROOM_COL_WIDTH_DESKTOP;
  const cellWidth = isMobile ? 48 : cellWidthProp;
  const todayStr = todayCNString();
  const daysInMonth = new Date(calMonth.year, calMonth.month, 0).getDate();
  const days = useMemo(
    () =>
      Array.from({ length: daysInMonth }, (_, i) => {
        const d = new Date(calMonth.year, calMonth.month - 1, i + 1);
        return ymdLocal(d);
      }),
    [calMonth.year, calMonth.month, daysInMonth],
  );

  // 按房型分组,业主主页同款 lib/room-types 工具,顺序一致
  const groups = useMemo(
    () => groupByRoomType(rooms, (r) => r.room_type),
    [rooms],
  );
  const hasGroupCol = groups.length > 1;
  const fixedLeftWidth = hasGroupCol ? GROUP_COL_WIDTH + ROOM_COL_WIDTH : ROOM_COL_WIDTH;

  // sticky 分组标题高度跟随 thead 实测
  const theadRef = useRef<HTMLTableSectionElement>(null);
  const [theadH, setTheadH] = useState(64);
  useEffect(() => {
    const el = theadRef.current;
    if (!el) return;
    const update = () => setTheadH(el.getBoundingClientRect().height || 64);
    update();
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // 默认横向滚到「今天」那一列
  const scrollRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const container = scrollRef.current;
    if (!container) return;
    const [ty, tm, td] = todayStr.split("-").map(Number);
    if (ty !== calMonth.year || tm !== calMonth.month) {
      container.scrollLeft = 0;
      return;
    }
    const todayIdx = td - 1;
    const visibleGridW = container.clientWidth - fixedLeftWidth;
    const target = todayIdx * cellWidth - visibleGridW / 2 + cellWidth / 2;
    container.scrollLeft = Math.max(0, target);
  }, [calMonth.year, calMonth.month, todayStr, cellWidth, fixedLeftWidth]);

  // 每日占用统计 — 第二行表头展示"剩 X/Y"
  const occupancyByDate = useMemo(() => {
    const total = rooms.length;
    const m: Record<string, { used: number; total: number }> = {};
    days.forEach((d) => {
      const used = rooms.filter((r) => r.days?.[d]).length;
      m[d] = { used, total };
    });
    return m;
  }, [rooms, days]);

  // 当前画面出现的渠道 → 图例（只列出现的）
  const presentChannels = useMemo(() => {
    const seen = new Set<string>();
    const list: string[] = [];
    for (const r of rooms) {
      for (const k of Object.keys(r.days ?? {})) {
        const d = r.days[k];
        const ch = d?.channel;
        if (!ch || d.status === "cancelled" || String(d.status).startsWith("block:")) continue;
        if (!seen.has(ch)) {
          seen.add(ch);
          list.push(ch);
        }
      }
    }
    return list;
  }, [rooms]);

  const [selected, setSelected] = useState<BlockInfo | null>(null);

  function openDetailFor(roomId: string, dayIdx: number) {
    const room = rooms.find((r) => r.room_id === roomId);
    if (!room) return;
    const dayKey = days[dayIdx];
    const cell = room.days?.[dayKey];
    if (!cell) return;
    let startIdx = dayIdx;
    while (startIdx > 0 && isSameBlock(room.days?.[days[startIdx - 1]], cell)) startIdx -= 1;
    let endIdx = dayIdx;
    while (endIdx < days.length - 1 && isSameBlock(room.days?.[days[endIdx + 1]], cell)) endIdx += 1;
    setSelected({
      roomId: room.room_id,
      roomName: room.room_name,
      startDate: days[startIdx],
      endDate: days[endIdx],
      nights: endIdx - startIdx + 1,
      status: cell.status,
      channel: cell.channel,
    });
  }

  if (rooms.length === 0) {
    return (
      <div
        style={{
          padding: 40,
          textAlign: "center",
          color: tokens.anyu.color.driftwood,
          background: tokens.anyu.color.shell,
          border: `0.5px solid ${tokens.anyu.color.linen}`,
          borderRadius: tokens.anyu.radius.md,
        }}
      >
        暂无房间
      </div>
    );
  }

  return (
    <>
      <div
        style={{
          background: tokens.anyu.color.shell,
          border: `0.5px solid ${tokens.anyu.color.linen}`,
          borderRadius: tokens.anyu.radius.md,
          padding: 10,
        }}
      >
        {/* 顶部图例 - 颜色=渠道（只列当前画面出现的）+ 维修/业主自用屏蔽。条上文字仍是状态。 */}
        <div
          style={{
            display: "flex",
            flexWrap: "wrap",
            gap: 12,
            padding: "4px 4px 10px",
            fontSize: 10,
            color: tokens.anyu.color.driftwood,
          }}
        >
          {presentChannels.map((ch) => {
            const p = getChannelBarColors(ch);
            return <ChannelLegend key={ch} body={p.body} cap={p.cap} label={getChannelMeta(ch).label} />;
          })}
          <LegendDot color="#F97316" label="维修" />
          <LegendDot color="#9CA3AF" label="业主自用 / 屏蔽" />
        </div>

        {/* 主表 - sticky thead + sticky 左侧固定列(房型/房间) */}
        <div
          ref={scrollRef}
          style={{
            overflow: "auto",
            borderRadius: tokens.anyu.radius.sm,
            maxHeight: "calc(100dvh - 260px)",
            minHeight: 320,
            border: `0.5px solid ${tokens.anyu.color.linen}`,
            background: tokens.anyu.color.shell,
          }}
        >
          <table
            style={{
              fontSize: 11,
              borderCollapse: "separate",
              borderSpacing: 0,
              width: "max-content",
            }}
          >
            <thead ref={theadRef} style={{ position: "sticky", top: 0, zIndex: 4 }}>
              {/* 第一行:房型 + 房间 + 日期 */}
              <tr>
                {hasGroupCol && (
                  <th
                    rowSpan={2}
                    style={{
                      position: "sticky",
                      left: 0,
                      background: tokens.anyu.color.shell,
                      padding: "8px 10px",
                      textAlign: "left",
                      fontWeight: 500,
                      color: tokens.anyu.color.driftwood,
                      width: GROUP_COL_WIDTH,
                      minWidth: GROUP_COL_WIDTH,
                      fontSize: 10,
                      letterSpacing: ".02em",
                      zIndex: 5,
                      borderBottom: `0.5px solid ${tokens.anyu.color.linen}`,
                      borderRight: `0.5px solid ${tokens.anyu.color.linen}`,
                    }}
                  >
                    房型
                  </th>
                )}
                <th
                  rowSpan={2}
                  style={{
                    position: "sticky",
                    left: hasGroupCol ? GROUP_COL_WIDTH : 0,
                    background: tokens.anyu.color.shell,
                    padding: "8px 10px",
                    textAlign: "left",
                    fontWeight: 500,
                    color: tokens.anyu.color.driftwood,
                    width: ROOM_COL_WIDTH,
                    minWidth: ROOM_COL_WIDTH,
                    fontSize: 10,
                    letterSpacing: ".02em",
                    zIndex: 5,
                    borderBottom: `0.5px solid ${tokens.anyu.color.linen}`,
                    borderRight: `0.5px solid ${tokens.anyu.color.linen}`,
                  }}
                >
                  房间
                </th>
                {days.map((d) => {
                  const day = parseInt(d.split("-")[2]);
                  const month = parseInt(d.split("-")[1]);
                  const isToday = d === todayStr;
                  const weekday = new Date(d).getDay();
                  const isWeekend = weekday === 0 || weekday === 6;
                  return (
                    <th
                      key={d}
                      style={{
                        padding: "6px 0 2px",
                        width: cellWidth,
                        minWidth: cellWidth,
                        textAlign: "center",
                        fontWeight: isToday ? 700 : 500,
                        color: isToday
                          ? "#EF4444"
                          : isWeekend
                          ? "#EF4444"
                          : tokens.anyu.color.stone,
                        fontSize: 11,
                        background: isToday ? "#FEF2F2" : tokens.anyu.color.shell,
                      }}
                    >
                      <div style={{ lineHeight: 1.2 }}>
                        {String(month).padStart(2, "0")}-{String(day).padStart(2, "0")}
                      </div>
                      <div
                        style={{
                          fontSize: 9,
                          fontWeight: 400,
                          lineHeight: 1.2,
                          marginTop: 2,
                        }}
                      >
                        {isToday ? "今天" : `周${WEEKDAY[weekday]}`}
                      </div>
                    </th>
                  );
                })}
              </tr>
              {/* 第二行:每日占用统计 */}
              <tr>
                {days.map((d) => {
                  const o = occupancyByDate[d];
                  const isToday = d === todayStr;
                  const remaining = o ? o.total - o.used : 0;
                  const total = o?.total ?? 0;
                  return (
                    <th
                      key={d}
                      style={{
                        padding: "0 0 6px",
                        width: cellWidth,
                        minWidth: cellWidth,
                        textAlign: "center",
                        fontWeight: 400,
                        color: tokens.anyu.color.driftwood,
                        fontSize: 10,
                        background: isToday ? "#FEF2F2" : tokens.anyu.color.shell,
                        borderBottom: `0.5px solid ${tokens.anyu.color.linen}`,
                      }}
                    >
                      {total > 0 ? `剩 ${remaining}/${total}` : "—"}
                    </th>
                  );
                })}
              </tr>
            </thead>
            <tbody>
              {groups.map((g) => {
                const isUngrouped = g.groupName === UNGROUPED_LABEL;
                const displayName = isUngrouped ? "其他房型" : g.groupName;
                const groupBg = isUngrouped ? "#FFF7E6" : "#F5F1EA";
                const groupFg = isUngrouped ? "#D46B08" : tokens.anyu.color.ink.default;
                const groupBorderRight = `1.5px solid ${
                  isUngrouped ? "#FFD591" : tokens.anyu.color.linen
                }`;
                return (
                  <React.Fragment key={g.groupName}>
                    {g.items.map((room, roomIdxInGroup) => (
                      <tr
                        key={room.room_id}
                        style={{
                          contentVisibility: "auto" as React.CSSProperties["contentVisibility"],
                          containIntrinsicSize: `${rowHeight}px 1200px` as unknown as string,
                        }}
                      >
                        {/* 房型分组列:rowSpan 覆盖本组所有行; sticky 让长分组下滚仍能看到标题 */}
                        {hasGroupCol && roomIdxInGroup === 0 && (
                          <td
                            rowSpan={g.items.length}
                            style={{
                              position: "sticky",
                              left: 0,
                              top: 0,
                              background: groupBg,
                              padding: 0,
                              width: GROUP_COL_WIDTH,
                              minWidth: GROUP_COL_WIDTH,
                              verticalAlign: "top",
                              zIndex: 2,
                              borderBottom: `0.5px solid ${tokens.anyu.color.linen}`,
                              borderRight: groupBorderRight,
                            }}
                          >
                            <div
                              style={{
                                position: "sticky",
                                top: theadH,
                                padding: "10px 8px",
                                fontSize: 11,
                                fontWeight: 700,
                                color: groupFg,
                                lineHeight: 1.4,
                                wordBreak: "break-all",
                              }}
                            >
                              <div>
                                {isUngrouped ? "⚠ " : ""}
                                {displayName}
                              </div>
                              <div
                                style={{
                                  fontSize: 10,
                                  fontWeight: 400,
                                  marginTop: 4,
                                  color: tokens.anyu.color.driftwood,
                                }}
                              >
                                {g.items.length} 间
                              </div>
                            </div>
                          </td>
                        )}
                        {/* 房间列 */}
                        <td
                          style={{
                            position: "sticky",
                            left: hasGroupCol ? GROUP_COL_WIDTH : 0,
                            background: tokens.anyu.color.shell,
                            padding: "0 8px",
                            height: rowHeight,
                            zIndex: 2,
                            borderBottom: `0.5px solid ${tokens.anyu.color.linen}`,
                            borderRight: `0.5px solid ${tokens.anyu.color.linen}`,
                          }}
                        >
                          <div
                            style={{
                              display: "flex",
                              flexDirection: "column",
                              justifyContent: "center",
                              height: "100%",
                              minWidth: 0,
                            }}
                          >
                            <div
                              style={{
                                fontWeight: 600,
                                fontSize: 12,
                                color: tokens.anyu.color.ink.default,
                                lineHeight: 1.3,
                                overflow: "hidden",
                                textOverflow: "ellipsis",
                                whiteSpace: "nowrap",
                              }}
                            >
                              {room.room_id}
                            </div>
                            {room.room_name && (
                              <div
                                style={{
                                  fontSize: 10,
                                  color: tokens.anyu.color.driftwood,
                                  lineHeight: 1.3,
                                  marginTop: 1,
                                  overflow: "hidden",
                                  textOverflow: "ellipsis",
                                  whiteSpace: "nowrap",
                                }}
                              >
                                {room.room_name}
                              </div>
                            )}
                          </div>
                        </td>
                        {/* 日期单元格 */}
                        {days.map((day, idx) => {
                          const cell = room.days?.[day];
                          const prev = idx > 0 ? room.days?.[days[idx - 1]] : undefined;
                          const next = idx < days.length - 1 ? room.days?.[days[idx + 1]] : undefined;
                          const isStart = !!cell && (idx === 0 || !isSameBlock(prev, cell));
                          const isEnd = !!cell && (idx === days.length - 1 || !isSameBlock(cell, next));
                          const isToday = day === todayStr;
                          const bc = cell ? barColorsFor(cell) : null;
                          const isBlockCell = !!cell && cell.status.startsWith("block:");
                          const isCompletedCell = cell?.status === "completed";
                          const capColor = bc && isStart ? bc.cap : null;

                          return (
                            <td
                              key={day}
                              onClick={() => cell && openDetailFor(room.room_id, idx)}
                              style={{
                                padding: 0,
                                width: cellWidth,
                                minWidth: cellWidth,
                                height: rowHeight,
                                background: isToday ? "#FEF2F2" : "transparent",
                                borderBottom: `0.5px solid ${tokens.anyu.color.linen}`,
                                // 连续块内部右边线染成条底色,消除两格间灰缝
                                borderRight:
                                  cell && !isEnd
                                    ? `1px solid ${bc?.body}`
                                    : `0.5px solid ${tokens.anyu.color.linen}`,
                                cursor: cell ? "pointer" : "default",
                                position: "relative",
                              }}
                            >
                              {cell && bc ? (
                                <div
                                  style={{
                                    // 锁房/屏蔽用斜纹，与订单实色块区分
                                    background: isBlockCell
                                      ? `repeating-linear-gradient(45deg, ${bc.body}, ${bc.body} 5px, ${bc.body}cc 5px, ${bc.body}cc 10px)`
                                      : bc.body,
                                    color: bc.text,
                                    opacity: isCompletedCell ? 0.6 : 1,
                                    height: rowHeight - 10,
                                    marginTop: 5,
                                    marginBottom: 5,
                                    marginLeft: isStart ? 4 : 0,
                                    marginRight: isEnd ? 4 : 0,
                                    borderRadius:
                                      isStart && isEnd
                                        ? 6
                                        : isStart
                                        ? "6px 0 0 6px"
                                        : isEnd
                                        ? "0 6px 6px 0"
                                        : 0,
                                    padding: isStart ? (capColor ? "0 6px 0 9px" : "0 6px") : 0,
                                    display: "flex",
                                    alignItems: "center",
                                    fontSize: 10,
                                    fontWeight: 600,
                                    overflow: "hidden",
                                    whiteSpace: "nowrap",
                                    position: "relative",
                                  }}
                                  title={STATUS_LABEL[cell.status] || cell.status}
                                >
                                  {capColor && (
                                    <span
                                      aria-hidden
                                      style={{
                                        position: "absolute",
                                        left: 0,
                                        top: 0,
                                        bottom: 0,
                                        width: 4,
                                        background: capColor,
                                      }}
                                    />
                                  )}
                                  {isStart && (STATUS_LABEL[cell.status] || cell.status)}
                                </div>
                              ) : null}
                            </td>
                          );
                        })}
                      </tr>
                    ))}
                  </React.Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      {/* 点击色块 → 简洁详情弹窗(继续保持 PII 不下发) */}
      <Modal
        open={!!selected}
        onCancel={() => setSelected(null)}
        footer={null}
        title={selected?.roomName ?? selected?.roomId}
        width={320}
      >
        {selected && (
          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            <Row label="状态">
              <Tag
                color={
                  selected.status.startsWith("block:") && selected.status !== "block:maintenance"
                    ? "default"
                    : selected.status === "block:maintenance"
                    ? "orange"
                    : "green"
                }
              >
                {STATUS_LABEL[selected.status] || selected.status}
              </Tag>
            </Row>
            {selected.channel && (
              <Row label="渠道">
                <span style={{ color: tokens.anyu.color.ink.default }}>
                  {CHANNEL_LABEL[selected.channel] || selected.channel}
                </span>
              </Row>
            )}
            <Row label="日期">
              <span style={{ color: tokens.anyu.color.ink.default }}>
                {selected.startDate} → {selected.endDate}
              </span>
            </Row>
            <Row label="天数">
              <span style={{ color: tokens.anyu.color.ink.default }}>{selected.nights} 天</span>
            </Row>
            <div
              style={{
                marginTop: 6,
                fontSize: 11,
                color: tokens.anyu.color.driftwood,
                lineHeight: 1.5,
              }}
            >
              出于隐私合规考虑,业主端不展示客人姓名 / 手机号 / 订单金额。
            </div>
          </div>
        )}
      </Modal>
    </>
  );
}

function LegendDot({ color, label }: { color: string; label: string }) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
      <span
        style={{
          width: 12,
          height: 8,
          borderRadius: 2,
          background: color,
        }}
      />
      {label}
    </span>
  );
}

// 渠道图例小色块：淡底 + 左浓条，跟订单条一致
function ChannelLegend({ body, cap, label }: { body: string; cap: string; label: string }) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
      <span
        style={{ width: 14, height: 9, borderRadius: 2, background: body, borderLeft: `3px solid ${cap}` }}
      />
      {label}
    </span>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
      <span style={{ fontSize: 12, color: tokens.anyu.color.driftwood }}>{label}</span>
      <span style={{ fontSize: 13 }}>{children}</span>
    </div>
  );
}
