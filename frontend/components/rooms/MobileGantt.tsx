"use client";

import React, { useEffect, useMemo, useRef, useState } from "react";
import { Modal, Tag, Segmented } from "antd";
import { tokens } from "@/lib/design-tokens";
import { todayCNString } from "@/lib/utils";
import { CHANNEL_LABEL } from "@/lib/channels";
import { getCompletionBarStyle, COMPLETION_LEGEND, stayGroupBarStatus } from "@/lib/gantt-bar-style";
import { groupByRoomType, UNGROUPED_LABEL } from "@/lib/room-types";
import { ORDER_STATUS_LABEL, orderStatusHex } from "@/lib/status-display";
import type { CalendarRoom, CalendarDay } from "@/lib/types";
import { CLEANING_STATUSES, CLEANING_CELL, TRIAL_BADGE } from "./constants";
import { StaySettlementLabel } from "@/components/ui/StaySettlementLabel";

// 后台移动端房态甘特图（路线 B）。
// 仿已上线的 components/owner/OwnerGanttView：sticky 双轴 + 状态色条 + 滚到今天 + 行虚拟化。
// 与业主版区别：用带 PII 的后台 calendar 数据，条内显示客人姓名。
// #79 只做只读渲染 + 只读详情；点空格开单 / 点条进订单详情抽屉由 #81 接。

const WEEKDAY = ["日", "一", "二", "三", "四", "五", "六"];

// 中国法定节假日(简) — 顶行展示节日小标签，与客户竞品对齐(如端午)。
// 仅列主要节日当天；新年份在此补充即可。
const HOLIDAYS: Record<string, string> = {
  "2026-01-01": "元旦",
  "2026-02-17": "春节",
  "2026-04-05": "清明",
  "2026-05-01": "劳动",
  "2026-06-19": "端午",
  "2026-09-25": "中秋",
  "2026-10-01": "国庆",
};

function statusColor(s?: string): string {
  // 移动端维修块高亮橙（桌面甘特是统一灰）——现状行为，特意保留
  if (s === "block:maintenance") return "#F97316";
  return orderStatusHex(s, tokens.anyu.color.sage);
}

function statusLabel(s: string): string {
  return ORDER_STATUS_LABEL[s] || s;
}

// 移动端订单条按「完成度」分色（与桌面 GanttView completion 模式同一色源 lib/gantt-bar-style）。
// 前台盯甘特找的是「还没办入住的活」：未办亮橙 / 已办浅绿 / 待退浅蓝 / 取消灰。
// 移动窄条塞不下状态小标 → 靠颜色区分，具体状态点开详情看。
// displayStatus：续住整条统一状态（对齐详情页 group_view），传入则用它取色，消除「花条」；
// 不传则回退本格裸状态（单张单本就一致）。block: 段仍用物理态色，不受影响。
function barColorsFor(
  cell: CalendarDay,
  displayStatus?: string | null,
): { body: string; text: string; cap: string | null } {
  const s = cell.status;
  if (s?.startsWith("block:")) return { body: statusColor(s), text: "#fff", cap: null };
  const cs = getCompletionBarStyle(displayStatus ?? s);
  return { body: cs.body, text: cs.text, cap: null };
}

function ymdLocal(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

// 同一段连续判定：续住组优先按 stay_group_id——同组多张单在移动端要连成一条横条
// （与桌面 GanttView.isSameBlock 同口径；此前只认 order_id，续住组在手机上断成多截，
// 前台会以为客人中途退过房）。无组再按 order_id（订单）/ block_id（锁房），最后退回 status。
// 前缀 g:/避免 stay_group_id 与 order_id 意外撞值；有组 vs 无组的相邻格不相连。
export function blockKey(cell?: CalendarDay): string | null {
  if (!cell) return null;
  if (cell.stay_group_id) return `g:${cell.stay_group_id}`;
  return cell.order_id || cell.block_id || `s:${cell.status}`;
}
export function isSameBlock(a?: CalendarDay, b?: CalendarDay): boolean {
  const ka = blockKey(a);
  const kb = blockKey(b);
  return ka != null && ka === kb;
}

function startsSettlementLabels(previous?: CalendarDay, current?: CalendarDay): boolean {
  if (!current) return false;
  return (
    !previous ||
    previous.stay_settlement_kind !== current.stay_settlement_kind ||
    Boolean(previous.is_manually_managed) !== Boolean(current.is_manually_managed)
  );
}

/**
 * 一行内每一天该用哪个状态**上色**：同一条连续横条（isSameBlock）取同一个「整组统一状态」，
 * 与后端 group_view 口径 1 一致（见 stayGroupBarStatus）。只喂颜色，cell.status 原样保留。
 * 单张单横条状态本就一致，映射后不变。桌面 GanttView 有同名口径，两端一致。
 */
export function blockDisplayStatusByDay(
  days: string[],
  roomDays?: Record<string, CalendarDay>,
): Record<string, string | null | undefined> {
  const out: Record<string, string | null | undefined> = {};
  if (!roomDays) return out;
  let i = 0;
  while (i < days.length) {
    if (!roomDays[days[i]]) {
      i++;
      continue;
    }
    const blockDays: string[] = [];
    const statuses: (string | null | undefined)[] = [];
    let j = i;
    while (j < days.length) {
      const cur = roomDays[days[j]];
      if (!cur) break;
      if (j > i && !isSameBlock(roomDays[days[j - 1]], cur)) break;
      blockDays.push(days[j]);
      statuses.push(cur.status);
      j++;
    }
    const unified = stayGroupBarStatus(statuses);
    for (const d of blockDays) out[d] = unified;
    i = j;
  }
  return out;
}

interface BlockInfo {
  roomId: string;
  roomName: string;
  startDate: string;
  endDate: string;
  nights: number;
  status: string;
  channel: string | null;
  guestName: string | null;
}

export interface MobileGanttProps {
  rooms: CalendarRoom[];
  calMonth: { year: number; month: number };
  rowHeight?: number;
  /** room_id → 房型(分组)。传入则按房型分组显示；不传则扁平列表。 */
  roomTypeById?: Record<string, string | null | undefined>;
  /** 点订单条 → 打开订单详情抽屉（不传则回退到内置只读详情）。 */
  onOrderClick?: (orderId: string) => void;
  /** 点空格(空房×日期) → 快速排房/开单。 */
  onCellClick?: (roomId: string, date: string) => void;
  /** room_id → 有效房态。传入则在今天那格给保洁房(待清扫/清扫中)铺「保洁中」色。 */
  effectiveStatusById?: Record<string, string>;
}

const ROOM_COL_WIDTH = 96;
const GROUP_COL_WIDTH = 104;

export function MobileGantt({
  rooms,
  calMonth,
  rowHeight = 44,
  roomTypeById,
  onOrderClick,
  onCellClick,
  effectiveStatusById,
}: MobileGanttProps) {
  const todayStr = todayCNString();
  const daysInMonth = new Date(calMonth.year, calMonth.month, 0).getDate();
  const days = useMemo(
    () =>
      Array.from({ length: daysInMonth }, (_, i) =>
        ymdLocal(new Date(calMonth.year, calMonth.month - 1, i + 1)),
      ),
    [calMonth.year, calMonth.month, daysInMonth],
  );

  // 续住整条统一取色表：roomId → (day → 整组状态)。渲染时查表喂 barColorsFor，消除花条。
  const barStatusByRoom = useMemo(() => {
    const m: Record<string, Record<string, string | null | undefined>> = {};
    for (const r of rooms) m[r.room_id] = blockDisplayStatusByDay(days, r.days);
    return m;
  }, [rooms, days]);

  // 按房型分组（传入 roomTypeById 时）。分组数 >1 才显示房型列。
  const groups = useMemo(
    () => groupByRoomType(rooms, (r) => roomTypeById?.[r.room_id]),
    [rooms, roomTypeById],
  );
  const hasGroupCol = groups.length > 1;
  const fixedLeftWidth = hasGroupCol ? GROUP_COL_WIDTH + ROOM_COL_WIDTH : ROOM_COL_WIDTH;

  // sticky 分组标题高度跟随 thead 实测（长分组下滚仍能看到房型名）
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

  // 密度切换：宽=3天(信息全)，窄=7天(看全局)。列宽随之变化。
  const [density, setDensity] = useState<"wide" | "narrow">("wide");
  const cellWidth = density === "wide" ? 92 : 48;

  // 横屏提示（#85 屏幕适配回归）：竖屏时提醒可横屏看更多天，横屏自动隐藏。
  // SSR 安全：初值 false，首帧不渲染提示，挂载后按真实朝向更新，避免水合不一致。
  const [isPortrait, setIsPortrait] = useState(false);
  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mq = window.matchMedia("(orientation: portrait)");
    const update = () => setIsPortrait(mq.matches);
    update();
    mq.addEventListener?.("change", update);
    return () => mq.removeEventListener?.("change", update);
  }, []);

  // 横向滚到「今天」那一列（首次挂载 + 点「回今天」按钮复用）
  const scrollRef = useRef<HTMLDivElement>(null);
  function scrollToToday(smooth = false) {
    const container = scrollRef.current;
    if (!container) return;
    const [ty, tm, td] = todayStr.split("-").map(Number);
    if (ty !== calMonth.year || tm !== calMonth.month) {
      container.scrollTo({ left: 0, behavior: smooth ? "smooth" : "auto" });
      return;
    }
    const todayIdx = td - 1;
    const visibleGridW = container.clientWidth - fixedLeftWidth;
    const target = todayIdx * cellWidth - visibleGridW / 2 + cellWidth / 2;
    container.scrollTo({ left: Math.max(0, target), behavior: smooth ? "smooth" : "auto" });
  }
  useEffect(() => {
    scrollToToday(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [calMonth.year, calMonth.month, todayStr, cellWidth]);

  // 每日剩余可订房量 = 总房 - 不可订房。
  // 不可订 = 当天有订单(非取消) 或 被锁房/维修/屏蔽。取消订单不占房。
  const occupancyByDate = useMemo(() => {
    const total = rooms.length;
    const m: Record<string, { used: number; total: number }> = {};
    days.forEach((d) => {
      const used = rooms.filter((r) => {
        const cell = r.days?.[d];
        return !!cell && cell.status !== "cancelled";
      }).length;
      m[d] = { used, total };
    });
    return m;
  }, [rooms, days]);

  const [selected, setSelected] = useState<BlockInfo | null>(null);

  function openDetailFor(roomId: string, dayIdx: number) {
    const room = rooms.find((r) => r.room_id === roomId);
    if (!room) return;
    const cell = room.days?.[days[dayIdx]];
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
      channel: cell.channel ?? null,
      guestName: cell.guest_name || null,
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
        {/* 工具栏：密度切换 + 回今天 */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 8,
            padding: "2px 2px 10px",
          }}
        >
          <Segmented
            size="small"
            value={density}
            onChange={(v) => setDensity(v as "wide" | "narrow")}
            options={[
              { label: "3 天", value: "wide" },
              { label: "7 天", value: "narrow" },
            ]}
          />
          <button
            type="button"
            onClick={() => scrollToToday(true)}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 4,
              height: 28,
              padding: "0 12px",
              borderRadius: 999,
              border: `0.5px solid ${tokens.anyu.color.linen}`,
              background: tokens.anyu.color.shell,
              color: tokens.anyu.color.ink.default,
              fontSize: 12,
              cursor: "pointer",
            }}
          >
            回今天
          </button>
        </div>

        {/* 图例：颜色=完成度（办没办入住）+ 维修/锁房 */}
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
          {COMPLETION_LEGEND.map((l) => (
            <LegendDot key={l.label} color={l.body} label={l.label} />
          ))}
          <LegendDot color="#F97316" label="维修" />
          <LegendDot color="#9CA3AF" label="锁房/屏蔽" />
        </div>

        {/* 横屏提示（#85）：竖屏时提示横屏可看更多天；横屏自动隐藏。 */}
        {isPortrait && (
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 6,
              margin: "0 2px 8px",
              padding: "6px 10px",
              borderRadius: tokens.anyu.radius.sm,
              background: tokens.anyu.color.sand,
              color: tokens.anyu.color.stone,
              fontSize: 11,
              lineHeight: 1.4,
            }}
          >
            <span aria-hidden style={{ fontSize: 13 }}>⟳</span>
            <span>横屏可看更多天</span>
          </div>
        )}

        <div
          ref={scrollRef}
          style={{
            overflow: "auto",
            borderRadius: tokens.anyu.radius.sm,
            maxHeight: "calc(100dvh - 240px)",
            minHeight: 320,
            border: `0.5px solid ${tokens.anyu.color.linen}`,
            background: tokens.anyu.color.shell,
            WebkitOverflowScrolling: "touch",
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
                    zIndex: 5,
                    borderBottom: `0.5px solid ${tokens.anyu.color.linen}`,
                    borderRight: `0.5px solid ${tokens.anyu.color.linen}`,
                  }}
                >
                  房间
                </th>
                {days.map((d) => {
                  const [yy, mm, dd] = d.split("-").map(Number);
                  const day = dd;
                  const month = mm;
                  const isToday = d === todayStr;
                  // 本地构造，避免 new Date("YYYY-MM-DD") 按 UTC 解析导致跨时区周几错位
                  const weekday = new Date(yy, mm - 1, dd).getDay();
                  const isWeekend = weekday === 0 || weekday === 6;
                  const holiday = HOLIDAYS[d];
                  return (
                    <th
                      key={d}
                      style={{
                        padding: "6px 0 2px",
                        width: cellWidth,
                        minWidth: cellWidth,
                        textAlign: "center",
                        fontWeight: isToday ? 700 : 500,
                        color: isToday ? "#EF4444" : isWeekend ? "#EF4444" : tokens.anyu.color.stone,
                        fontSize: 11,
                        background: isToday ? "#FEF2F2" : tokens.anyu.color.shell,
                      }}
                    >
                      <div style={{ lineHeight: 1.2 }}>
                        {String(month).padStart(2, "0")}-{String(day).padStart(2, "0")}
                      </div>
                      <div style={{ fontSize: 9, fontWeight: 400, lineHeight: 1.2, marginTop: 2 }}>
                        {holiday ? (
                          <span style={{ color: "#D46B08", fontWeight: 600 }}>{holiday}</span>
                        ) : isToday ? (
                          "今天"
                        ) : (
                          `周${WEEKDAY[weekday]}`
                        )}
                      </div>
                    </th>
                  );
                })}
              </tr>
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
                      {total > 0 ? `剩 ${remaining}` : "—"}
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
                  {hasGroupCol && roomIdxInGroup === 0 && (
                    <td
                      rowSpan={g.items.length}
                      style={{
                        position: "sticky",
                        left: 0,
                        background: groupBg,
                        padding: 0,
                        width: GROUP_COL_WIDTH,
                        minWidth: GROUP_COL_WIDTH,
                        verticalAlign: "top",
                        zIndex: 2,
                        borderBottom: `0.5px solid ${tokens.anyu.color.linen}`,
                        borderRight: `1.5px solid ${isUngrouped ? "#FFD591" : tokens.anyu.color.linen}`,
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
                        <div>{isUngrouped ? "⚠ " : ""}{displayName}</div>
                        <div style={{ fontSize: 10, fontWeight: 400, marginTop: 4, color: tokens.anyu.color.driftwood }}>
                          {g.items.length} 间
                        </div>
                      </div>
                    </td>
                  )}
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
                  {days.map((day, idx) => {
                    const cell = room.days?.[day];
                    const prev = idx > 0 ? room.days?.[days[idx - 1]] : undefined;
                    const next = idx < days.length - 1 ? room.days?.[days[idx + 1]] : undefined;
                    const isStart = !!cell && (idx === 0 || !isSameBlock(prev, cell));
                    const isStartOfSettlementLabels =
                      !!cell && (isStart || startsSettlementLabels(prev, cell));
                    const isEnd = !!cell && (idx === days.length - 1 || !isSameBlock(cell, next));
                    const isToday = day === todayStr;
                    // 已离店的单（已退房/已完成）不在「今天」盖住「保洁中」：客人已走、房在打扫，
                    // 该显示保洁中而非残留「待退房」条（与桌面 GanttView 同口径）。在住/将到店照常画。
                    const departedCell =
                      !!cell && (cell.status === "pending_checkout" || cell.status === "completed");
                    const isCleaningRoom = CLEANING_STATUSES.includes(
                      effectiveStatusById?.[room.room_id] ?? ""
                    );
                    const nextIsToday = idx < days.length - 1 && days[idx + 1] === todayStr;
                    // 保洁中叠标（王总 2026-07-21，方案B「叠在旧订单最后一格」，与桌面 GanttView 同口径）：
                    // 把小「保洁中」金标叠到刚退房订单条的最后一格上——订单条照旧可点开详情。命中：
                    // 这一格是已退房订单的末格，且清扫就在今天（末格是今天，或末格是昨天而今天已空）。
                    const cleaningOnBar =
                      isCleaningRoom && departedCell && isEnd && (isToday || nextIsToday);
                    // 今天格空、旁边也没有可叠标的已退房订单条（客人早已离店、房仍没清扫）——
                    // 退化成整格金色「保洁中」兜底，让前台知道这房还没清扫、不能进新客。
                    const prevIsDepartedBar =
                      !!prev && (prev.status === "pending_checkout" || prev.status === "completed");
                    const cleaningEmptyCell =
                      isToday && isCleaningRoom && !cell && !prevIsDepartedBar;
                    const bc = cell
                      ? barColorsFor(cell, barStatusByRoom[room.room_id]?.[day])
                      : null;
                    const isBlockCell = !!cell && cell.status.startsWith("block:");
                    const isCompletedCell = cell?.status === "completed";
                    const capColor = bc && isStart ? bc.cap : null;
                    const barLabel = cell
                      ? cell.status.startsWith("block:")
                        ? statusLabel(cell.status)
                        : cell.guest_name || statusLabel(cell.status)
                      : "";

                    return (
                      <td
                        key={day}
                        onClick={() => {
                          if (cell) {
                            // 有订单 → 订单详情抽屉；锁房/屏蔽或无回调 → 内置只读详情
                            if (cell.order_id && onOrderClick) onOrderClick(cell.order_id);
                            else openDetailFor(room.room_id, idx);
                          } else if (onCellClick) {
                            // 空格 → 快速排房/开单
                            onCellClick(room.room_id, day);
                          }
                        }}
                        style={{
                          padding: 0,
                          width: cellWidth,
                          minWidth: cellWidth,
                          height: rowHeight,
                          background: isToday ? "#FEF2F2" : "transparent",
                          borderBottom: `0.5px solid ${tokens.anyu.color.linen}`,
                          borderRight:
                            cell && !isEnd
                              ? `1px solid ${bc?.body}`
                              : `0.5px solid ${tokens.anyu.color.linen}`,
                          cursor: cell || onCellClick ? "pointer" : "default",
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
                              gap: 3,
                              fontSize: 10,
                              fontWeight: 600,
                              overflow: "hidden",
                              whiteSpace: "nowrap",
                              position: "relative",
                            }}
                            title={barLabel}
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
                            {isStart && (
                              <span
                                style={{
                                  overflow: "hidden",
                                  textOverflow: "ellipsis",
                                  minWidth: 0,
                                }}
                              >
                                {barLabel}
                              </span>
                            )}
                            {isStartOfSettlementLabels && cell.stay_settlement_kind ? (
                              <StaySettlementLabel kind={cell.stay_settlement_kind} compact />
                            ) : null}
                            {isStartOfSettlementLabels && cell.is_manually_managed ? (
                              <StaySettlementLabel kind="manual_override" compact />
                            ) : null}
                            {/* 试运营房型角标（备注含极海）：金色小标 */}
                            {isStart && cell.trial_tag && (
                              <span
                                style={{
                                  flex: "0 0 auto",
                                  padding: "0 4px",
                                  fontSize: 9,
                                  fontWeight: 700,
                                  color: TRIAL_BADGE.fg,
                                  background: TRIAL_BADGE.bg,
                                  borderRadius: 3,
                                  lineHeight: 1.5,
                                  whiteSpace: "nowrap",
                                }}
                              >
                                {cell.trial_tag}
                              </span>
                            )}
                            {isStart && !cell.stay_settlement_kind && (cell.free_room_kind === "all" || cell.free_room_kind === "mixed") && (
                              <span
                                aria-label={cell.free_room_kind === "mixed" ? "含免房" : "免房"}
                                style={{
                                  flex: "0 0 auto",
                                  padding: "0 4px",
                                  fontSize: 9,
                                  fontWeight: 500,
                                  color: "currentColor",
                                  background: "transparent",
                                  border: "1px solid currentColor",
                                  borderRadius: 999,
                                  lineHeight: 1.5,
                                  whiteSpace: "nowrap",
                                }}
                              >
                                {cell.free_room_kind === "mixed" ? "含免房" : "免房"}
                              </span>
                            )}
                          </div>
                        ) : cleaningEmptyCell ? (
                          // 兜底金盒：今天格空、旁边也没有可叠标的已退房订单条
                          //（客人早已离店、房仍没清扫）。整格金色「保洁中」，让前台知道不能进新客。
                          <div
                            title={CLEANING_CELL.label}
                            style={{
                              background: CLEANING_CELL.bg,
                              color: CLEANING_CELL.fg,
                              height: rowHeight - 10,
                              margin: 5,
                              borderRadius: 6,
                              display: "flex",
                              alignItems: "center",
                              justifyContent: "center",
                              fontSize: 10,
                              fontWeight: 600,
                              overflow: "hidden",
                              whiteSpace: "nowrap",
                              padding: "0 3px",
                            }}
                          >
                            {CLEANING_CELL.label}
                          </div>
                        ) : null}
                        {/* 保洁中叠标：金标叠在刚退房订单条最后一格右上角，订单条照旧可点开详情。pointerEvents:none → 点击穿透。 */}
                        {cleaningOnBar && (
                          <span
                            aria-hidden
                            title={CLEANING_CELL.label}
                            style={{
                              position: "absolute",
                              top: 3,
                              right: 3,
                              zIndex: 2,
                              pointerEvents: "none",
                              maxWidth: "calc(100% - 6px)",
                              padding: "0 4px",
                              fontSize: 8,
                              fontWeight: 700,
                              lineHeight: 1.4,
                              color: CLEANING_CELL.fg,
                              background: CLEANING_CELL.bg,
                              border: "0.5px solid rgba(0,0,0,0.08)",
                              borderRadius: 4,
                              boxShadow: "0 1px 2px rgba(0,0,0,0.15)",
                              whiteSpace: "nowrap",
                              overflow: "hidden",
                              textOverflow: "ellipsis",
                            }}
                          >
                            {CLEANING_CELL.label}
                          </span>
                        )}
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

      {/* 只读详情（#81 将替换为订单详情抽屉 + 流转动作） */}
      <Modal
        open={!!selected}
        onCancel={() => setSelected(null)}
        footer={null}
        title={selected ? `${selected.roomId}${selected.roomName ? " · " + selected.roomName : ""}` : ""}
        width={320}
      >
        {selected && (
          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            {selected.guestName && !selected.status.startsWith("block:") && (
              <Row label="客人">
                <span style={{ color: tokens.anyu.color.ink.default }}>{selected.guestName}</span>
              </Row>
            )}
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
                {statusLabel(selected.status)}
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
          </div>
        )}
      </Modal>
    </>
  );
}

function LegendDot({ color, label }: { color: string; label: string }) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
      <span style={{ width: 12, height: 8, borderRadius: 2, background: color }} />
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
