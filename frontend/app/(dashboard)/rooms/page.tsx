"use client";

import React, { useMemo, useState } from "react";
import { extractErrorMessage } from "@/lib/api-errors";
import { useSearchParams } from "next/navigation";
import { useQuery, useMutation, useQueryClient, keepPreviousData } from "@tanstack/react-query";
import { roomsApi, ordersApi, financeApi, roomBlocksApi, ownersApi } from "@/lib/api";
import { invalidateOrderRelated, invalidatePaymentRelated } from "@/lib/order-cache";
import { planDragConfirm } from "@/lib/drag-reschedule";
import type { RoomOut, RoomPricingPoint, CalendarRoom, OrderOut, PaymentOut, OwnerOut, OrderRoomOut } from "@/lib/types";
import {
  Card, Space, Button, Row, Skeleton,
  Modal, Select, message, Input, Form, Segmented,
  Drawer, DatePicker,
} from "antd";
import dayjs from "dayjs";
import {
  AppstoreOutlined,
  TableOutlined,
  PlusOutlined,
  ThunderboltOutlined,
  FilterOutlined,
  CalendarOutlined,
  ExclamationCircleFilled,
} from "@ant-design/icons";
import {
  RoomCard, GanttView, MobileGantt, PricingDetailModal,
  PendingRoomStrip, AssignRoomModal, QuickCreateOrderModal, BatchOrdersDrawer,
  OrderQuickSearch,
  TodayRoomList,
  ROOM_STATUS,
  type PricingDay, type PricingDetail, type QuickCreateInit,
  type RoomGroup,
} from "@/components/rooms";
import { OrderDetailModal, PaymentModal } from "@/components/orders";
import { formatRoomStatusSummary } from "@/lib/room-status-summary";
import type { OrderStatus, PaymentCreate } from "@/lib/types";
import { useIsMobile } from "@/lib/responsive";
import { useRoomsFocus } from "@/hooks/useRoomsFocus";
import { useAuthStore } from "@/lib/auth";
import { groupByRoomType } from "@/lib/room-types";
import { shiftWindow, GANTT_LOOKBACK_DAYS, windowFocusMonth } from "@/lib/gantt-window";
import { useRoomBlocking, type BlockType } from "@/hooks/useRoomBlocking";
import { RoomBlockModal } from "@/components/rooms/RoomBlockModal";
import { RoomStatusModal } from "@/components/rooms/RoomStatusModal";
import { RoomEditModal } from "@/components/rooms/RoomEditModal";
import { RoomPricingDrawer } from "@/components/rooms/RoomPricingDrawer";
import { useDragReschedule, type DragSource } from "@/hooks/useDragReschedule";
import { PageHeader } from "@/components/ui/PageHeader";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { tokens } from "@/lib/design-tokens";

// 待排房订单池入口开关。
// bypms 自建单接口不带房型（unitName 为空），同步进来只能在本系统手动排房，
// 故该入口须常开（2026-07-08 交接后由操作员自行排房）。甘特图拖拽排房不受此开关影响。
const SHOW_PENDING_ROOM_STRIP = true;

export default function RoomsPage() {
  const isMobile = useIsMobile();
  const today = new Date();
  const qc = useQueryClient();
  type View = "grid" | "gantt" | "today";
  // 移动端默认甘特图（客户明确要求；OwnerGanttView 已验证移动甘特可行）。房卡作备选。
  const [view, setView] = useState<View>("gantt");

  // ─── 全屏专注模式（接线抽到 useRoomsFocus，含手机首帧竞态守卫）──────────
  const role = useAuthStore((s) => s.user?.role);
  const { toggleFocus, focusActive } = useRoomsFocus({ isMobile, role, view, setView });
  const [calMonth, setCalMonth] = useState({ year: today.getFullYear(), month: today.getMonth() + 1 });
  // D1 滚动日期窗口（甘特图用）：起始日默认「今天 - GANTT_LOOKBACK_DAYS」，
  // 这样今天落在第 3 列，左侧能看到最近两天（方便看今天/前两天要退房的），往后 30 天，天然跨月。
  const todayYmd = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}-${String(today.getDate()).padStart(2, "0")}`;
  const [windowStart, setWindowStart] = useState<string>(() => shiftWindow(todayYmd, -GANTT_LOOKBACK_DAYS));
  const [rangeDays, setRangeDays] = useState<number>(30);
  // 移动端甘特按自然月渲染。缩放窗口跨 1024px 断点时会把桌面 GanttView 换成 MobileGantt，
  // 二者原本是两套独立日期 state（windowStart vs calMonth），导致用户在桌面选的未来日期丢失、
  // 移动端回到今天。这里让移动甘特跟随桌面滚动窗口的锚点月，保住用户选的日期。
  // 桌面「单日」视图仍用 calMonth(=今天)；它与移动甘特从不同时激活，互不影响。
  const activeCalMonth = isMobile && view === "gantt" ? windowFocusMonth(windowStart) : calMonth;
  // 7 日定价 Drawer（甘特图行 hover 菜单触发）
  const [pricingForRoom, setPricingForRoom] = useState<string | null>(null);

  // ─── 房态管理（既有）状态 ─────────────────────────────────────────────────
  const [editRoom, setEditRoom] = useState<RoomOut | null>(null);
  const [newStatus, setNewStatus] = useState<string>("");
  const [editLocation, setEditLocation] = useState<Record<string, string | undefined>>({});
  const [roomModalOpen, setRoomModalOpen] = useState(false);
  const [editingRoom, setEditingRoom] = useState<RoomOut | null>(null);
  const [expandedRoomId, setExpandedRoomId] = useState<string | null>(null);
  const [pricingDetailOpen, setPricingDetailOpen] = useState(false);
  const [selectedPricing, setSelectedPricing] = useState<{
    roomId: string;
    date: string;
    sourceUrl: string | null;
  } | null>(null);

  // ─── 运营台（订单整合）状态 ───────────────────────────────────────────────
  // 订单详情抽屉：用 selectedOrderId 驱动；PendingStrip 已有完整 order 时直接 setQueryData
  // 预填缓存让点击秒开，Gantt 点击只有 order_id 时由 useQuery 拉取。
  const [selectedOrderId, setSelectedOrderId] = useState<string | null>(null);
  const [paymentOpen, setPaymentOpen] = useState(false);
  const [batchDrawerOpen, setBatchDrawerOpen] = useState(false);
  const [createInit, setCreateInit] = useState<QuickCreateInit | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [assignTarget, setAssignTarget] = useState<OrderOut | null>(null);

  // Phase 2 拖拽排房/换房/改期：state + 撤销快照 + 三个 mutation 收敛到 useDragReschedule
  const {
    contextHolder,
    draggingOrderId,
    startDrag, clearDrag, canDropOnCell,
    lastDragSnapshot, setLastDragSnapshot,
    assignRoomMutation,
    dragRescheduleMutation,
    swapMutation,
  } = useDragReschedule();

  // ─── issue#5 锁房：状态 + 创建/解除 mutation 收敛到 useRoomBlocking hook ─────
  // 整包传给 RoomBlockModal；page 只直接用「打开锁房表单 / 解除锁房」这几项
  const blocking = useRoomBlocking();
  const { setBlockingRoom, setBlockType, releaseBlockMutation } = blocking;

  const [roomForm] = Form.useForm();

  // ─── 既有数据查询 ────────────────────────────────────────────────────────
  // 房态板准实时刷新（方案 A，王总 2026-07-14）：让保洁在飞书点「打扫完了」后，
  // 前台甘特图几秒内自动变色，不用手动刷、也不用切回标签页。
  //
  // 分两档，按「变化频率 × 查询开销」平衡：
  // - STATUS_REFRESH（房态徽章 + 今日「保洁中」金色格，读 GET /rooms，轻量）：5 秒快刷。
  //   保洁完工只改房态不改订单条，所以这一档才是「点了没反应」的正解。
  // - CALENDAR_REFRESH（甘特订单条，读较重的日历矩阵接口）：30 秒适中，别猛捶重接口；
  //   新建/拖拽改期本就即时 invalidate，跨设备 30 秒够用。
  // 两档都开 refetchIntervalInBackground：前台常把房态板放在屏上、人却在飞书操作，
  // 标签页不在最前时也要能自动刷（配合 refetchOnWindowFocus 覆盖「切回标签页」）。
  const STATUS_REFRESH = {
    refetchInterval: 5_000,
    refetchIntervalInBackground: true,
    refetchOnWindowFocus: true,
  } as const;
  const CALENDAR_REFRESH = {
    refetchInterval: 30_000,
    refetchIntervalInBackground: true,
    refetchOnWindowFocus: true,
  } as const;

  const { data: rooms, isLoading } = useQuery({
    queryKey: ["rooms"],
    queryFn: () => roomsApi.list().then((r) => r.data),
    ...STATUS_REFRESH,
  });

  // 状态弹窗里展示「当前锁房记录」并可逐条解除——打开弹窗时按房间拉取
  const { data: editRoomBlocks } = useQuery({
    queryKey: ["room-blocks", editRoom?.room_id],
    queryFn: () => roomBlocksApi.list({ room_id: editRoom!.room_id }).then((r) => r.data),
    enabled: !!editRoom,
  });

  // 业主下拉用；只在编辑弹窗用到，staleTime 长一点避免反复请求
  const { data: owners } = useQuery<OwnerOut[]>({
    queryKey: ["owners"],
    queryFn: () => ownersApi.list().then((r) => r.data),
    staleTime: 5 * 60 * 1000,
  });
  const ownerOptions = useMemo(
    () =>
      (owners ?? []).map((o) => ({
        value: o.owner_id,
        label: o.phone ? `${o.name} · ${o.phone}` : o.name,
      })),
    [owners]
  );

  const { data: rawPricing } = useQuery<RoomPricingPoint[]>({
    queryKey: ["room-pricing", expandedRoomId],
    queryFn: () => roomsApi.pricing(expandedRoomId as string, 7).then((r) => r.data),
    enabled: !!expandedRoomId,
    staleTime: 5 * 60 * 1000,
  });

  const normalizedPricing: PricingDay[] | undefined = useMemo(
    () =>
      rawPricing?.map((p) => {
        const toNumber = (v: number | string | null | undefined) => {
          if (v === null || v === undefined) return undefined;
          const n = typeof v === "number" ? v : Number(v);
          return Number.isFinite(n) ? n : undefined;
        };
        const price = toNumber(p.recommended_price) ?? toNumber(p.base_price) ?? toNumber(p.competitor_avg_price) ?? null;
        return { date: p.date, price, source: p.source ?? "", source_url: p.source_url ?? null };
      }),
    [rawPricing]
  );

  const { data: pricingDetail } = useQuery<PricingDetail>({
    queryKey: ["room-pricing-detail", selectedPricing?.roomId, selectedPricing?.date],
    queryFn: () => roomsApi.pricingDetail(selectedPricing!.roomId, selectedPricing!.date).then((r) => r.data),
    enabled: !!selectedPricing,
    staleTime: 5 * 60 * 1000,
  });

  // 自然月日历：移动端甘特 + 单日视图用（这俩仍按月）
  const { data: calendar, isLoading: calLoading } = useQuery<CalendarRoom[]>({
    queryKey: ["rooms", "calendar", activeCalMonth.year, activeCalMonth.month],
    queryFn: () => roomsApi.calendar(activeCalMonth.year, activeCalMonth.month).then((r) => r.data),
    // 桌面甘特已改用滚动窗口(calendarWindow)，这里只服务移动端甘特 + 单日视图。
    enabled: (isMobile && view === "gantt") || view === "today",
    placeholderData: keepPreviousData,
    ...CALENDAR_REFRESH,
  });

  // D1 滚动窗口日历：桌面甘特专用，起始日 + 天数，天然跨月。
  const { data: calendarWindow, isLoading: calWindowLoading } = useQuery<CalendarRoom[]>({
    queryKey: ["rooms", "calendar-window", windowStart, rangeDays],
    queryFn: () => roomsApi.calendarWindow(windowStart, rangeDays).then((r) => r.data),
    enabled: !isMobile && view === "gantt",
    placeholderData: keepPreviousData,
    ...CALENDAR_REFRESH,
  });

  // 7 日定价查询已随 RoomPricingDrawer 下沉到该组件内部

  // ─── 运营台数据查询 ────────────────────────────────────────────────────
  const { data: pendingRoomOrders, isLoading: pendingLoading } = useQuery<OrderOut[]>({
    queryKey: ["orders", "pending-room"],
    queryFn: () => ordersApi.pendingRoom().then((r) => r.data),
    staleTime: 30 * 1000,
    // 新单进来待排房条要自己冒出来，不该让前台手刷页面。与 CALENDAR_REFRESH 同档。
    ...CALENDAR_REFRESH,
  });

  // 选中订单详情 — selectedOrderId 即 enable 拉取
  const { data: selectedOrder } = useQuery<OrderOut>({
    queryKey: ["order", selectedOrderId],
    queryFn: () => ordersApi.get(selectedOrderId!).then((r) => r.data),
    enabled: !!selectedOrderId,
    staleTime: 10 * 1000,
  });

  const { data: selectedPayments } = useQuery<PaymentOut[]>({
    queryKey: ["payments", selectedOrderId],
    queryFn: () => financeApi.payments.list(selectedOrderId!).then((r) => r.data),
    enabled: !!selectedOrderId,
    staleTime: 5 * 60 * 1000,
  });

  // ─── 订单 mutations（内联以避免触发列表请求） ─────────────────────────────
  // 不直接复用 useOrders 因为它会自动拉一个 ordersQuery —— 运营台默认无需。
  // 三个 mutation 与 OrderDetailModal 约定的 props 形态保持一致。
  const transitionMutation = useMutation({
    mutationFn: ({ id, status }: { id: string; status: OrderStatus }) =>
      ordersApi.transition(id, status),
    onSuccess: () => {
      message.success("订单状态已更新");
      invalidateOrderRelated(qc);
    },
  });

  const cancelMutation = useMutation({
    mutationFn: (id: string) => ordersApi.cancel(id),
    onSuccess: () => {
      message.success("订单已取消");
      invalidateOrderRelated(qc);
    },
  });

  const createPaymentMutation = useMutation({
    mutationFn: (data: PaymentCreate) => financeApi.payments.create(data),
    onSuccess: () => {
      message.success("收款已记录");
      invalidatePaymentRelated(qc);
      setPaymentOpen(false);
    },
  });

  // Phase 2: 拖拽排房 mutation（甘特图单元格 drop 触发）
  // ─── 既有 mutations ──────────────────────────────────────────────────────
  const updateMutation = useMutation({
    mutationFn: (payload: { id: string; data: Record<string, unknown> }) =>
      roomsApi.update(payload.id, payload.data),
    onSuccess: () => {
      message.success("房间状态已更新");
      qc.invalidateQueries({ queryKey: ["rooms"] });
      setEditRoom(null);
    },
    onError: () => message.error("更新失败"),
  });

  const saveRoomMutation = useMutation({
    mutationFn: (values: Record<string, unknown>) => {
      if (editingRoom) return roomsApi.update(editingRoom.room_id, values as never);
      return roomsApi.create(values as never);
    },
    onSuccess: () => {
      message.success("房间信息已保存");
      qc.invalidateQueries({ queryKey: ["rooms"] });
      setRoomModalOpen(false);
      setEditingRoom(null);
      roomForm.resetFields();
    },
    onError: (e: unknown) => {
      const err = e as { response?: { data?: { detail?: string } } };
      message.error(extractErrorMessage(err, "保存失败，请稍后重试"));
    },
  });

  const deleteRoomMutation = useMutation({
    mutationFn: (room_id: string) => roomsApi.delete(room_id),
    onSuccess: () => {
      message.success("房间已下线，历史订单与账目已保留");
      qc.invalidateQueries({ queryKey: ["rooms"] });
    },
    onError: (e: unknown) => {
      const err = e as { response?: { data?: { detail?: string } } };
      message.error(extractErrorMessage(err, "下线失败，请稍后重试"));
    },
  });

  const confirmDeleteRoom = (room: { room_id: string; room_name: string }) => {
    Modal.confirm({
      title: `下线房间「${room.room_name}」？`,
      content:
        "下线后，前台的房态列表、排房、甘特图不再显示这间房。它的历史订单、收款、退款、业主分账会完整保留，随时可查、可溯源。业主收回或不再托管的房间用「下线」。",
      okText: "确认下线",
      okButtonProps: { danger: true },
      cancelText: "取消",
      onOk: () => deleteRoomMutation.mutate(room.room_id),
    });
  };

  const prevMonth = () =>
    setCalMonth((p) => {
      const d = new Date(p.year, p.month - 2, 1);
      return { year: d.getFullYear(), month: d.getMonth() + 1 };
    });
  const nextMonth = () =>
    setCalMonth((p) => {
      const d = new Date(p.year, p.month, 1);
      return { year: d.getFullYear(), month: d.getMonth() + 1 };
    });

  // 页头计数用当日 effective_status,与房卡(RoomCard 用 effective_status ?? room_status)
  // 口径一致;否则同屏的"空置/在住"数字会和卡片对不上 (#49)。
  const statusCounts = Object.keys(ROOM_STATUS).reduce((acc, k) => {
    acc[k] = rooms?.filter((r) => (r.effective_status ?? r.room_status) === k).length ?? 0;
    return acc;
  }, {} as Record<string, number>);
  // 页头明细：列出所有非零状态桶，加总 = 总房间数（不再只显示空置/在住，避免"少 5 间"错觉）。
  const statusSummary = formatRoomStatusSummary(statusCounts);

  // ─── 房间分组（按 room_type） ────────────────────────────────────────────
  // calendar 接口不返回 room_type，需要从 rooms 列表里查；rooms 没拉到时 fallback 到扁平。
  const roomTypeById = useMemo(() => {
    const m: Record<string, string | null | undefined> = {};
    (rooms ?? []).forEach((r) => {
      m[r.room_id] = r.room_type;
    });
    return m;
  }, [rooms]);

  const roomNamesById = useMemo(() => {
    const m: Record<string, string> = {};
    (rooms ?? []).forEach((r) => {
      m[r.room_id] = r.room_name;
    });
    return m;
  }, [rooms]);

  // 甘特图行内状态标签用：当前状态 + 上一个状态（一键恢复用）
  const roomStatusById = useMemo(() => {
    const m: Record<string, string> = {};
    (rooms ?? []).forEach((r) => {
      m[r.room_id] = r.room_status;
    });
    return m;
  }, [rooms]);

  // 徽章显示用的有效状态：跟着订单实时算（在住/已预订/空置），
  // 与页头计数、房卡口径一致，避免甘特图行头读静态 room_status 造成的漂移。
  const effectiveStatusById = useMemo(() => {
    const m: Record<string, string> = {};
    (rooms ?? []).forEach((r) => {
      m[r.room_id] = r.effective_status ?? r.room_status;
    });
    return m;
  }, [rooms]);

  const roomPrevStatusById = useMemo(() => {
    const m: Record<string, string | null | undefined> = {};
    (rooms ?? []).forEach((r) => {
      m[r.room_id] = r.previous_status;
    });
    return m;
  }, [rooms]);

  // 房间分组下拉提示选项（AutoComplete 用）
  const roomTypeOptions = useMemo(() => {
    const set = new Set<string>();
    (rooms ?? []).forEach((r) => {
      if (r.room_type) set.add(r.room_type);
    });
    return Array.from(set).map((v) => ({ value: v }));
  }, [rooms]);

  // 桌面甘特用滚动窗口数据，其余（移动甘特/单日）用自然月数据
  const ganttCalendar = !isMobile && view === "gantt" ? calendarWindow : calendar;

  // 房型展示顺序统一从 lib/room-types 取，避免 dashboard / 业主端两处重复维护
  const groupedRooms: RoomGroup[] = useMemo(() => {
    if (!ganttCalendar || ganttCalendar.length === 0) return [];
    const grouped = groupByRoomType(
      ganttCalendar,
      (r) => roomTypeById[r.room_id],
    );
    return grouped.map(({ groupName, items }) => ({ groupName, rooms: items }));
  }, [ganttCalendar, roomTypeById]);

  // 分成比例下拉预设：业主拿的百分比（小数）。改这里就改全局。
  // 王总 2026-05-10 反馈：不要"X 分"中文叫法（七三分/六四分容易歧义），直接"业主 X%"
  const SHARE_RATIO_PRESETS: { label: string; value: number }[] = [
    { label: "业主 30%", value: 0.3 },
    { label: "业主 40%", value: 0.4 },
    { label: "业主 50%", value: 0.5 },
    { label: "业主 60%", value: 0.6 },
    { label: "业主 70%", value: 0.7 },
  ];
  // 把后端的 owner_share_ratio 数字解码成"预设值 + 自定义数字"两栏
  const decodeShareRatio = (
    ratio: number | string | null | undefined
  ): { preset: number | "__custom__"; custom: number | undefined } => {
    const n = ratio == null || ratio === "" ? NaN : Number(ratio);
    if (!Number.isFinite(n)) return { preset: "__custom__", custom: undefined };
    const hit = SHARE_RATIO_PRESETS.find((p) => Math.abs(p.value - n) < 0.0005);
    return hit ? { preset: hit.value, custom: undefined } : { preset: "__custom__", custom: n };
  };

  // ─── 甘特图行右侧 hover 菜单回调 ─────────────────────────────────────────
  const openRoomEditById = (roomId: string) => {
    const r = rooms?.find((x) => x.room_id === roomId);
    if (!r) return;
    setEditingRoom(r);
    const { preset, custom } = decodeShareRatio(r.owner_share_ratio);
    roomForm.setFieldsValue({
      room_id: r.room_id,
      room_name: r.room_name,
      room_type: r.room_type ?? undefined,
      floor: r.floor,
      beds: r.beds ?? 1,
      base_price: r.base_price,
      province: r.province,
      city: r.city,
      district: r.district,
      community_name: r.community_name,
      building_no: r.building_no,
      unit_no: r.unit_no,
      // 新增 6 字段
      owner_id: r.owner_id ?? undefined,
      share_ratio_preset: preset,
      share_ratio_custom: custom,
      contract_signed_date: r.contract_signed_date ? dayjs(r.contract_signed_date) : null,
      sale_date: r.sale_date ? dayjs(r.sale_date) : null,
      is_disabled: r.room_status === "locked",
      remarks: r.remarks ?? undefined,
    });
    setRoomModalOpen(true);
  };

  // 「点房号直接锁房」：从房卡/甘特图行一步打开锁房弹窗，复用既有锁房逻辑
  const lockRoomById = (roomId: string, blockType: BlockType) => {
    const r = rooms?.find((x) => x.room_id === roomId);
    if (!r) return;
    setBlockingRoom(r);
    setBlockType(blockType);
  };

  const openStatusEditById = (roomId: string) => {
    const r = rooms?.find((x) => x.room_id === roomId);
    if (!r) return;
    setEditRoom(r);
    setNewStatus(r.room_status);
    setEditLocation({
      province: r.province ?? undefined,
      city: r.city ?? undefined,
      district: r.district ?? undefined,
      community_name: r.community_name ?? undefined,
      building_no: r.building_no ?? undefined,
      unit_no: r.unit_no ?? undefined,
    });
  };

  // ─── 运营台交互回调 ─────────────────────────────────────────────────────
  const openOrderDetailFromKnown = (o: OrderOut) => {
    // 已有完整数据，预填缓存让 Drawer 立刻显示
    qc.setQueryData(["order", o.order_id], o);
    setSelectedOrderId(o.order_id);
  };

  const openOrderDetailById = (orderId: string) => {
    setSelectedOrderId(orderId);
  };

  const openCreateAt = (init: QuickCreateInit | null) => {
    setCreateInit(init);
    setCreateOpen(true);
  };

  // IA-2: 移动端 FAB 跳 /rooms?action=new → 自动打开新建订单 Modal
  const searchParams = useSearchParams();
  React.useEffect(() => {
    if (searchParams.get("action") === "new") {
      setCreateOpen(true);
      // 清掉 query param 避免来回切换重新触发
      const url = new URL(window.location.href);
      url.searchParams.delete("action");
      window.history.replaceState({}, "", url.toString());
    }
  }, [searchParams]);

  // 待排房卡片数（顶栏角标）
  const pendingCount = pendingRoomOrders?.length ?? 0;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      {contextHolder}
      <PageHeader
        title="房态管理"
        subtitle={focusActive ? undefined : `共 ${rooms?.length ?? 0} 套房间${statusSummary ? ` · ${statusSummary}` : ""}${SHOW_PENDING_ROOM_STRIP && pendingCount > 0 ? ` · ${pendingCount} 单待排房` : ""}`}
        extra={focusActive ? undefined : (
          <Space wrap>
            {/* 常驻订单快搜（前台反馈：搜索常用，别收起进抽屉）。选中 → 直接开订单详情 */}
            <OrderQuickSearch
              onSelectOrder={openOrderDetailById}
              style={isMobile ? { width: "100%" } : undefined}
            />
            {!isMobile && (
              <Button
                icon={<FilterOutlined />}
                onClick={() => setBatchDrawerOpen(true)}
              >
                筛选 / 批量
              </Button>
            )}
            {!isMobile && (
              <Button
                icon={<ThunderboltOutlined />}
                onClick={async () => {
                  try {
                    await Promise.all((rooms || []).map((r) => roomsApi.triggerPricing(r.room_id, 30)));
                    message.success("已为所有房间触发定价计算");
                    qc.invalidateQueries({ queryKey: ["rooms"] });
                  } catch {
                    message.error("触发定价失败");
                  }
                }}
              >
                刷新定价
              </Button>
            )}
            <Button
              onClick={() => {
                setEditingRoom(null);
                roomForm.resetFields();
                setRoomModalOpen(true);
              }}
            >
              新建房间
            </Button>
            <Button
              type="primary"
              icon={<PlusOutlined />}
              onClick={() => openCreateAt(null)}
            >
              新建订单
            </Button>
          </Space>
        )}
      />

      {/* 待排房订单池 — 0 条时组件自动隐藏 */}
      {/* 桌面端开启拖拽到甘特图；移动端走点击「排房」弹 Modal（拖拽体验差） */}
      {/* 依托 bypms 排房期间隐藏该入口，开关见文件顶部 SHOW_PENDING_ROOM_STRIP */}
      {SHOW_PENDING_ROOM_STRIP && (
      <PendingRoomStrip
        orders={pendingRoomOrders}
        isLoading={pendingLoading}
        onAssign={(o) => setAssignTarget(o)}
        onDetail={(o) => openOrderDetailFromKnown(o)}
        draggable={!isMobile && view === "gantt"}
        collapsible={focusActive}
        onDragStart={(o, e) => {
          startDrag(o.order_id, "pending");
          e.dataTransfer.setData("text/source-type", "pending");
        }}
        onDragEnd={clearDrag}
      />
      )}

      {/* 移动端视图切换：甘特(默认) / 房卡 */}
      {isMobile && (
        <Segmented
          block
          value={view === "grid" ? "grid" : "gantt"}
          onChange={(v) => setView(v as View)}
          options={[
            { label: "甘特房态", value: "gantt", icon: <TableOutlined /> },
            { label: "房间卡片", value: "grid", icon: <AppstoreOutlined /> },
          ]}
        />
      )}

      {!isMobile && !focusActive && (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 12,
            flexWrap: "wrap",
          }}
        >
          <Segmented
            value={view}
            onChange={(v) => setView(v as View)}
            options={[
              { label: "日历", value: "gantt", icon: <TableOutlined /> },
              { label: "单日", value: "today", icon: <CalendarOutlined /> },
              { label: "房间卡片", value: "grid", icon: <AppstoreOutlined /> },
            ]}
          />
          {rooms && (
            <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
              {Object.entries(ROOM_STATUS).map(([k, v]) =>
                statusCounts[k] > 0 ? (
                  <span
                    key={k}
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      gap: 6,
                      fontSize: 12,
                      color: tokens.color.text.secondary,
                    }}
                  >
                    <StatusBadge status={k} label={v.label} size="sm" />
                    <span className="tabular" style={{ color: tokens.color.text.primary, fontWeight: 500 }}>
                      {statusCounts[k]}
                    </span>
                  </span>
                ) : null
              )}
            </div>
          )}
        </div>
      )}

      {/* 视图分支：移动端默认甘特(MobileGantt)，view==="grid" 时走房卡；桌面按 view 切换 */}
      {isMobile && view !== "grid" ? (
        <Skeleton loading={calLoading} active>
          <MobileGantt
            rooms={calendar ?? []}
            calMonth={activeCalMonth}
            roomTypeById={roomTypeById}
            effectiveStatusById={effectiveStatusById}
            onOrderClick={(orderId) => openOrderDetailById(orderId)}
            onCellClick={(roomId, date) => {
              // 空格 → 新建订单 Modal，预填房间 + 入住日期，退房默认 +1 天
              const next = new Date(date);
              next.setDate(next.getDate() + 1);
              const y = next.getFullYear();
              const m = String(next.getMonth() + 1).padStart(2, "0");
              const d = String(next.getDate()).padStart(2, "0");
              openCreateAt({ room_id: roomId, check_in_date: date, check_out_date: `${y}-${m}-${d}` });
            }}
          />
        </Skeleton>
      ) : view === "grid" ? (
        <Skeleton loading={isLoading} active>
          <Row gutter={[12, 12]}>
            {rooms?.map((room) => (
              <RoomCard
                key={room.room_id}
                room={room}
                expanded={expandedRoomId === room.room_id}
                pricingDays={expandedRoomId === room.room_id ? normalizedPricing : undefined}
                onOpenPricingDetail={(date, sourceUrl) => {
                  setSelectedPricing({ roomId: room.room_id, date, sourceUrl });
                  setPricingDetailOpen(true);
                }}
                onToggleExpand={() => setExpandedRoomId(expandedRoomId === room.room_id ? null : room.room_id)}
                onOpenStatusModal={() => openStatusEditById(room.room_id)}
                onLock={(bt) => lockRoomById(room.room_id, bt)}
                onDelete={() => confirmDeleteRoom(room)}
                onOpenEditInfo={() => openRoomEditById(room.room_id)}
              />
            ))}
          </Row>
        </Skeleton>
      ) : view === "today" ? (
        <TodayRoomList
          calendar={calendar}
          isLoading={calLoading}
          effectiveStatusById={effectiveStatusById}
          onOrderClick={(orderId) => openOrderDetailById(orderId)}
          onCreateOrder={(roomId, date) => {
            const next = new Date(date);
            next.setDate(next.getDate() + 1);
            const y = next.getFullYear();
            const m = String(next.getMonth() + 1).padStart(2, "0");
            const d = String(next.getDate()).padStart(2, "0");
            openCreateAt({ room_id: roomId, check_in_date: date, check_out_date: `${y}-${m}-${d}` });
          }}
        />
      ) : (
        <GanttView
          calendar={calendarWindow}
          isLoading={calWindowLoading}
          windowStart={windowStart}
          rangeDays={rangeDays}
          onWindowStartChange={setWindowStart}
          onRangeDaysChange={setRangeDays}
          onCreateOrder={() => openCreateAt(null)}
          onOpenBatch={() => setBatchDrawerOpen(true)}
          groupedRooms={groupedRooms.length > 0 ? groupedRooms : undefined}
          roomNamesById={roomNamesById}
          roomStatusById={roomStatusById}
          effectiveStatusById={effectiveStatusById}
          roomPrevStatusById={roomPrevStatusById}
          onQuickChangeRoomStatus={(roomId, status) =>
            updateMutation.mutate({ id: roomId, data: { room_status: status } })
          }
          onReleaseBlock={(blockId) => releaseBlockMutation.mutate(blockId)}
          focusMode={focusActive}
          onToggleFocus={toggleFocus}
          onEditRoom={(roomId) => openRoomEditById(roomId)}
          onChangeRoomStatus={(roomId) => openStatusEditById(roomId)}
          onLockRoom={(roomId, bt) => lockRoomById(roomId, bt)}
          onShowPricing={(roomId) => setPricingForRoom(roomId)}
          onOrderClick={(orderId) => openOrderDetailById(orderId)}
          onCellClick={(roomId, date) => {
            // 空白单元格 → 新建订单 Modal，预填房间号 + 入住日期
            // 退房日期默认 +1 天，由用户在表单里调整
            const next = new Date(date);
            next.setDate(next.getDate() + 1);
            const y = next.getFullYear();
            const m = String(next.getMonth() + 1).padStart(2, "0");
            const d = String(next.getDate()).padStart(2, "0");
            openCreateAt({ room_id: roomId, check_in_date: date, check_out_date: `${y}-${m}-${d}` });
          }}
          onCellDragOver={(_roomId, _date, day, e) => {
            // 原生 dragover 可能早于 React state 重渲染，必须读同步拖拽会话；
            // 否则首个 dragover 不 preventDefault，浏览器会直接吞掉后续 drop。
            if (!canDropOnCell(Boolean(day))) return;
            e.preventDefault();
            e.dataTransfer.dropEffect = "move";
          }}
          onCellDrop={(roomId, date, day, e) => {
            e.preventDefault();
            const orderId = e.dataTransfer.getData("text/order-id");
            const sourceType = e.dataTransfer.getData("text/source-type");
            if (!orderId) {
              clearDrag();
              return;
            }
            // 分支 1：待排房卡片 → 空格（现有逻辑）
            if (sourceType === "pending") {
              if (day) {
                message.warning("该位置已有订单，请放到空格");
              } else {
                assignRoomMutation.mutate({ orderId, roomId });
              }
              clearDrag();
              return;
            }
            // 分支 2 / 3：gantt 已排房订单
            if (sourceType === "gantt") {
              const sourceRoomId = e.dataTransfer.getData("text/source-room-id");
              const sourceDate = e.dataTransfer.getData("text/source-date");
              const sourceOrderRoomId = e.dataTransfer.getData("text/source-order-room-id");
              // 同位置不动
              if (sourceRoomId === roomId && sourceDate === date) {
                clearDrag();
                return;
              }
              // 目标占用 → 尝试对调两个订单的房间
              if (day) {
                const targetOrderId = day.order_id;
                // 拖到自己（多房订单的另一行/同单）不处理
                if (!targetOrderId || targetOrderId === orderId) {
                  clearDrag();
                  return;
                }
                // 拉两单详情：A 定位被拖行；B 定位目标房+目标日期所在行；并判状态
                Promise.all([ordersApi.get(orderId), ordersApi.get(targetOrderId)])
                  .then(([ra, rb]) => {
                    const oa = ra.data;
                    const ob = rb.data;
                    const checkedIn = (s: string) => s === "checked_in";
                    if (checkedIn(oa.order_status) && checkedIn(ob.order_status)) {
                      Modal.warning({
                        title: "无法对调",
                        content: "两个订单都在住，不支持对调房间。",
                      });
                      clearDrag();
                      return;
                    }
                    const aRooms: OrderRoomOut[] = Array.isArray(oa.rooms) ? oa.rooms : [];
                    const bRooms: OrderRoomOut[] = Array.isArray(ob.rooms) ? ob.rooms : [];
                    const aRow = sourceOrderRoomId
                      ? aRooms.find((rr) => rr.order_room_id === sourceOrderRoomId) || aRooms[0]
                      : aRooms[0];
                    // B 行：命中目标房间、且目标日期落在该行入住区间
                    // 日期均为 "YYYY-MM-DD" 串，可直接字典序比较判区间（勿改成 new Date 以免引入 UTC 偏移）
                    const bRow =
                      bRooms.find(
                        (rr) =>
                          rr.room_id === roomId &&
                          rr.check_in_date <= date &&
                          date < rr.check_out_date,
                      ) || bRooms.find((rr) => rr.room_id === roomId) || bRooms[0];
                    if (!aRow?.order_room_id || !bRow?.order_room_id) {
                      message.error("无法定位待对调的房间行");
                      clearDrag();
                      return;
                    }
                    const aRoomName = aRow.room_id;
                    const bRoomName = bRow.room_id;
                    const priceNote =
                      Number(
                        (rooms ?? []).find((r) => r.room_id === aRoomName)?.base_price ?? 0,
                      ) !==
                      Number(
                        (rooms ?? []).find((r) => r.room_id === bRoomName)?.base_price ?? 0,
                      )
                        ? "（两房挂牌价不同，对调不改订单金额，如需改价请到订单详情）"
                        : "";
                    // 在住方对调会重置其门锁密码（旧码失效、新房自动下新码），提醒前台转发新码
                    const lockNote =
                      checkedIn(oa.order_status) || checkedIn(ob.order_status)
                        ? "\n\n🔁 在住方换房会重置其门锁密码（旧码失效、新房自动下发新码），请到密码群查看并转发给客人。"
                        : "";
                    // 对调已完成的单且两房不是同一房东 → 收入归属会变，提醒重算分账
                    const aOwner = (rooms ?? []).find((r) => r.room_id === aRoomName)?.owner_id ?? null;
                    const bOwner = (rooms ?? []).find((r) => r.room_id === bRoomName)?.owner_id ?? null;
                    const anyCompleted =
                      oa.order_status === "completed" || ob.order_status === "completed";
                    const ownerNote =
                      anyCompleted && aOwner && bOwner && aOwner !== bOwner
                        ? "\n\n⚠️ 这两间房不是同一个房东，对调已完成的订单会改变这段收入的归属。若本月房东分账已经生成，请记得重新生成一次。"
                        : "";
                    Modal.confirm({
                      title: "对调房间",
                      content: `把【${oa.guest_name} · 房间${aRoomName}】与【${ob.guest_name} · 房间${bRoomName}】对调？A → 房间${bRoomName}、B → 房间${aRoomName}，各自日期不变。${priceNote}${lockNote}${ownerNote}`,
                      okText: "确认对调",
                      cancelText: "取消",
                      onOk: () =>
                        swapMutation.mutate({
                          order_a_id: orderId,
                          order_room_a_id: aRow.order_room_id,
                          order_b_id: targetOrderId,
                          order_room_b_id: bRow.order_room_id,
                        }),
                    });
                    clearDrag();
                  })
                  .catch((e: unknown) => {
                    const err = e as { response?: { data?: { detail?: string } } };
                    message.error(extractErrorMessage(err, "读取订单失败"));
                    clearDrag();
                  });
                return;
              }
              // 拉订单详情拿 check_in/out 计算偏移；多房订单要找到被拖拽的那一行 OrderRoom
              ordersApi
                .get(orderId)
                .then((r) => r.data)
                .then(async (order) => {
                  // 续住组拖拽拆组提示：拖拽负载是单段，拖动组内某段只动那一段 → 可能拆断
                  // 连续入住。只提示不挡。段数从 stay-group 接口拿（该接口自带 .then(r=>r.data)
                  // 拆包，返回的就是数据——别照 ordersApi.get 的样子再 .data 一层）。
                  // 拉不到段数也照样提示（isStayGroup 仍为 true），只是不写段数。
                  let staySegmentCount: number | null = null;
                  if (order.stay_group_id) {
                    try {
                      const g = await ordersApi.stayGroup(orderId);
                      staySegmentCount = g?.segments?.length ?? null;
                    } catch {
                      staySegmentCount = null;
                    }
                  }
                  // 已完成的单拖到空房 = 纠正历史房号（后端 update 路径已放开终态编辑，
                  // 2026-05-24 王总诉求「店长能补录/纠错」）。跨房东会改分账归属，下方
                  // planDragConfirm 会据 isCompleted + 房东 id 弹「重算分账」提醒。
                  // Multi-room: 优先按 source order_room_id 定位被拖拽的房行；
                  // fallback 到首行（兼容老数据）
                  const orderRoomsArr: any[] = Array.isArray(order.rooms) && order.rooms.length > 0
                    ? order.rooms
                    : [{
                        order_room_id: undefined,
                        room_id: order.room_id,
                        check_in_date: order.check_in_date,
                        check_out_date: order.check_out_date,
                        list_price: order.list_price,
                        actual_price: order.actual_price,
                        guests_count: 0,
                        position: 0,
                      }];
                  const draggedRow = sourceOrderRoomId
                    ? orderRoomsArr.find((rr) => rr.order_room_id === sourceOrderRoomId) || orderRoomsArr[0]
                    : orderRoomsArr[0];

                  const before = {
                    room_id: draggedRow.room_id ?? null,
                    check_in_date: draggedRow.check_in_date,
                    check_out_date: draggedRow.check_out_date,
                  };
                  // 计算位移：drop date - source cell date
                  const offsetDays =
                    Math.round(
                      (new Date(date).getTime() - new Date(sourceDate).getTime()) /
                        (1000 * 60 * 60 * 24)
                    ) || 0;
                  const newCheckIn = dayjs(draggedRow.check_in_date).add(offsetDays, "day").format("YYYY-MM-DD");
                  const newCheckOut = dayjs(draggedRow.check_out_date).add(offsetDays, "day").format("YYYY-MM-DD");
                  const roomChanged = roomId !== draggedRow.room_id;
                  const datesChanged = offsetDays !== 0;
                  if (!roomChanged && !datesChanged) {
                    clearDrag();
                    return;
                  }

                  // 构造 rooms[] 整体替换 payload（其它房保持不变）
                  const newRooms = orderRoomsArr.map((rr) => ({
                    room_id: rr === draggedRow ? roomId : (rr.room_id ?? null),
                    check_in_date: rr === draggedRow ? newCheckIn : rr.check_in_date,
                    check_out_date: rr === draggedRow ? newCheckOut : rr.check_out_date,
                    list_price: rr.list_price ?? null,
                    actual_price: rr.actual_price ?? null,
                    guests_count: rr.guests_count ?? 0,
                    position: rr.position ?? 0,
                  }));
                  const payload: any = { rooms: newRooms };

                  // 跨房型价格不同 → confirm（默认决策 G：保留原价）
                  const targetRoom = (rooms ?? []).find((r) => r.room_id === roomId);
                  const sourceRoom = (rooms ?? []).find((r) => r.room_id === draggedRow.room_id);
                  const priceDiffers =
                    targetRoom && sourceRoom &&
                    Number(targetRoom.base_price ?? 0) !== Number(sourceRoom.base_price ?? 0);
                  const doMutate = () => {
                    setLastDragSnapshot({
                      orderId,
                      before,
                      beforeRooms: orderRoomsArr.map((rr) => ({
                        room_id: rr.room_id ?? null,
                        check_in_date: rr.check_in_date,
                        check_out_date: rr.check_out_date,
                        list_price: rr.list_price ?? null,
                        actual_price: rr.actual_price ?? null,
                        guests_count: rr.guests_count ?? 0,
                        position: rr.position ?? 0,
                      })),
                    });
                    dragRescheduleMutation.mutate({ orderId, payload });
                  };
                  // 换房拖拽：日期被改动 or 跨房型价差 → 合并成一个确认弹窗
                  // （客户诉求：换房时日期不能被静默改动，落到别的日期列要提示确认）
                  const plan = planDragConfirm({
                    datesChanged,
                    priceDiffers: Boolean(priceDiffers),
                    before: {
                      check_in_date: draggedRow.check_in_date,
                      check_out_date: draggedRow.check_out_date,
                    },
                    next: { check_in_date: newCheckIn, check_out_date: newCheckOut },
                    sourceRoom: sourceRoom ?? null,
                    targetRoom: targetRoom ?? null,
                    isCheckedIn: order.order_status === "checked_in",
                    // 已完成单跨房东纠错 → 提示重算分账（收入归属已随原房落到某房东名下）
                    isCompleted: order.order_status === "completed",
                    sourceOwnerId: sourceRoom?.owner_id ?? null,
                    targetOwnerId: targetRoom?.owner_id ?? null,
                    // 拖回过去=补录/纠错老单的正当动作，但与手滑同形，故弹窗问人；
                    // 确认后带 allow_past_dates 放行后端守卫（王总 2026-07-17 拍板）
                    movesToPast: dayjs(newCheckIn).isBefore(dayjs().startOf("day")),
                    // 续住组某段被拖 → 弹拆组提示（只提示不挡操作）
                    isStayGroup: Boolean(order.stay_group_id),
                    staySegmentCount,
                  });
                  // 确认即授权：拖到过去必然 datesChanged → 必然经过下面的 Modal.confirm
                  if (plan.allowPastDates) payload.allow_past_dates = true;
                  if (plan.needsConfirm) {
                    Modal.confirm({
                      title: plan.title,
                      icon: plan.danger ? (
                        <ExclamationCircleFilled style={{ color: "#ff4d4f" }} />
                      ) : undefined,
                      okButtonProps: plan.danger ? { danger: true } : undefined,
                      content: (
                        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                          {plan.dateChange ? (
                            <div
                              style={{
                                background: "#fff2f0",
                                border: "1px solid #ffccc7",
                                borderRadius: 6,
                                padding: "8px 12px",
                                lineHeight: 1.9,
                              }}
                            >
                              <div style={{ fontWeight: 600, color: "#cf1322", marginBottom: 2 }}>
                                注意：入住日期将被改动，与实际入住时间不符
                              </div>
                              <div>
                                入住{" "}
                                <span style={{ textDecoration: "line-through", opacity: 0.6 }}>
                                  {plan.dateChange.fromCheckIn}
                                </span>{" "}
                                →{" "}
                                <strong style={{ color: "#cf1322" }}>
                                  {plan.dateChange.toCheckIn}
                                </strong>
                              </div>
                              <div>
                                退房{" "}
                                <span style={{ textDecoration: "line-through", opacity: 0.6 }}>
                                  {plan.dateChange.fromCheckOut}
                                </span>{" "}
                                →{" "}
                                <strong style={{ color: "#cf1322" }}>
                                  {plan.dateChange.toCheckOut}
                                </strong>
                              </div>
                              <div style={{ marginTop: 4, color: "#8c8c8c", fontSize: 12 }}>
                                仅想换房不改日期？请把卡片拖回原来的日期列。
                              </div>
                            </div>
                          ) : null}
                          {plan.priceWarning ? <div>{plan.priceWarning}</div> : null}
                          {plan.lockWarning ? (
                            <div
                              style={{
                                background: "#fffbe6",
                                border: "1px solid #ffe58f",
                                borderRadius: 6,
                                padding: "8px 12px",
                                lineHeight: 1.8,
                              }}
                            >
                              <div style={{ fontWeight: 600, color: "#ad6800", marginBottom: 2 }}>
                                🔁 门锁密码将重置
                              </div>
                              <div>{plan.lockWarning}</div>
                            </div>
                          ) : null}
                          {plan.settlementWarning ? (
                            <div
                              style={{
                                background: "#fffbe6",
                                border: "1px solid #ffe58f",
                                borderRadius: 6,
                                padding: "8px 12px",
                                lineHeight: 1.8,
                              }}
                            >
                              <div style={{ fontWeight: 600, color: "#ad6800", marginBottom: 2 }}>
                                ⚠️ 分账归属将改变
                              </div>
                              <div>{plan.settlementWarning}</div>
                            </div>
                          ) : null}
                          {plan.stayGroupWarning ? (
                            <div
                              style={{
                                background: "#fffbe6",
                                border: "1px solid #ffe58f",
                                borderRadius: 6,
                                padding: "8px 12px",
                                lineHeight: 1.8,
                              }}
                            >
                              <div style={{ fontWeight: 600, color: "#ad6800", marginBottom: 2 }}>
                                ⚠️ 续住组：仅移动该段
                              </div>
                              <div>{plan.stayGroupWarning}</div>
                            </div>
                          ) : null}
                        </div>
                      ),
                      okText: plan.okText,
                      cancelText: plan.cancelText,
                      onOk: doMutate,
                    });
                  } else {
                    doMutate();
                  }
                })
                .catch((e: any) => {
                  message.error(extractErrorMessage(e, "拉取订单失败"));
                });
              clearDrag();
              return;
            }
            // 兜底
            clearDrag();
          }}
          onCellDragStart={(orderId, source, e) => {
            startDrag(orderId, "gantt");
            e.dataTransfer.setData("text/order-id", orderId);
            e.dataTransfer.setData("text/source-type", "gantt");
            e.dataTransfer.setData("text/source-room-id", source.roomId);
            e.dataTransfer.setData("text/source-date", source.date);
            // Multi-room: 拖拽起点的 OrderRoom 行 id（避免多房订单错改其它行）
            if (source.orderRoomId) {
              e.dataTransfer.setData("text/source-order-room-id", source.orderRoomId);
            }
            e.dataTransfer.effectAllowed = "move";
          }}
          onCellDragEnd={clearDrag}
          draggingOrderId={draggingOrderId}
          barColorMode="completion"
        />
      )}

      {/* Pricing Detail Modal */}
      <PricingDetailModal
        open={pricingDetailOpen}
        onClose={() => {
          setPricingDetailOpen(false);
          setSelectedPricing(null);
        }}
        roomId={selectedPricing?.roomId}
        date={selectedPricing?.date}
        detail={pricingDetail}
      />

      {/* Edit Status Modal — 既有功能保留 */}
      <RoomStatusModal
        editRoom={editRoom}
        onClose={() => setEditRoom(null)}
        newStatus={newStatus}
        setNewStatus={setNewStatus}
        editLocation={editLocation}
        setEditLocation={setEditLocation}
        updateMutation={updateMutation}
        editRoomBlocks={editRoomBlocks}
        blocking={blocking}
        isMobile={isMobile}
      />

      {/* Create / Edit Room Modal — 既有功能保留 */}
      <RoomEditModal
        open={roomModalOpen}
        onClose={() => {
          setRoomModalOpen(false);
          setEditingRoom(null);
          roomForm.resetFields();
        }}
        editingRoom={editingRoom}
        roomForm={roomForm}
        saveRoomMutation={saveRoomMutation}
        roomTypeOptions={roomTypeOptions}
        isMobile={isMobile}
      />

      {/* ─── 运营台新增 Modal/Drawer ───────────────────────────────────────── */}

      {/* 订单详情抽屉（OrderDetailModal 内部已是 Drawer） */}
      <OrderDetailModal
        open={!!selectedOrderId && !!selectedOrder}
        order={selectedOrder}
        payments={Array.isArray(selectedPayments) ? selectedPayments : []}
        onClose={() => setSelectedOrderId(null)}
        onOpenPayment={() => {
          setPaymentOpen(true);
        }}
        transitionMutation={transitionMutation}
        cancelMutation={cancelMutation}
      />

      {/* 收款 Modal */}
      <PaymentModal
        open={paymentOpen}
        order={selectedOrder}
        payments={Array.isArray(selectedPayments) ? selectedPayments : []}
        onClose={() => setPaymentOpen(false)}
        onFinish={(values) => createPaymentMutation.mutate(values)}
        loading={createPaymentMutation.isPending}
      />

      {/* 选房 Modal — 待排房卡片「排房」按钮专用 */}
      <AssignRoomModal
        order={assignTarget}
        onClose={() => setAssignTarget(null)}
      />

      {/* 新建订单 Modal — 顶栏「+新建订单」/ 甘特图空白格 */}
      <QuickCreateOrderModal
        open={createOpen}
        initial={createInit}
        onClose={() => {
          setCreateOpen(false);
          setCreateInit(null);
        }}
      />

      {/* 筛选/批量抽屉 — 整页订单管理放在 80vw 抽屉里 */}
      <BatchOrdersDrawer
        open={batchDrawerOpen}
        onClose={() => setBatchDrawerOpen(false)}
      />

      {/* 7 日定价 Drawer — 甘特图行 hover 菜单触发 */}
      <RoomPricingDrawer
        roomId={pricingForRoom}
        onClose={() => setPricingForRoom(null)}
        isMobile={isMobile}
      />

      {/* 锁房 Modal — issue#5 */}
      <RoomBlockModal blocking={blocking} />
    </div>
  );
}
