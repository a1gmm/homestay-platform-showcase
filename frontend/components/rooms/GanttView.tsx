"use client";

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Button, Tooltip, Dropdown, DatePicker, Tag, Popover, Segmented, Space } from "antd";
import type { MenuProps } from "antd";
import { buildLockMenuItems, type LockBlockType } from "./room-lock-actions";
import {
  LeftOutlined,
  RightOutlined,
  LockOutlined,
  MoreOutlined,
  EditOutlined,
  ThunderboltOutlined,
  TagsOutlined,
  CheckOutlined,
  RollbackOutlined,
  PlusOutlined,
  FilterOutlined,
  FullscreenOutlined,
  FullscreenExitOutlined,
} from "@ant-design/icons";
import type { CalendarRoom, CalendarDay } from "@/lib/types";
import { ROOM_STATUS, restoreTarget, RESTORABLE_STATUSES, CLEANING_STATUSES, CLEANING_CELL, TRIAL_BADGE } from "./constants";
import { tokens } from "@/lib/design-tokens";
import { todayCNString } from "@/lib/utils";
import { getChannelMeta } from "@/lib/channels";
import { getChannelBarColors } from "@/lib/channel-bar-palette";
import { getCompletionBarStyle, COMPLETION_LEGEND, stayGroupBarStatus } from "@/lib/gantt-bar-style";
import { buildWindowDays, computeWindowOccupancy, windowRangeLabel, shiftWindow, GANTT_LOOKBACK_DAYS } from "@/lib/gantt-window";
import { computeAutoScrollVelocity } from "@/lib/gantt-autoscroll";
import {
  ORDER_BAR_STATUS_TAG as BAR_STATUS_TAG,
  BLOCK_LABEL,
  orderStatusHex,
} from "@/lib/status-display";
import dayjs, { Dayjs } from "dayjs";
import { StaySettlementLabel } from "@/components/ui/StaySettlementLabel";

const WEEKDAY_LABEL = ["日", "一", "二", "三", "四", "五", "六"];

function statusColor(s?: string) {
  return orderStatusHex(s, tokens.color.brand.primary);
}

// channel 模式已取消订单：暖灰底 + 灰字（置灰，不误认为某个渠道）
const CH_CANCEL_BG = "#E4E2DE";
const CH_CANCEL_FG = "#8A857C";

// D2 底部状态快捷筛选：对标 bypms（新单/待排房/预抵/预离/在住/已完成/锁房）。
// 每个 key 给一个判定函数，命中的格子高亮、未命中的淡化，方便倒查/排房。
export type GanttStatusFilter =
  | "all"
  | "new"
  | "pending_room"
  | "arriving"
  | "leaving"
  | "in_house"
  | "done"
  | "blocked";

interface FilterDef {
  key: GanttStatusFilter;
  label: string;
  match: (day: CalendarDay, ctx: { date: string; todayStr: string }) => boolean;
}

export const GANTT_STATUS_FILTERS: FilterDef[] = [
  { key: "all", label: "全部", match: () => true },
  {
    key: "new",
    label: "新单",
    match: (d) => d.status === "pending_confirm" || d.status === "pending_payment",
  },
  {
    key: "pending_room",
    label: "待排房",
    match: (d) => d.status === "paid_pending_room",
  },
  {
    // 预抵：今天 check-in（该格是订单首晚且 == 今天）—— 用 block_start 无法判定订单首晚，
    // 这里用「该格日期 == 今天 且 该订单在今天有格」近似，组件内进一步用 isStartOfBlock 收敛。
    key: "arriving",
    label: "预抵",
    match: (d, { date, todayStr }) =>
      date === todayStr && (d.status === "roomed_pending_checkin" || d.status === "paid_pending_room"),
  },
  {
    key: "leaving",
    label: "预离",
    match: (d, { date, todayStr }) =>
      date === todayStr && (d.status === "checked_in" || d.status === "pending_checkout"),
  },
  {
    key: "in_house",
    label: "在住",
    match: (d) => d.status === "checked_in" || d.status === "pending_checkout",
  },
  { key: "done", label: "已完成", match: (d) => d.status === "completed" },
  {
    key: "blocked",
    label: "锁房",
    match: (d) => String(d.status).startsWith("block:") || d.order_id == null,
  },
];

// 同一订单/同一屏蔽块的连续判定：优先用 order_room_id（多房订单的同一房行），
// fallback 到 order_id（老数据无 order_room_id 字段）。status 相同
function isSameBlock(a?: CalendarDay, b?: CalendarDay): boolean {
  if (!a || !b) return false;
  // 续住关联组：同组相邻同房格连成一条连续横条（跨订单，即使各段状态不同）
  if (a.stay_group_id && b.stay_group_id) {
    return a.stay_group_id === b.stay_group_id;
  }
  // 多房：order_room_id 区分同订单不同房，避免同一订单跨房被错连
  if (a.order_room_id && b.order_room_id) {
    return a.order_room_id === b.order_room_id && a.status === b.status;
  }
  return (a.order_id ?? "") === (b.order_id ?? "") && a.status === b.status;
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
 * 一行内每一天该用哪个状态**上色**（completion 模式）。同一条连续横条（isSameBlock）
 * 的所有格取同一个「整条统一状态」，与后端 group_view 口径 1 一致（见 stayGroupBarStatus）。
 * 只影响颜色/条内小标；不碰 booking.status —— 拖拽闸、保洁叠标等仍读各格裸状态。
 * 单张单的横条状态本就一致，映射后与原状态相同，行为不变。
 */
function blockDisplayStatusByDay(
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
    // 收集从 i 起的一条连续横条（相邻 isSameBlock），按天序即入住先后
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

export interface RoomGroup {
  groupName: string;
  rooms: CalendarRoom[];
}

interface GanttViewProps {
  calendar: CalendarRoom[] | undefined;
  isLoading: boolean;
  /** D1 滚动窗口：起始日 YYYY-MM-DD */
  windowStart: string;
  /** D1 滚动窗口：天数（默认 30，可月/双周/周切换） */
  rangeDays: number;
  /** 改窗口起始日（DatePicker 选日 / 左右翻页都走它） */
  onWindowStartChange: (start: string) => void;
  /** 改窗口天数（月/双周/周视图切换） */
  onRangeDaysChange?: (days: number) => void;
  /** C2：高频动作上工具栏 —— 新建订单 */
  onCreateOrder?: () => void;
  /** C2：高频动作上工具栏 —— 筛选 / 批量 */
  onOpenBatch?: () => void;
  /** 点击订单块 */
  onOrderClick?: (orderId: string) => void;
  /** 点击空白单元格 → 新建订单预填房间+日期 */
  onCellClick?: (roomId: string, date: string, day: CalendarDay | null) => void;
  /** 拖拽排房：透传到外层处理冲突视觉/排房 */
  onCellDragOver?: (
    roomId: string,
    date: string,
    day: CalendarDay | null,
    e: React.DragEvent<HTMLTableCellElement>
  ) => void;
  onCellDrop?: (
    roomId: string,
    date: string,
    day: CalendarDay | null,
    e: React.DragEvent<HTMLTableCellElement>
  ) => void;
  /** issue#8: 已排房格子拖拽起点。父组件应在 dataTransfer 设置 source meta + setDraggingOrderId */
  onCellDragStart?: (
    orderId: string,
    source: { roomId: string; date: string; orderRoomId?: string },
    e: React.DragEvent<HTMLDivElement>
  ) => void;
  onCellDragEnd?: (e: React.DragEvent<HTMLDivElement>) => void;
  /** 当前正在被拖拽的订单 id（用于源块半透明视觉反馈） */
  draggingOrderId?: string | null;
  /** 房间分组：传则按分组渲染，否则把所有房间扔进单组「全部」 */
  groupedRooms?: RoomGroup[];
  /** 行右侧 hover 菜单回调 */
  onEditRoom?: (roomId: string) => void;
  onShowPricing?: (roomId: string) => void;
  onChangeRoomStatus?: (roomId: string) => void;
  /** 「点房号直接锁房」：行菜单锁房三类置顶，一步打开锁房弹窗 */
  onLockRoom?: (roomId: string, blockType: LockBlockType) => void;
  /** 房间名 → 单纯展示用，Map 形式从 page 传入 */
  roomNamesById?: Record<string, string>;
  /** 房间当前状态 Map（stored room_status，可写；菜单/一键恢复用它） */
  roomStatusById?: Record<string, string>;
  /** 房间按订单实时推算的有效状态 Map（在住/已预订/空置随订单自动算）；徽章显示用它 */
  effectiveStatusById?: Record<string, string>;
  /** 房间上一个状态 Map（用于维修/锁房一键恢复） */
  roomPrevStatusById?: Record<string, string | null | undefined>;
  /** 行内轻量切换房间状态（含一键恢复）；不传则状态标签只读 */
  onQuickChangeRoomStatus?: (roomId: string, status: string) => void;
  /** 解除时间段锁房（点甘特图灰色锁房条）；不传则灰条只读 */
  onReleaseBlock?: (blockId: string) => void;
  cellWidth?: number;
  rowHeight?: number;
  /** 订单条底色口径：status=按订单状态（默认，历史行为）；channel=按渠道分色；completion=按「办没办入住」分色（前台找待办用，见 lib/gantt-bar-style） */
  barColorMode?: "status" | "channel" | "completion";
  /** 全屏专注模式：为 true 时工具栏显示「退出全屏」、图例收进弹出、监听 ESC 退出 */
  focusMode?: boolean;
  /** 切换专注模式（进入/退出全屏）；不传则不渲染全屏按钮 */
  onToggleFocus?: () => void;
}

const ROOM_COL_WIDTH = 130;
const GROUP_COL_WIDTH = 132; // 房型分组列宽（只在多分组时显示）
const HEADER_HEIGHT = 50;
// 房型分组之间的分隔线：用一整行独立的色条实现（而非单元格 border），
// 避免被行虚拟化 content-visibility / rowSpan 吃掉，滚动也不会消失。
const GROUP_DIVIDER_COLOR = "#B89B5E"; // 比普通行线 (#E5DDCB) 明显加深
const GROUP_DIVIDER_H = 3; // 分隔条高度(px)

export function GanttView({
  calendar,
  isLoading,
  windowStart,
  rangeDays,
  onWindowStartChange,
  onRangeDaysChange,
  onCreateOrder,
  onOpenBatch,
  onOrderClick,
  onCellClick,
  onCellDragOver,
  onCellDrop,
  onCellDragStart,
  onCellDragEnd,
  draggingOrderId,
  groupedRooms,
  onEditRoom,
  onShowPricing,
  onChangeRoomStatus,
  onLockRoom,
  roomNamesById,
  roomStatusById,
  effectiveStatusById,
  roomPrevStatusById,
  onQuickChangeRoomStatus,
  onReleaseBlock,
  cellWidth = 80,
  rowHeight = 60,
  barColorMode = "status",
  focusMode = false,
  onToggleFocus,
}: GanttViewProps) {
  const todayStr = todayCNString();

  // 专注模式下按 ESC 退出全屏
  useEffect(() => {
    if (!focusMode || !onToggleFocus) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onToggleFocus();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [focusMode, onToggleFocus]);
  // D1：天数由 windowStart + rangeDays 决定，天然跨月/跨年。
  const days = useMemo(
    () => buildWindowDays(windowStart, rangeDays),
    [windowStart, rangeDays]
  );
  // D2 底部状态快捷筛选当前选中项
  const [activeFilter, setActiveFilter] = useState<GanttStatusFilter>("all");
  const activeFilterDef = GANTT_STATUS_FILTERS.find((f) => f.key === activeFilter) ?? GANTT_STATUS_FILTERS[0];

  // 没传分组就用单组「全部」
  const groups: RoomGroup[] = useMemo(() => {
    if (groupedRooms && groupedRooms.length > 0) return groupedRooms;
    return [{ groupName: "全部房间", rooms: calendar ?? [] }];
  }, [groupedRooms, calendar]);

  // channel 模式：当前画面里实际出现的渠道，做「颜色=渠道」图例。
  // 只列出现的（不是 11 个渠道全铺），避免图例过长；顺序按首次出现。
  const presentChannels = useMemo(() => {
    if (barColorMode !== "channel") return [] as string[];
    const seen = new Set<string>();
    const list: string[] = [];
    for (const room of calendar ?? []) {
      for (const key of Object.keys(room.days)) {
        const d = room.days[key];
        const ch = d?.channel;
        if (!ch || d.status === "cancelled" || String(d.status).startsWith("block:")) continue;
        if (!seen.has(ch)) {
          seen.add(ch);
          list.push(ch);
        }
      }
    }
    return list;
  }, [barColorMode, calendar]);

  // 真正展示房型分组列：仅当 groupedRooms 真的多分组（不是 fallback 的"全部房间"）
  const hasGroupCol = groups.length > 1;
  const fixedLeftWidth = hasGroupCol ? GROUP_COL_WIDTH + ROOM_COL_WIDTH : ROOM_COL_WIDTH;

  // 测量 thead 实际高度，给分组列里的 sticky 标题用作 top 偏移
  const theadRef = useRef<HTMLTableSectionElement>(null);
  const [theadH, setTheadH] = useState(72);
  useEffect(() => {
    const el = theadRef.current;
    if (!el) return;
    const update = () => setTheadH(el.getBoundingClientRect().height || 72);
    update();
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // 默认横向滚动到「今天」那一列（仅当今天落在当前窗口内）。
  // 防止前台打开房态时默认停在窗口首日、看不到今天的格子。
  const scrollRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (isLoading) return;
    const container = scrollRef.current;
    if (!container) return;
    const todayIdx = days.indexOf(todayStr);
    if (todayIdx < 0) return; // 今天不在窗口内（如倒查过去），不强制滚动
    const visibleGridW = container.clientWidth - fixedLeftWidth;
    const target = todayIdx * cellWidth - visibleGridW / 2 + cellWidth / 2;
    container.scrollLeft = Math.max(0, target);
  }, [isLoading, days, todayStr, cellWidth, fixedLeftWidth]);

  // 拖拽换房时的「边缘自动滚动」：原生 HTML5 拖拽期间浏览器屏蔽滚轮，
  // 用户无法滚到视口外的房间。拖到滚动容器上/下边缘时用 rAF 持续滚动补救。
  const autoScrollPointerY = useRef<number | null>(null);
  const autoScrollRaf = useRef<number | null>(null);
  const stopAutoScroll = useCallback(() => {
    if (autoScrollRaf.current != null) {
      cancelAnimationFrame(autoScrollRaf.current);
      autoScrollRaf.current = null;
    }
    autoScrollPointerY.current = null;
  }, []);
  const autoScrollTick = useCallback(() => {
    const el = scrollRef.current;
    const y = autoScrollPointerY.current;
    // 容器没有纵向溢出（房间少）时直接停，别空转
    if (!el || y == null || el.scrollHeight <= el.clientHeight) {
      autoScrollRaf.current = null;
      return;
    }
    const v = computeAutoScrollVelocity(y, el.getBoundingClientRect());
    // 离开上/下边缘区就停，下次 dragover 再启动 —— 避免中部拖拽时每帧强制回流
    if (v === 0) {
      autoScrollRaf.current = null;
      return;
    }
    el.scrollTop += v;
    autoScrollRaf.current = requestAnimationFrame(autoScrollTick);
  }, []);
  const handleAutoScrollDragOver = useCallback(
    (e: React.DragEvent<HTMLDivElement>) => {
      if (draggingOrderId == null) return; // 仅拖拽已排房卡片时启用
      autoScrollPointerY.current = e.clientY;
      if (autoScrollRaf.current == null) {
        autoScrollRaf.current = requestAnimationFrame(autoScrollTick);
      }
    },
    [draggingOrderId, autoScrollTick]
  );
  // 拖拽结束（含取消）时父组件会清空 draggingOrderId → 停止滚动；卸载时也清理。
  useEffect(() => {
    if (draggingOrderId == null) stopAutoScroll();
    return stopAutoScroll;
  }, [draggingOrderId, stopAutoScroll]);

  // 每日占用统计：用于表头第二行「入住率% · 剩 X」(D2)
  const occupancyByDate = useMemo(
    () => computeWindowOccupancy(days, (calendar ?? []) as CalendarRoom[]),
    [calendar, days]
  );

  return (
    <div
      style={{
        background: tokens.color.bg.container,
        border: `1px solid ${tokens.color.bg.border}`,
        borderRadius: tokens.radius.lg,
        padding: 14,
        boxShadow: tokens.shadow.sm,
      }}
    >
      {/* 顶栏 D1：滚动日期窗口导航 —— 自由选起始日 + 左右平移整段 + 月/双周/周视图 + C2 高频动作 */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 12,
          marginBottom: 10,
          flexWrap: "wrap",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          {/* 左右平移整段：步长 = 一周（与 bypms 一致，整段滚动而非翻月） */}
          <Tooltip title="前移一周">
            <Button
              icon={<LeftOutlined />}
              size="small"
              onClick={() => onWindowStartChange(dayjs(windowStart).subtract(7, "day").format("YYYY-MM-DD"))}
            />
          </Tooltip>
          {/* 自由选任意起始日（DatePicker 选日，天然跨月） */}
          <DatePicker
            size="small"
            value={dayjs(windowStart)}
            onChange={(d: Dayjs | null) => {
              if (d) onWindowStartChange(d.format("YYYY-MM-DD"));
            }}
            allowClear={false}
            format="MM.DD"
            style={{ width: 110 }}
            inputReadOnly
          />
          <Tooltip title="后移一周">
            <Button
              icon={<RightOutlined />}
              size="small"
              onClick={() => onWindowStartChange(dayjs(windowStart).add(7, "day").format("YYYY-MM-DD"))}
            />
          </Tooltip>
          <span
            style={{
              fontSize: 13,
              fontWeight: 600,
              color: tokens.color.text.secondary,
              minWidth: 96,
              textAlign: "center",
            }}
          >
            {windowRangeLabel(windowStart, rangeDays)}
          </span>
          <Button size="small" onClick={() => onWindowStartChange(shiftWindow(todayStr, -GANTT_LOOKBACK_DAYS))}>
            今天
          </Button>
          {onRangeDaysChange && (
            <Segmented
              size="small"
              value={rangeDays}
              onChange={(v) => onRangeDaysChange(Number(v))}
              options={[
                { label: "周", value: 7 },
                { label: "双周", value: 14 },
                { label: "月", value: 30 },
                { label: "60天", value: 60 },
              ]}
            />
          )}
        </div>
        {/* C2：把高频动作做成显眼工具栏按钮（前台投诉「找不到锁房/新增」） */}
        <Space size={8} wrap>
          {onToggleFocus && (
            <Tooltip title={focusMode ? "退出全屏 (Esc)" : "全屏看房态"}>
              <Button
                size="small"
                icon={focusMode ? <FullscreenExitOutlined /> : <FullscreenOutlined />}
                onClick={onToggleFocus}
              >
                {focusMode ? "退出全屏" : "全屏"}
              </Button>
            </Tooltip>
          )}
          {onOpenBatch && (
            <Button icon={<FilterOutlined />} size="small" onClick={onOpenBatch}>
              筛选 / 批量
            </Button>
          )}
          {onCreateOrder && (
            <Button type="primary" icon={<PlusOutlined />} size="small" onClick={onCreateOrder}>
              新建订单
            </Button>
          )}
        </Space>
      </div>

      {/* D2 底部状态快捷筛选（放顶部更显眼）：新单/待排房/预抵/预离/在住/已完成/锁房 */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          marginBottom: 12,
          flexWrap: "wrap",
        }}
      >
        {GANTT_STATUS_FILTERS.map((f) => {
          const active = f.key === activeFilter;
          return (
            <Button
              key={f.key}
              size="small"
              type={active ? "primary" : "default"}
              onClick={() => setActiveFilter(f.key)}
              style={{ fontSize: 12, borderRadius: 999, paddingInline: 12 }}
            >
              {f.label}
            </Button>
          );
        })}
        <span style={{ fontSize: 11, color: tokens.color.text.tertiary, marginLeft: 6 }}>
          {activeFilter === "all" ? "显示全部订单" : "已高亮匹配，其余淡化便于倒查"}
        </span>
      </div>

      {/* 渠道图例（仅 channel 模式）：颜色=哪个渠道，只列当前画面出现的渠道。
          专注模式下收进「图例▾」弹出，省一整行高度 */}
      {barColorMode === "channel" && presentChannels.length > 0 && (
        focusMode ? (
          <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 8 }}>
            <Popover
              placement="bottomRight"
              content={
                <div style={{ display: "flex", flexDirection: "column", gap: 6, maxWidth: 220 }}>
                  {presentChannels.map((ch) => {
                    const pal = getChannelBarColors(ch);
                    return (
                      <span
                        key={ch}
                        style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 12, color: tokens.color.text.secondary }}
                      >
                        <span
                          aria-hidden
                          style={{ width: 16, height: 11, borderRadius: 3, background: pal.body, borderLeft: `3px solid ${pal.cap}` }}
                        />
                        {getChannelMeta(ch).label}
                      </span>
                    );
                  })}
                </div>
              }
            >
              <Button size="small" type="text">图例 ▾</Button>
            </Popover>
          </div>
        ) : (
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 12,
              marginBottom: 12,
              flexWrap: "wrap",
            }}
          >
            <span style={{ fontSize: 11, color: tokens.color.text.tertiary }}>渠道</span>
            {presentChannels.map((ch) => {
              const pal = getChannelBarColors(ch);
              return (
                <span
                  key={ch}
                  style={{ display: "inline-flex", alignItems: "center", gap: 5, fontSize: 12, color: tokens.color.text.secondary }}
                >
                  <span
                    aria-hidden
                    style={{
                      width: 16,
                      height: 11,
                      borderRadius: 3,
                      background: pal.body,
                      borderLeft: `3px solid ${pal.cap}`,
                    }}
                  />
                  {getChannelMeta(ch).label}
                </span>
              );
            })}
          </div>
        )
      )}

      {/* 完成度图例（仅 completion 模式）：前台按「办没办入住」认色块。
          专注模式下收进「图例▾」弹出，省一整行高度 */}
      {barColorMode === "completion" && (
        focusMode ? (
          <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 8 }}>
            <Popover
              placement="bottomRight"
              content={
                <div style={{ display: "flex", flexDirection: "column", gap: 6, maxWidth: 220 }}>
                  {COMPLETION_LEGEND.map((l) => (
                    <span
                      key={l.label}
                      style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 12, color: tokens.color.text.secondary }}
                    >
                      <span
                        aria-hidden
                        style={{ width: 16, height: 11, borderRadius: 3, background: l.body, border: l.border ? `1px solid ${l.border}` : undefined }}
                      />
                      {l.label}
                    </span>
                  ))}
                </div>
              }
            >
              <Button size="small" type="text">图例 ▾</Button>
            </Popover>
          </div>
        ) : (
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 14,
              marginBottom: 12,
              flexWrap: "wrap",
            }}
          >
            {COMPLETION_LEGEND.map((l) => (
              <span
                key={l.label}
                style={{ display: "inline-flex", alignItems: "center", gap: 5, fontSize: 12, color: tokens.color.text.secondary }}
              >
                <span
                  aria-hidden
                  style={{
                    width: 16,
                    height: 11,
                    borderRadius: 3,
                    background: l.body,
                    border: l.border ? `1px solid ${l.border}` : undefined,
                  }}
                />
                {l.label}
              </span>
            ))}
          </div>
        )
      )}

      {/* 主表 */}
      <div
        ref={scrollRef}
        onDragOver={handleAutoScrollDragOver}
        onDrop={stopAutoScroll}
        style={{
          overflow: "auto",
          borderRadius: tokens.radius.md,
          // 专注模式砍掉了侧栏/顶栏/页头等 chrome，甘特可用高度更大
          maxHeight: focusMode ? "calc(100vh - 150px)" : "calc(100vh - 320px)",
          minHeight: 360,
          border: `1px solid ${tokens.color.bg.borderSubtle}`,
        }}
      >
        <table style={{ fontSize: 12, borderCollapse: "separate", borderSpacing: 0, width: "max-content" }}>
          {/* sticky thead：日期/占用行钉在容器顶部，避免下滚时看不到日期错录单 */}
          <thead ref={theadRef} style={{ position: "sticky", top: 0, zIndex: 2 }}>
            {/* 第一行：[房型 +] 房间列头 + 日期+周几 */}
            <tr>
              {hasGroupCol && (
                <th
                  rowSpan={2}
                  style={{
                    position: "sticky",
                    left: 0,
                    background: tokens.color.bg.page,
                    padding: "10px 12px",
                    textAlign: "left",
                    fontWeight: 500,
                    color: tokens.color.text.tertiary,
                    width: GROUP_COL_WIDTH,
                    minWidth: GROUP_COL_WIDTH,
                    fontSize: 11,
                    letterSpacing: ".02em",
                    zIndex: 3,
                    borderBottom: `1px solid ${tokens.color.bg.border}`,
                    borderRight: `1px solid ${tokens.color.bg.border}`,
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
                  background: tokens.color.bg.page,
                  padding: "10px 12px",
                  textAlign: "left",
                  fontWeight: 500,
                  color: tokens.color.text.tertiary,
                  width: ROOM_COL_WIDTH,
                  minWidth: ROOM_COL_WIDTH,
                  fontSize: 11,
                  letterSpacing: ".02em",
                  zIndex: 3,
                  borderBottom: `1px solid ${tokens.color.bg.border}`,
                  borderRight: `1px solid ${tokens.color.bg.border}`,
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
                        : tokens.color.text.secondary,
                      fontSize: 12,
                      background: isToday ? "#FEF2F2" : tokens.color.bg.page,
                    }}
                  >
                    <div style={{ lineHeight: 1.2 }}>
                      {String(month).padStart(2, "0")}-{String(day).padStart(2, "0")}
                    </div>
                    <div style={{ fontSize: 10, fontWeight: 400, lineHeight: 1.2, marginTop: 2 }}>
                      {isToday ? "今天" : `周${WEEKDAY_LABEL[weekday]}`}
                    </div>
                  </th>
                );
              })}
            </tr>
            {/* 第二行：每日入住率% + 空房数（D2）。入住率高用 clay 提示「快满」。 */}
            <tr>
              {days.map((d) => {
                const o = occupancyByDate[d];
                const isToday = d === todayStr;
                const total = o?.total ?? 0;
                const free = o?.free ?? 0;
                const rate = o?.rate ?? 0;
                const rateColor =
                  rate >= 90 ? "#C0392B" : rate >= 70 ? "#8A6E5A" : tokens.color.text.tertiary;
                return (
                  <th
                    key={d}
                    style={{
                      padding: "0 0 6px",
                      width: cellWidth,
                      minWidth: cellWidth,
                      textAlign: "center",
                      fontWeight: 400,
                      fontSize: 10,
                      background: isToday ? "#FEF2F2" : tokens.color.bg.page,
                      borderBottom: `1px solid ${tokens.color.bg.border}`,
                    }}
                  >
                    {total > 0 ? (
                      <Tooltip title={`入住率 = 当天有订单的房间占比（含「已预订·未入住」，故会高于页头「在住」数）。当天占用 ${total - free} 间 / 共 ${total} 间，空 ${free} 间。`}>
                        <div style={{ lineHeight: 1.3, cursor: "help" }}>
                          <div className="tabular" style={{ color: rateColor, fontWeight: 600 }}>
                            {rate}%
                          </div>
                          <div className="tabular" style={{ color: tokens.color.text.tertiary }}>
                            空 {free}
                          </div>
                        </div>
                      </Tooltip>
                    ) : (
                      <span style={{ color: tokens.color.text.tertiary }}>—</span>
                    )}
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {isLoading ? (
              <tr>
                <td
                  colSpan={days.length + (hasGroupCol ? 2 : 1)}
                  style={{ padding: 32, textAlign: "center", color: tokens.color.text.tertiary }}
                >
                  加载中...
                </td>
              </tr>
            ) : (
              groups.map((g, groupIdx) => {
                const isUngrouped = g.groupName === "未分组";
                const groupBg = isUngrouped ? "#FFF7E6" : tokens.color.brand.primarySoft;
                const groupFg = isUngrouped ? "#D46B08" : tokens.color.brand.primary;
                const groupBorderRight = `2px solid ${isUngrouped ? "#FFD591" : tokens.color.brand.primary}`;
                return (
                <React.Fragment key={g.groupName}>
                  {g.rooms.map((room, roomIdxInGroup) => {
                    // curStatus = 可写的 stored room_status，菜单勾选/禁用/一键恢复都用它
                    const curStatus = roomStatusById?.[room.room_id] ?? room.room_status;
                    const prevStatus = roomPrevStatusById?.[room.room_id];
                    // displayStatus = 按订单实时推算的有效态，徽章与锁图标显示用它
                    // （在住/已预订/空置随订单自动算，物理态维修/锁房/待清扫优先保留）
                    const displayStatus =
                      effectiveStatusById?.[room.room_id] ?? curStatus;
                    const isLocked =
                      displayStatus === "maintenance" || displayStatus === "locked";
                    const statusMeta = ROOM_STATUS[displayStatus] ?? ROOM_STATUS.available;
                    // 保洁流程态（待清扫/清扫中）：网格里只在「今天」那格铺一种「保洁中」色，
                    // 让前台一眼看到哪间在走保洁流程（左列徽章仍细分两色）。
                    const isCleaning = CLEANING_STATUSES.includes(displayStatus);
                    // completion 模式取色用：一条续住横条整条取同一状态（对齐详情页 group_view），
                    // 消除「绿一半蓝一半」的花条。只喂颜色，booking.status 原样保留给拖拽/保洁判定。
                    const barStatusByDay = blockDisplayStatusByDay(days, room.days);

                    // 行内轻量状态切换菜单（点状态标签弹出）
                    const statusMenuItems: MenuProps["items"] = onQuickChangeRoomStatus
                      ? [
                          // 维修/锁房时置顶「一键恢复上一个状态」
                          ...(RESTORABLE_STATUSES.includes(curStatus)
                            ? [
                                {
                                  key: "__restore__",
                                  icon: <RollbackOutlined style={{ color: tokens.color.brand.primary }} />,
                                  label: (
                                    <span style={{ fontWeight: 600, color: tokens.color.brand.primary }}>
                                      结束{statusMeta.label} · 恢复为
                                      {ROOM_STATUS[restoreTarget(prevStatus)]?.label ?? "空置"}
                                    </span>
                                  ),
                                  onClick: () =>
                                    onQuickChangeRoomStatus(room.room_id, restoreTarget(prevStatus)),
                                },
                                { type: "divider" as const },
                              ]
                            : []),
                          ...Object.entries(ROOM_STATUS).map(([key, meta]) => ({
                            key,
                            icon: (
                              <span
                                style={{
                                  display: "inline-block",
                                  width: 8,
                                  height: 8,
                                  borderRadius: "50%",
                                  background: meta.bg,
                                  border: `1px solid ${tokens.color.bg.border}`,
                                }}
                              />
                            ),
                            label: (
                              <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                                {meta.label}
                                {key === curStatus && (
                                  <CheckOutlined style={{ fontSize: 10, color: tokens.color.brand.primary }} />
                                )}
                              </span>
                            ),
                            disabled: key === curStatus,
                            onClick: () => onQuickChangeRoomStatus(room.room_id, key),
                          })),
                        ]
                      : [];

                    // 行右侧 hover 菜单（锁房三类置顶 —— 点房号直接锁房）
                    const menuItems = [
                      ...(onLockRoom
                        ? [
                            ...buildLockMenuItems((bt) => onLockRoom(room.room_id, bt)),
                            { type: "divider" as const },
                          ]
                        : []),
                      onEditRoom && {
                        key: "edit",
                        icon: <EditOutlined />,
                        label: "编辑房间",
                        onClick: () => onEditRoom(room.room_id),
                      },
                      onShowPricing && {
                        key: "pricing",
                        icon: <ThunderboltOutlined />,
                        label: "7 日定价",
                        onClick: () => onShowPricing(room.room_id),
                      },
                      onChangeRoomStatus && {
                        key: "status",
                        icon: <TagsOutlined />,
                        label: "修改状态",
                        onClick: () => onChangeRoomStatus(room.room_id),
                      },
                    ].filter(Boolean) as NonNullable<MenuProps["items"]>;

                    return (
                      <tr key={room.room_id}>
                        {/* 分组列：仅在多分组且本组首行时渲染（rowSpan 覆盖本组所有行）；
                            内部用 sticky div 让分组标题随滚动停在 thead 下方，长分组也不丢失上下文 */}
                        {hasGroupCol && roomIdxInGroup === 0 && (
                          <td
                            rowSpan={g.rooms.length}
                            style={{
                              position: "sticky",
                              left: 0,
                              top: 0,
                              background: groupBg,
                              padding: 0,
                              width: GROUP_COL_WIDTH,
                              minWidth: GROUP_COL_WIDTH,
                              verticalAlign: "top",
                              zIndex: 1,
                              borderBottom: `1px solid ${tokens.color.bg.border}`,
                              borderRight: groupBorderRight,
                            }}
                          >
                            <div
                              style={{
                                position: "sticky",
                                top: theadH,
                                padding: "10px 10px",
                                fontSize: 12,
                                fontWeight: 700,
                                color: groupFg,
                                lineHeight: 1.4,
                                wordBreak: "break-all",
                              }}
                            >
                              <div>
                                {isUngrouped ? "⚠ " : ""}
                                {g.groupName}
                              </div>
                              <div
                                style={{
                                  fontSize: 11,
                                  fontWeight: 400,
                                  marginTop: 4,
                                  color: tokens.color.text.tertiary,
                                }}
                              >
                                {g.rooms.length} 间
                              </div>
                            </div>
                          </td>
                        )}
                        <td
                          style={{
                            position: "sticky",
                            left: hasGroupCol ? GROUP_COL_WIDTH : 0,
                            background: tokens.color.bg.container,
                            padding: "0 8px",
                            height: rowHeight,
                            zIndex: 1,
                            borderBottom: `1px solid ${tokens.color.bg.borderSubtle}`,
                            borderRight: `1px solid ${tokens.color.bg.border}`,
                          }}
                        >
                          <div
                            style={{
                              display: "flex",
                              alignItems: "center",
                              justifyContent: "space-between",
                              gap: 4,
                              height: "100%",
                            }}
                          >
                            <div style={{ minWidth: 0, flex: 1 }}>
                              <div
                                style={{
                                  display: "flex",
                                  alignItems: "center",
                                  gap: 4,
                                  fontWeight: 600,
                                  fontSize: 13,
                                  color: tokens.color.text.primary,
                                  lineHeight: 1.3,
                                }}
                              >
                                {isLocked && (
                                  <LockOutlined style={{ fontSize: 11, color: tokens.color.text.tertiary }} />
                                )}
                                <span
                                  style={{
                                    overflow: "hidden",
                                    textOverflow: "ellipsis",
                                    whiteSpace: "nowrap",
                                  }}
                                >
                                  {room.room_id}
                                </span>
                              </div>
                              {(roomNamesById?.[room.room_id] || room.room_name) && (
                                <div
                                  style={{
                                    fontSize: 10,
                                    color: tokens.color.text.tertiary,
                                    lineHeight: 1.3,
                                    marginTop: 1,
                                    overflow: "hidden",
                                    textOverflow: "ellipsis",
                                    whiteSpace: "nowrap",
                                  }}
                                >
                                  {roomNamesById?.[room.room_id] || room.room_name}
                                </div>
                              )}
                              {/* 行内状态标签：点击直接切换状态（含维修/锁房一键恢复） */}
                              <div style={{ marginTop: 3 }}>
                                {statusMenuItems.length > 0 ? (
                                  <Dropdown
                                    menu={{ items: statusMenuItems }}
                                    trigger={["click"]}
                                    placement="bottomLeft"
                                  >
                                    <Tag
                                      color={statusMeta.color}
                                      style={{
                                        margin: 0,
                                        fontSize: 10,
                                        lineHeight: "16px",
                                        padding: "0 6px",
                                        cursor: "pointer",
                                        userSelect: "none",
                                      }}
                                      onClick={(e) => e.stopPropagation()}
                                    >
                                      {statusMeta.label} ▾
                                    </Tag>
                                  </Dropdown>
                                ) : (
                                  <Tag
                                    color={statusMeta.color}
                                    style={{ margin: 0, fontSize: 10, lineHeight: "16px", padding: "0 6px" }}
                                  >
                                    {statusMeta.label}
                                  </Tag>
                                )}
                              </div>
                            </div>
                            {menuItems.length > 0 && (
                              <Dropdown
                                menu={{ items: menuItems }}
                                trigger={["click"]}
                                placement="bottomRight"
                              >
                                <Button
                                  type="text"
                                  size="small"
                                  icon={<MoreOutlined />}
                                  onClick={(e) => e.stopPropagation()}
                                  className="gantt-row-action"
                                  style={{ flex: "0 0 auto" }}
                                />
                              </Dropdown>
                            )}
                          </div>
                        </td>
                        {days.map((day, idx) => {
                          const booking = room.days?.[day];
                          const prevDay = days[idx - 1];
                          const nextDay = days[idx + 1];
                          const prevBooking = prevDay ? room.days?.[prevDay] : undefined;
                          const nextBooking = nextDay ? room.days?.[nextDay] : undefined;
                          const isStartOfBlock =
                            !!booking && (idx === 0 || !isSameBlock(prevBooking, booking));
                          const isStartOfSettlementLabels =
                            !!booking &&
                            (isStartOfBlock || startsSettlementLabels(prevBooking, booking));
                          const isEndOfBlock =
                            !!booking &&
                            (idx === days.length - 1 || !isSameBlock(booking, nextBooking));
                          const isToday = day === todayStr;
                          const isBlock =
                            !!booking && (booking.order_id == null || String(booking.status).startsWith("block:"));
                          // 已离店的单（已退房/已完成）不该在「今天」盖住「保洁中」：客人已走、房在打扫，
                          // 该显示保洁中，而非残留的「待退房」条（提前退房 / 当天进退但退房日仍挂后面时会触发）。
                          // 「即将到店/在住」的单不算离店，照常画条——保住 turnaround（走一个来一个）能看到新客。
                          const departedBooking =
                            !!booking &&
                            (booking.status === "pending_checkout" || booking.status === "completed");
                          const nextIsToday = nextDay === todayStr;
                          // 保洁中叠标（王总 2026-07-21，方案B「叠在旧订单最后一格」）：
                          // 把小「保洁中」金标叠到刚退房订单条的最后一格上——订单条本身照旧
                          // （入住连到最后一晚、渠道色、点开详情都不变），只在末格右上角压一个金标。
                          // 命中条件：这一格是某已退房订单的末格，且清扫就发生在今天——
                          //   · 末格本身就是今天（提前退房，订单条仍挂到今天）；或
                          //   · 末格是昨天、今天那格已空（退房日当天最常见：客人已走，条停在昨晚）。
                          const cleaningOnBar =
                            isCleaning && departedBooking && isEndOfBlock && (isToday || nextIsToday);
                          // 今天格空着、旁边也没有可叠标的已退房订单条（客人早已离店、房仍没清扫）——
                          // 退化成整格金色「保洁中」，至少让前台知道这房还没清扫、不能进新客。
                          const prevIsDepartedBar =
                            !!prevBooking &&
                            (prevBooking.status === "pending_checkout" ||
                              prevBooking.status === "completed");
                          const cleaningEmptyCell =
                            isCleaning && isToday && !booking && !prevIsDepartedBar;
                          const cellInteractive = !!(
                            onOrderClick ||
                            onCellClick ||
                            onCellDragOver ||
                            onCellDrop
                          );

                          const guestName = booking?.guest_name ?? "";
                          const channelMeta = booking?.channel ? getChannelMeta(booking.channel) : null;
                          const channel = channelMeta?.label ?? "";
                          // channel 模式：渠道分色（C2 淡底浓条）。非 block、非取消才取渠道色板。
                          const isChannelMode = barColorMode === "channel";
                          // completion 模式：按「办没办入住」分色（前台找待办）。非 block 才走。
                          const isCompletionMode = barColorMode === "completion";
                          const isCancelled = booking?.status === "cancelled";
                          const barPal =
                            isChannelMode && booking && !isBlock && !isCancelled && booking.channel
                              ? getChannelBarColors(booking.channel)
                              : null;
                          // completion 模式的完成度样式（block 不走，仍用状态色）
                          const compStyle =
                            isCompletionMode && booking && !isBlock
                              ? getCompletionBarStyle(barStatusByDay[day] ?? booking.status)
                              : null;
                          // 底色/字色：completion 优先；否则渠道色板；channel 模式取消置灰；否则回退状态色白字（历史行为）
                          let barBg: string;
                          let barFg: string;
                          if (compStyle) {
                            barBg = compStyle.body;
                            barFg = compStyle.text;
                          } else if (barPal) {
                            barBg = barPal.body;
                            barFg = barPal.text;
                          } else if (isChannelMode && isCancelled) {
                            barBg = CH_CANCEL_BG;
                            barFg = CH_CANCEL_FG;
                          } else {
                            barBg = statusColor(booking?.status);
                            barFg = "#FFFFFF";
                          }
                          // 左侧浓色条：只在块起点画一截（多日连续块不重复），一眼识别渠道
                          const capColor = barPal && isStartOfBlock ? barPal.cap : null;
                          // completion 模式：未办入住的闪点提醒
                          const showTodoPulse = !!compStyle?.pulse;
                          // 渠道 chip：channel 模式整条已是渠道色故隐掉；completion 模式主打「完成度」保持清爽也隐掉
                          // （渠道当辅助信息仍在悬停 tooltip 里）；仅历史 status 模式显示
                          const showChannelChip = !isChannelMode && !isCompletionMode;
                          // 条内状态小标：completion 用完成度标（待办/已入住…）；channel 用淡雅状态词
                          const statusTag = compStyle
                            ? compStyle.badge ?? null
                            : isChannelMode && !isBlock && !isCancelled
                            ? BAR_STATUS_TAG[String(booking?.status ?? "")] ?? null
                            : null;
                          // 状态标背景：淡彩底上用极淡黑压一层即可
                          const tagScrim = "rgba(0,0,0,0.08)";
                          // 已完成整条略淡，往后退，进一步弱化「过去时」
                          const completedFade = booking?.status === "completed" ? 0.6 : 1;
                          const blockText = isBlock
                            ? BLOCK_LABEL[String(booking?.status).replace(/^block:/, "")] || guestName
                            : "";
                          // issue#8: 拖拽条件 — cancelled/pending_checkout 禁拖。
                          // checked_in（在住）可拖：#238 已接好换房自动换码链路
                          // （撤旧码+新房下码+飞书卡+确认提示），落点侧 rooms/page.tsx 按
                          // isCheckedIn 走换码路径。
                          // completed（已完成）可拖，但落点只支持「对调另一单」订正历史房号，
                          // 落到空房不处理（后端 SWAP_ALLOWED_STATUSES 只放开对调，不放开单房换房）。
                          const canDrag = !!(
                            booking?.order_id &&
                            !isBlock &&
                            !["cancelled", "pending_checkout"].includes(
                              String(booking?.status ?? "")
                            ) &&
                            onCellDragStart
                          );
                          const isBeingDragged =
                            draggingOrderId != null && booking?.order_id === draggingOrderId;
                          // D2 状态筛选：未命中的订单格淡化（all 时全亮）
                          const matchesFilter =
                            !booking ||
                            activeFilter === "all" ||
                            activeFilterDef.match(booking, { date: day, todayStr });
                          const dimmed = !!booking && !matchesFilter;
                          // D2 在条上显示 ￥价格：起点格汇总整段总价（连续同块日的 price 之和）
                          // 顺带数这条横条底下有几张单（续住组用）：同块格子里不同的 order_id 个数。
                          // 段数从格子推，不为一个标去多发请求。
                          let blockTotalPrice: number | null = null;
                          let blockSegments = 0;
                          if (booking && !isBlock && isStartOfBlock) {
                            let sum = 0;
                            let any = false;
                            const orderIds = new Set<string>();
                            for (let j = idx; j < days.length; j++) {
                              const dd = room.days?.[days[j]];
                              if (!dd || !isSameBlock(booking, dd)) break;
                              if (dd.order_id) orderIds.add(dd.order_id);
                              if (dd.price != null) {
                                sum += Number(dd.price);
                                any = true;
                              }
                            }
                            blockTotalPrice = any ? Math.round(sum) : null;
                            blockSegments = orderIds.size;
                          }
                          // 「续住 N 段」标：只在续住组的横条起点画。条本身早已按 stay_group_id 连成一条
                          // （isSameBlock），这个标是告诉前台「这一条底下是 N 张单」。
                          const showStaySegTag = !!booking?.stay_group_id && isStartOfBlock && blockSegments > 1;

                          return (
                            <td
                              key={day}
                              onClick={() => {
                                if (booking?.order_id && onOrderClick) {
                                  onOrderClick(booking.order_id);
                                } else if (!booking && onCellClick) {
                                  onCellClick(room.room_id, day, null);
                                }
                              }}
                              onDragOver={
                                onCellDragOver
                                  ? (e) => onCellDragOver(room.room_id, day, booking ?? null, e)
                                  : undefined
                              }
                              onDrop={
                                onCellDrop
                                  ? (e) => onCellDrop(room.room_id, day, booking ?? null, e)
                                  : undefined
                              }
                              style={{
                                padding: 0,
                                width: cellWidth,
                                minWidth: cellWidth,
                                height: rowHeight,
                                background: isToday ? "#FEF2F2" : "transparent",
                                borderBottom: `1px solid ${tokens.color.bg.borderSubtle}`,
                                // 同一订单连续多日时，把 td 的 1px 右边线染成条底色，避免两格之间出现灰色竖线
                                borderRight:
                                  booking && !isEndOfBlock
                                    ? `1px solid ${barBg}`
                                    : `1px solid ${tokens.color.bg.borderSubtle}`,
                                cursor: cellInteractive ? "pointer" : "default",
                                position: "relative",
                              }}
                            >
                              {booking ? ((() => {
                                const cellContent = (
                                <div
                                  draggable={canDrag}
                                  onDragStart={
                                    canDrag && booking.order_id
                                      ? (e) => {
                                          e.stopPropagation();
                                          onCellDragStart?.(
                                            booking.order_id as string,
                                            { roomId: room.room_id, date: day, orderRoomId: booking.order_room_id },
                                            e
                                          );
                                        }
                                      : undefined
                                  }
                                  onDragEnd={canDrag ? (e) => onCellDragEnd?.(e) : undefined}
                                  style={{
                                    background: barBg,
                                    color: barFg,
                                    height: rowHeight - 8,
                                    marginTop: 4,
                                    marginBottom: 4,
                                    // 块连续：起点 4px 左外边距，终点 4px 右外边距，中间 0
                                    marginLeft: isStartOfBlock ? 4 : 0,
                                    marginRight: isEndOfBlock ? 4 : 0,
                                    borderRadius:
                                      isStartOfBlock && isEndOfBlock
                                        ? 6
                                        : isStartOfBlock
                                        ? "6px 0 0 6px"
                                        : isEndOfBlock
                                        ? "0 6px 6px 0"
                                        : 0,
                                    // 起点格留左内边距；有浓色条时再多留 5px 给条腾位
                                    padding: isStartOfBlock ? (capColor ? "6px 8px 6px 13px" : "6px 8px") : 0,
                                    overflow: "hidden",
                                    display: "flex",
                                    flexDirection: "column",
                                    justifyContent: "center",
                                    position: "relative",
                                    cursor: canDrag ? "grab" : "default",
                                    opacity: isBeingDragged ? 0.4 : dimmed ? 0.28 : completedFade,
                                    transition: "opacity 0.15s",
                                  }}
                                  title={
                                    isBlock
                                      ? `${blockText}（屏蔽）`
                                      : `${guestName}${channel ? " · " + channel : ""}${
                                          blockTotalPrice != null ? " · ¥" + blockTotalPrice : ""
                                        }${canDrag ? " · 可拖拽换房/换日期" : ""}`
                                  }
                                >
                                  {capColor && (
                                    <span
                                      aria-hidden
                                      style={{
                                        position: "absolute",
                                        left: 0,
                                        top: 0,
                                        bottom: 0,
                                        width: 5,
                                        background: capColor,
                                      }}
                                    />
                                  )}
                                  {isStartOfBlock && (
                                    <>
                                      <div
                                        style={{
                                          fontSize: 13,
                                          fontWeight: 600,
                                          lineHeight: 1.2,
                                          overflow: "hidden",
                                          textOverflow: "ellipsis",
                                          whiteSpace: "nowrap",
                                          textDecoration: compStyle?.cancelled ? "line-through" : undefined,
                                        }}
                                      >
                                        {showTodoPulse && (
                                          <span
                                            aria-hidden
                                            className="gantt-todo-dot"
                                            style={{
                                              display: "inline-block",
                                              width: 7,
                                              height: 7,
                                              borderRadius: "50%",
                                              background: barFg,
                                              marginRight: 5,
                                              verticalAlign: "middle",
                                            }}
                                          />
                                        )}
                                        {isBlock ? blockText : guestName}
                                      </div>
                                      {!isBlock &&
                                        ((channelMeta && showChannelChip) ||
                                          statusTag ||
                                          showStaySegTag ||
                                          booking?.trial_tag ||
                                          booking?.stay_settlement_kind ||
                                          booking?.is_manually_managed ||
                                          booking?.free_room_kind === "all" ||
                                          booking?.free_room_kind === "mixed" ||
                                          blockTotalPrice != null) && (
                                        <div
                                          style={{
                                            display: "flex",
                                            alignItems: "center",
                                            gap: 4,
                                            marginTop: 3,
                                            maxWidth: "100%",
                                          }}
                                        >
                                          {/* 试运营房型角标（备注含极海）：金色小标，压在最前一眼识别 */}
                                          {booking?.trial_tag && (
                                            <span
                                              style={{
                                                flex: "0 0 auto",
                                                padding: "1px 5px",
                                                fontSize: 10,
                                                fontWeight: 700,
                                                color: TRIAL_BADGE.fg,
                                                background: TRIAL_BADGE.bg,
                                                borderRadius: 3,
                                                lineHeight: 1.4,
                                                whiteSpace: "nowrap",
                                              }}
                                            >
                                              {booking.trial_tag}
                                            </span>
                                          )}
                                          {isStartOfSettlementLabels && booking?.stay_settlement_kind ? (
                                            <StaySettlementLabel kind={booking.stay_settlement_kind} compact />
                                          ) : null}
                                          {isStartOfSettlementLabels && booking?.is_manually_managed ? (
                                            <StaySettlementLabel kind="manual_override" compact />
                                          ) : null}
                                          {!booking?.stay_settlement_kind &&
                                            (booking?.free_room_kind === "all" || booking?.free_room_kind === "mixed") && (
                                            <span
                                              aria-label={booking.free_room_kind === "mixed" ? "含免房" : "免房"}
                                              style={{
                                                flex: "0 0 auto",
                                                padding: "1px 5px",
                                                fontSize: 10,
                                                fontWeight: 500,
                                                color: barFg,
                                                background: "transparent",
                                                border: `1px solid ${barFg}`,
                                                borderRadius: 999,
                                                lineHeight: 1.4,
                                                whiteSpace: "nowrap",
                                              }}
                                            >
                                              {booking.free_room_kind === "mixed" ? "含免房" : "免房"}
                                            </span>
                                          )}
                                          {/* 续住组：这一条横条底下是 N 张单。点任意一天都进同一个整段详情页。 */}
                                          {showStaySegTag && (
                                            <span
                                              style={{
                                                flex: "0 0 auto",
                                                padding: "1px 5px",
                                                fontSize: 10,
                                                fontWeight: 600,
                                                color: barFg,
                                                background: tagScrim,
                                                borderRadius: 3,
                                                lineHeight: 1.4,
                                                whiteSpace: "nowrap",
                                              }}
                                            >
                                              续住 {blockSegments} 段
                                            </span>
                                          )}
                                          {statusTag && (
                                            <span
                                              style={{
                                                flex: "0 0 auto",
                                                padding: "1px 5px",
                                                fontSize: 10,
                                                fontWeight: 600,
                                                color: barFg,
                                                background: tagScrim,
                                                borderRadius: 3,
                                                lineHeight: 1.4,
                                                whiteSpace: "nowrap",
                                              }}
                                            >
                                              {statusTag}
                                            </span>
                                          )}
                                          {channelMeta && showChannelChip && (
                                            <span
                                              style={{
                                                display: "inline-flex",
                                                alignItems: "center",
                                                flex: "0 0 auto",
                                                padding: "1px 5px",
                                                fontSize: 10,
                                                fontWeight: 600,
                                                color: channelMeta.color,
                                                background: channelMeta.bgColor,
                                                borderRadius: 3,
                                                lineHeight: 1.4,
                                                whiteSpace: "nowrap",
                                              }}
                                            >
                                              {channelMeta.label}
                                            </span>
                                          )}
                                          {blockTotalPrice != null && (
                                            <span
                                              className="tabular"
                                              style={{
                                                flex: "0 0 auto",
                                                fontSize: 11,
                                                fontWeight: 600,
                                                color: barFg,
                                                lineHeight: 1.4,
                                                whiteSpace: "nowrap",
                                              }}
                                            >
                                              ¥{blockTotalPrice}
                                            </span>
                                          )}
                                        </div>
                                      )}
                                    </>
                                  )}
                                  {!isStartOfBlock && isStartOfSettlementLabels ? (
                                    <div
                                      style={{
                                        display: "flex",
                                        alignItems: "center",
                                        gap: 4,
                                        marginLeft: 4,
                                      }}
                                    >
                                      {booking?.stay_settlement_kind ? (
                                        <StaySettlementLabel kind={booking.stay_settlement_kind} compact />
                                      ) : null}
                                      {booking?.is_manually_managed ? (
                                        <StaySettlementLabel kind="manual_override" compact />
                                      ) : null}
                                    </div>
                                  ) : null}
                                </div>
                                );
                                // 时间段锁房灰条：点一下弹出「解除锁房」+ 起止/备注
                                const blk = booking;
                                if (isBlock && blk.block_id && onReleaseBlock) {
                                  return (
                                    <Popover
                                      trigger="click"
                                      placement="top"
                                      title={`${blockText}锁房`}
                                      content={
                                        <div style={{ maxWidth: 220 }}>
                                          <div
                                            style={{
                                              fontSize: 12,
                                              color: tokens.color.text.secondary,
                                              marginBottom: 10,
                                            }}
                                          >
                                            {blk.block_start ?? ""}
                                            {blk.block_end ? ` 至 ${blk.block_end}（不含当天）` : ""}
                                            {blk.block_reason ? (
                                              <div style={{ marginTop: 2 }}>备注：{blk.block_reason}</div>
                                            ) : null}
                                          </div>
                                          <Button
                                            danger
                                            size="small"
                                            block
                                            icon={<RollbackOutlined />}
                                            onClick={() => onReleaseBlock(blk.block_id as string)}
                                          >
                                            解除锁房
                                          </Button>
                                        </div>
                                      }
                                    >
                                      {cellContent}
                                    </Popover>
                                  );
                                }
                                return cellContent;
                              })()) : cleaningEmptyCell ? (
                                // 兜底金盒：今天格空、旁边也没有可叠标的已退房订单条
                                //（客人早已离店、房仍没清扫）。整格金色「保洁中」，
                                // 至少让前台知道这房还没清扫、不能进新客。
                                <div
                                  title={CLEANING_CELL.label}
                                  style={{
                                    background: CLEANING_CELL.bg,
                                    color: CLEANING_CELL.fg,
                                    height: rowHeight - 8,
                                    margin: 4,
                                    borderRadius: 6,
                                    display: "flex",
                                    alignItems: "center",
                                    justifyContent: "center",
                                    fontSize: 11,
                                    fontWeight: 600,
                                    lineHeight: 1.2,
                                    overflow: "hidden",
                                    whiteSpace: "nowrap",
                                    textOverflow: "ellipsis",
                                    padding: "0 4px",
                                  }}
                                >
                                  {CLEANING_CELL.label}
                                </div>
                              ) : null}
                              {/* 保洁中叠标：金标叠在刚退房订单条的最后一格右上角，订单条照旧可点开详情。
                                  pointerEvents:none → 点击穿透到 td，照常打开订单详情。 */}
                              {cleaningOnBar && (
                                <span
                                  aria-hidden
                                  title={CLEANING_CELL.label}
                                  style={{
                                    position: "absolute",
                                    top: 3,
                                    right: 3,
                                    // 比吸顶表头 thead(z-index:2) 低一档，否则行滚到表头下面时
                                    // 金标会从表头里「顶穿」出来，看着像飘到日期行上（王总 2026-07-22 反馈）。
                                    zIndex: 1,
                                    pointerEvents: "none",
                                    display: "inline-flex",
                                    alignItems: "center",
                                    maxWidth: "calc(100% - 6px)",
                                    padding: "1px 5px",
                                    fontSize: 9,
                                    fontWeight: 700,
                                    lineHeight: 1.3,
                                    color: CLEANING_CELL.fg,
                                    background: CLEANING_CELL.bg,
                                    border: "1px solid rgba(0,0,0,0.08)",
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
                    );
                  })}
                  {/* 房型分组分隔行：独立一整行的色条，跨「房型列 + 房号列 + 所有日期格」，
                      用 background 而非 border，滚动/虚拟化都不会丢失。最后一个分组后不画。 */}
                  {groupIdx < groups.length - 1 && (
                    <tr aria-hidden="true">
                      {hasGroupCol && (
                        <td
                          style={{
                            position: "sticky",
                            left: 0,
                            zIndex: 1,
                            background: GROUP_DIVIDER_COLOR,
                            padding: 0,
                            height: GROUP_DIVIDER_H,
                          }}
                        />
                      )}
                      <td
                        style={{
                          position: "sticky",
                          left: hasGroupCol ? GROUP_COL_WIDTH : 0,
                          zIndex: 1,
                          background: GROUP_DIVIDER_COLOR,
                          padding: 0,
                          height: GROUP_DIVIDER_H,
                        }}
                      />
                      {days.map((day) => (
                        <td
                          key={day}
                          style={{
                            background: GROUP_DIVIDER_COLOR,
                            padding: 0,
                            height: GROUP_DIVIDER_H,
                          }}
                        />
                      ))}
                    </tr>
                  )}
                </React.Fragment>
                );
              })
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
