"use client";

import React, { useEffect, useState } from "react";
import { extractErrorMessage } from "@/lib/api-errors";
import { buildEditDatesPayload } from "@/lib/order-dates";
import { formatExpectedRevenue } from "@/lib/order-display";
import { Drawer, Button, Space, Timeline, Popconfirm, message, Modal, Tag, Dropdown, Tooltip, Tabs, Alert } from "antd";
import {
  CheckCircleOutlined,
  CloseCircleOutlined,
  DollarOutlined,
  DeleteOutlined,
  HomeOutlined,
  EditOutlined,
  MoreOutlined,
  RollbackOutlined,
  LockOutlined,
  LinkOutlined,
  DisconnectOutlined,
  WarningOutlined,
  CopyOutlined,
  LoadingOutlined,
} from "@ant-design/icons";
import { useQueryClient, useQuery, useQueries, useMutation } from "@tanstack/react-query";
import { ordersApi, roomsApi, financeApi } from "@/lib/api";
import { invalidateOrderRelated, invalidateOrderWithAudit, invalidatePaymentWithAudit } from "@/lib/order-cache";
import OrderAuditTimeline from "./OrderAuditTimeline";
import EditOrderModal from "./EditOrderModal";
import PaymentModal from "./PaymentModal";
import { TransferRoomModal } from "./TransferRoomModal";
import { CheckinDepositModal } from "./detail/CheckinDepositModal";
import { RevertCheckoutModal } from "./detail/RevertCheckoutModal";
import { RevertCheckinModal } from "./detail/RevertCheckinModal";
import { ExtendLockModal } from "./detail/ExtendLockModal";
import { LinkStayModal } from "./detail/LinkStayModal";
import { StaySegmentBreakdown } from "./detail/StaySegmentBreakdown";
import { FreeRoomBadge } from "@/components/ui/FreeRoomBadge";
import { segmentActionGates } from "@/lib/stay-group-actions";
import { segmentCountLabel } from "@/lib/stay-group-display";
import { CheckoutModal } from "./detail/CheckoutModal";
import { AssignRoomModal } from "./detail/AssignRoomModal";
import { EditDatesModal, type EditDatesCtx } from "./detail/EditDatesModal";
import { DailyPriceModal, type EditDailyCtx } from "./detail/DailyPriceModal";
import dayjs from "dayjs";
import { useIsMobile } from "@/lib/responsive";
import { tokens } from "@/lib/design-tokens";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { BOOKING_TYPE_LABEL, BOOKING_TYPE_TAG_COLOR, type BookingType, type RoomOut, type StaySegment } from "@/lib/types";
import { useAuthStore } from "@/lib/auth";

import { CHANNEL_LABELS } from "@/lib/channels";
import { computeCheckinFinancialRisks } from "@/lib/checkin-risk";
import { ManualOverridePanel } from "./ManualOverridePanel";
import { SourcePriceOverrideDialog } from "./SourcePriceOverrideDialog";
import { SplitStayDialog } from "./SplitStayDialog";
import { StaySettlementLabel } from "@/components/ui/StaySettlementLabel";

const PAYMENT_METHOD_LABELS: Record<string, string> = {
  wechat: "微信",
  alipay: "支付宝",
  cash: "现金",
  bank_transfer: "转账",
  platform: "平台代收",
  other: "其他",
};

// 2026-06-27 王总流程调整：砍掉「确认收款」中间步，退房后一步直接「完成订单」。
// 确认订单 → 确认排房 → 办理入住(收押金) → 发起退房(退押金) → 完成订单(校验房费收齐)。
// 注：pending_payment（待完成）仍保留为存量在途单的出口，新单不会再进入该态。
const NEXT_STATUS: Record<string, { label: string; target: string }> = {
  pending_confirm: { label: "确认订单", target: "paid_pending_room" },
  paid_pending_room: { label: "确认排房", target: "roomed_pending_checkin" },
  roomed_pending_checkin: { label: "办理入住", target: "checked_in" },
  checked_in: { label: "发起退房", target: "pending_checkout" },
  pending_checkout: { label: "完成订单", target: "completed" },
  pending_payment: { label: "完成订单", target: "completed" },
  abnormal: { label: "标记完成", target: "completed" },
  rescheduled: { label: "重新排房", target: "roomed_pending_checkin" },
};

interface Props {
  open: boolean;
  order: any;
  payments: any[];
  onClose: () => void;
  onOpenPayment: () => void;
  transitionMutation: any;
  cancelMutation: any;
}

// 单条订单 live query — 解决 mutation 后 Drawer 拿 stale 父级快照的问题
// （单日改价 / 修改订单 / 换房 / 收款编辑 后字段不更新）
function useLiveOrder(open: boolean, fallback: any) {
  const orderId = fallback?.order_id;
  const { data } = useQuery({
    queryKey: ["order", orderId],
    queryFn: () => ordersApi.get(orderId).then((r) => r.data),
    enabled: open && !!orderId,
    initialData: fallback,
    staleTime: 0,
  });
  return data ?? fallback;
}

// 续住段：拿到任意一段后问整段。点 7/16 还是 7/17，收敛到同一个视图。
// 无组单也发（后端返回退化的单段组 + 可能的续住候选），前端因此不用写「什么时候该问候选」的判断。
function useStayGroup(open: boolean, order: any) {
  const orderId = order?.order_id;
  const { data } = useQuery({
    queryKey: ["stay-group", orderId],
    queryFn: () => ordersApi.stayGroup(orderId),
    enabled: open && !!orderId,
    staleTime: 0,
  });
  return data;
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
      <span style={{ fontSize: 12, color: tokens.color.text.tertiary }}>{label}</span>
      <span
        style={{
          fontSize: 14,
          color: tokens.color.text.primary,
          fontWeight: 500,
          wordBreak: "break-all",
        }}
      >
        {children}
      </span>
    </div>
  );
}

export default function OrderDetailModal({
  open,
  order: orderProp,
  payments,
  onClose,
  onOpenPayment,
  transitionMutation,
  cancelMutation,
}: Props) {
  const isMobile = useIsMobile();
  const queryClient = useQueryClient();
  // 始终用 live query 数据渲染；父组件传入的 orderProp 仅作为 initialData
  const order = useLiveOrder(open, orderProp);
  // 续住段：整段口径（日期/晚数/房间序列/房费/押金）一律由后端 group_view 算好，前端不重算。
  const stayGroup = useStayGroup(open, order);
  const isGroup = !!stayGroup?.stay_group_id;
  const gates = segmentActionGates(order?.order_id, stayGroup);
  // 分段明细里点某一段 → 编辑那一段（而非当前段）。null = 未选中。
  const [editSegment, setEditSegment] = useState<any>(null);
  const currentStatus = order?.order_status || order?.status;
  // 已入住但「该收的钱没收」风险（房费/押金），给前台醒目提醒。逻辑见 lib/checkin-risk。
  const checkinRisks = computeCheckinFinancialRisks(order);
  // 组单：「已收」必须是组内各段合计。宿主传入的 payments 只拉了被点那一段
  //（真机实证：整段房费 513.99，已收只显示锚单段 211.56，续段 256 不显示 → 前台会重复收钱）。
  // 每段各自拉 ["payments", segId] —— 与 usePaymentsQuery 同键，invalidatePaymentRelated
  // 失效 ["payments"] 前缀时对组内每段都生效（键规则见 lib/__tests__/order-cache.test.ts）。
  const groupSegments: StaySegment[] = isGroup ? stayGroup?.segments ?? [] : [];
  const segmentPaymentQueries = useQueries({
    queries: groupSegments.map((s) => ({
      queryKey: ["payments", s.order_id],
      queryFn: () => financeApi.payments.list(s.order_id).then((r) => r.data),
      enabled: open,
      staleTime: 5 * 60 * 1000,
    })),
  });
  // 组单合并各段收款并按时间排序，每笔带所属段标注（段序号+该段入住日）；非组单用宿主的 prop。
  const paymentList: any[] = isGroup
    ? groupSegments
        .flatMap((s, i) => {
          const data = segmentPaymentQueries[i]?.data;
          const list = Array.isArray(data) ? data : [];
          return list.map((p: any) => ({
            ...p,
            __segLabel: `第${i + 1}段 · ${(s.check_in_date ?? "").slice(5)}`,
          }));
        })
        .sort((a: any, b: any) => String(a.paid_at).localeCompare(String(b.paid_at)))
    : Array.isArray(payments)
      ? payments
      : [];
  const totalPaid = paymentList.reduce((s: number, p: any) => s + Number(p.amount), 0);
  const next = currentStatus ? NEXT_STATUS[currentStatus] : null;
  // cancelled 是真终态；completed 只是"已退房"，店长仍能修改/补录/撤销退房（2026-05-24 王总诉求）
  const isCancelled = currentStatus === "cancelled";
  const isCompleted = currentStatus === "completed";
  const isTerminal = isCancelled || isCompleted;
  // 换房/排房两条后端路径允许集不同，按行（有无 room_id）分别对齐，避免前端放行→后端 400。
  // 换房（已排房行走 /transfer-room）：与后端 TRANSFER_ALLOWED_STATUSES 逐项对齐（含入住中 midstay 拆账）。
  const canTransferRoom = [
    "pending_confirm", "paid_pending_room", "roomed_pending_checkin", "rescheduled", "checked_in",
  ].includes(currentStatus || "");
  // 排房（未排房行走 /assign-room）：与后端 assign-room 允许集对齐（屏蔽入住中/退房后态/终态）。
  const canAssignRoom = !isTerminal && !["checked_in", "pending_checkout", "pending_payment"].includes(currentStatus || "");
  // 2026-06-27 流程调整：取消「完成订单必须房费收齐」卡点（后端同步移除）。
  // 走携程等平台不存在欠款，完成订单不再要求记录收款，按钮始终可点（无禁用/提示）。

  // 单间入住：本单已排房、尚未入住、尚未退房的房行（checked_in_at/checked_out_at 均空）。
  // OTA 一笔订多间被同步成一单多房 → 需按房逐间办理入住（老坑根治）。多于一间时建单/确认后
  // 用 CheckinDepositModal 选房入住；订单已 checked_in 但还剩待入住房时，额外给一个补办入住按钮。
  const waitingCheckinRooms = (Array.isArray(order?.rooms) ? order.rooms : []).filter(
    (r: any) => r?.room_id && !r?.checked_in_at && !r?.checked_out_at,
  );
  const isMultiRoomCheckin = waitingCheckinRooms.length > 1;

  const [assignOpen, setAssignOpen] = useState(false);
  const [transferOpen, setTransferOpen] = useState(false);
  const [transferRowId, setTransferRowId] = useState<string | undefined>(undefined);
  const [pickedOrderRoomId, setPickedOrderRoomId] = useState<string | undefined>(undefined);
  const [checkoutOpen, setCheckoutOpen] = useState(false);
  // 办理入住时的收押金确认弹窗
  const [checkinOpen, setCheckinOpen] = useState(false);
  const [editOrderOpen, setEditOrderOpen] = useState(false);
  const [editingPayment, setEditingPayment] = useState<any | null>(null);
  const [revertOpen, setRevertOpen] = useState(false);
  const [revertCheckinOpen, setRevertCheckinOpen] = useState(false);
  const [extendOpen, setExtendOpen] = useState(false);
  const [linkStayOpen, setLinkStayOpen] = useState(false);
  const [activeTab, setActiveTab] = useState<"detail" | "audit">("detail");
  const [splitOpen, setSplitOpen] = useState(false);
  const [splitPending, setSplitPending] = useState(false);
  const [sourcePriceOpen, setSourcePriceOpen] = useState(false);
  const [autoPreviewKey, setAutoPreviewKey] = useState(0);
  const settlementAnchorRef = React.useRef<HTMLElement>(null);

  // 内联日期编辑（续住 / 缩短住 / 改入住日 都走这里）
  const [editDatesCtx, setEditDatesCtx] = useState<EditDatesCtx | null>(null);

  // 切换订单 / 关闭 Drawer 时把 Tab 复位到详情
  useEffect(() => {
    if (!open) {
      setActiveTab("detail");
      setSplitOpen(false);
      setSplitPending(false);
      setSourcePriceOpen(false);
      setAutoPreviewKey(0);
    }
  }, [open, orderProp?.order_id]);
  // 单日改价交互：点某天 chip 打开 Modal，里面 InputNumber 改完确认
  const [editDailyCtx, setEditDailyCtx] = useState<EditDailyCtx | null>(null);

  const currentUser = useAuthStore((s) => s.user);
  const isAdmin = currentUser?.role === "admin";
  const canEditPayment = currentUser?.role === "admin" || currentUser?.role === "operator";
  const canEditOrder = currentUser?.role === "admin" || currentUser?.role === "operator";

  const updatePaymentMutation = useMutation({
    mutationFn: ({ payment_id, ...payload }: any) =>
      financeApi.payments.update(payment_id, payload),
    onSuccess: () => {
      message.success("收款已更新");
      invalidatePaymentWithAudit(queryClient, order?.order_id);
      setEditingPayment(null);
    },
    onError: (e: any) => message.error(extractErrorMessage(e, "修改失败")),
  });

  const deletePaymentMutation = useMutation({
    mutationFn: (payment_id: string) => financeApi.payments.remove(payment_id),
    onSuccess: () => {
      message.success("收款已删除");
      invalidatePaymentWithAudit(queryClient, order?.order_id);
    },
    onError: (e: any) => message.error(extractErrorMessage(e, "删除失败")),
  });

  const { data: roomList } = useQuery({
    queryKey: ["rooms"],
    queryFn: () => roomsApi.list().then((r) => r.data),
    // issue#6 "业主应付"预览要查 share_ratio_*
    enabled: open,
    staleTime: 30 * 1000,
  });

  const manualControlQuery = useQuery({
    queryKey: ["manual-control", order?.order_id],
    queryFn: () => ordersApi.manualControl(order.order_id),
    enabled:
      open &&
      !!order?.order_id &&
      typeof ordersApi.manualControl === "function",
    staleTime: 0,
    retry: false,
  });
  const manualControl = manualControlQuery.data;
  const manualControlOrderId = manualControl?.source_order_id ?? order?.order_id;

  // 门锁密码兜底：飞书发不出时前台在详情里直接看+复制。只在住/待退房拉（同重发卡口径），
  // 且仅 admin/operator 可见（后端 assert_can_write 已卡，前端也别给别的角色渲染出入口）。
  const canViewLockCode = currentUser?.role === "admin" || currentUser?.role === "operator";
  const showLockCodeSection =
    canViewLockCode && ["checked_in", "pending_checkout"].includes(currentStatus);
  const { data: lockCodesResp } = useQuery({
    queryKey: ["lock-codes", order?.order_id],
    queryFn: () => ordersApi.lockCodes(order.order_id),
    enabled: open && showLockCodeSection && !!order?.order_id,
    staleTime: 0,
    // 入住瞬间下码可能 FAILED（锁一时离线），码要等重试轮（每 5min）自动重推才到 →
    // issuing 期间轻量轮询自愈，码一到即停。空态后端已不写审计，轮询不刷屏审计。
    refetchInterval: (q) =>
      (q.state.data as { codes?: unknown[]; issuing?: boolean } | undefined)?.issuing ? 20000 : false,
  });
  const lockCodes = lockCodesResp?.codes;
  const lockCodesIssuing = lockCodesResp?.issuing === true;

  // 内联日期编辑 mutation：payload 计算在 lib/order-dates.ts（含"续住保留
  // daily_prices / 缩短住不重摊 / 新增日取上一日价"语义，见该文件注释与单测）。
  const editDatesMutation = useMutation({
    mutationFn: async ({ orderRoomId, ci, co }: { orderRoomId: string; ci: string; co: string }) => {
      const payload = buildEditDatesPayload(order?.rooms, orderRoomId, ci, co);
      return ordersApi.update(order.order_id, {
        rooms: payload.rooms,
        actual_price: payload.actual_price as any,
      } as any);
    },
    onSuccess: () => {
      message.success("已更新入住/退房日期");
      invalidateOrderWithAudit(queryClient, order?.order_id);
      setEditDatesCtx(null);
    },
    onError: (e: any) => message.error(extractErrorMessage(e, "更新失败")),
  });

  // 解除续住关联：前台误把一张单关到别的续住组时用（生产 2026-08-06 韩佩羽/高云晴同房被误拼）。
  // 走后端幂等的 unlink-continuation：把本单从组里摘出，组内若只剩一张另一张也一并恢复独立，
  // 门锁共享码有效期后端自动按新组末调整。缓存失效对齐 LinkStayModal 走 invalidateOrderWithAudit
  // （含 ["stay-group"]，否则开着的抽屉整段头部/分段明细/候选提示不刷新，前台以为没解除）。
  const unlinkMutation = useMutation({
    mutationFn: () => ordersApi.unlinkContinuation(order.order_id),
    onSuccess: () => {
      message.success("已解除续住关联：这张单已从续住组摘出，恢复独立单");
      invalidateOrderWithAudit(queryClient, order?.order_id);
    },
    onError: (e: any) => message.error(extractErrorMessage(e, "解除失败")),
  });

  if (!order) return null;

  // issue#6: booking_type Tag 在标题旁显示（普通单不显示 Tag，避免视觉噪音）
  const bookingType: BookingType = (order?.booking_type as BookingType) || "normal";
  // Multi-room: 取该订单所有房间行；若后端尚未返回 rooms（老数据兼容），降级用顶层 room_id 合成单行
  const orderRooms: any[] = Array.isArray(order?.rooms) && order.rooms.length > 0
    ? order.rooms
    : (order?.room_id || order?.check_in_date)
      ? [{
          order_room_id: `__legacy_${order.order_id}`,
          room_id: order.room_id,
          check_in_date: order.check_in_date,
          check_out_date: order.check_out_date,
          nights: order.nights,
          actual_price: order.actual_price,
          list_price: order.list_price,
          guests_count: 0,
          position: 0,
        }]
      : [];
  // 业主应付预览只在单房 trial/owner_self 订单显示（多房跨业主不展示，避免歧义）
  const matchedRoom = (roomList as RoomOut[] | undefined)?.find((r) => r.room_id === order?.room_id);
  const isSingleRoom = orderRooms.length === 1;
  const ownerShareRatio = isSingleRoom
    ? bookingType === "trial"
      ? Number(matchedRoom?.share_ratio_trial ?? 1)
      : bookingType === "owner_self"
      ? Number(matchedRoom?.share_ratio_owner_self ?? 1)
      : null
    : null;
  const ownerAmount =
    ownerShareRatio != null && order?.actual_price != null
      ? Number(order.actual_price) * ownerShareRatio
      : null;

  const managedSegments = stayGroup?.group_kind === "managed_split" ? stayGroup.segments : [];
  const sponsorshipEntries = managedSegments.length > 0
    ? managedSegments.flatMap((segment) =>
        segment.company_sponsorship
          ? [{ sponsorship: segment.company_sponsorship, channel: segment.channel }]
          : [],
      )
    : order.company_sponsorship
      ? [{ sponsorship: order.company_sponsorship, channel: order.channel }]
      : [];
  const companySponsorshipTotal = sponsorshipEntries.reduce(
    (sum, entry) => sum + Number(entry.sponsorship.effective_amount),
    0,
  );
  const showSponsoredSettlement =
    manualControl?.split.visible === true || managedSegments.length > 0;
  const settlementCheckIn = managedSegments.length > 0 ? stayGroup!.check_in_date : order.check_in_date;
  const settlementCheckOut = managedSegments.length > 0 ? stayGroup!.check_out_date : order.check_out_date;
  const settlementNights = managedSegments.length > 0 ? stayGroup!.nights : order.nights;
  const settlementKinds = Array.from(
    new Set(
      [order.stay_settlement_kind, ...managedSegments.map((segment) => segment.stay_settlement_kind)]
        .filter(Boolean),
    ),
  );
  const hasManualOwnership = (manualControl?.locked_fields.length ?? 0) > 0;
  const showManualControl =
    Boolean(manualControl?.is_relevant) &&
    ((manualControl?.locked_fields.length ?? 0) > 0 ||
      (manualControl?.open_conflicts.length ?? 0) > 0);
  const sourcePriceCanBeCorrected =
    manualControl?.can_administer === true &&
    manualControl?.split.visible === true &&
    ["SOURCE_PRICE_SNAPSHOT_MISSING", "SOURCE_PRICE_SNAPSHOT_INVALID"].includes(
      manualControl?.split.blocker_code ?? "",
    );
  const closeSplit = async () => {
    if (splitPending) return;
    setSplitOpen(false);
    await manualControlQuery.refetch();
    requestAnimationFrame(() => settlementAnchorRef.current?.focus());
  };

  const title = isMobile && splitOpen ? (
    <span style={{ fontSize: 16, fontWeight: 500, letterSpacing: 0 }}>拆分住宿段</span>
  ) : (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
        <StatusBadge status={currentStatus} />
        {managedSegments.length === 0 ? (
          <FreeRoomBadge kind={stayGroup?.free_room_kind ?? (order.is_ota_free_room ? "all" : "none")} />
        ) : null}
        {bookingType !== "normal" && (
          <Tag color={BOOKING_TYPE_TAG_COLOR[bookingType]}>
            {BOOKING_TYPE_LABEL[bookingType]}
          </Tag>
        )}
        <span
          style={{
            fontSize: 12,
            color: tokens.color.text.tertiary,
            fontFamily: tokens.font.mono,
          }}
        >
          #{order.order_id}
        </span>
      </div>
      <Tabs
        size="small"
        activeKey={activeTab}
        onChange={(k) => setActiveTab(k as "detail" | "audit")}
        items={[
          { key: "detail", label: "订单详情" },
          { key: "audit", label: "操作日志" },
        ]}
        tabBarStyle={{ marginBottom: 0 }}
      />
    </div>
  );

  return (
    <Drawer
      open={open}
      onClose={() => {
        if (splitOpen) {
          if (splitPending) return;
          setSplitOpen(false);
          return;
        }
        onClose();
      }}
      closable={!splitPending}
      keyboard={!splitPending}
      maskClosable={!splitPending}
      title={title}
      width={isMobile ? "100%" : 560}
      placement="right"
      destroyOnHidden
      styles={{ body: { padding: 0, background: tokens.color.bg.page } }}
      footer={
        activeTab === "audit" || (isMobile && splitOpen) ? null : (
        <div
          style={{
            display: "flex",
            gap: 8,
            rowGap: 8,
            justifyContent: "space-between",
            alignItems: "center",
            flexWrap: "wrap",
          }}
        >
          <Space wrap>
            {(() => {
              // 组单防呆：取消/删除某一段会影响整组（门锁密码挂锚单段、押金/保洁按整段算一次）。
              // 只加前端强确认，不改后端行为——引导先解除关联或找管理员，别让前台随手点掉一段。
              const doDelete = async () => {
                const hide = message.loading("删除中...", 0);
                try {
                  await ordersApi.delete(order.order_id);
                  hide();
                  message.success("订单已删除");
                  // 删除会从房态图(甘特)移除该订单的条；必须失效 ["rooms"] 等，
                  // 否则条留在图上要手动刷新才消失。走统一 helper 收口，别只失效 ["orders"]。
                  invalidateOrderRelated(queryClient);
                  onClose();
                } catch (e: any) {
                  hide();
                  message.error(extractErrorMessage(e, "删除失败"));
                }
              };
              const groupWarnContent = (action: string) => {
                const segN = stayGroup?.segments?.length ?? 0;
                const isAnchor = order?.order_id === stayGroup?.anchor_order_id;
                return (
                  `这张单属于续住组（共 ${segN} 段连续入住）` +
                  (isAnchor ? "，且是锚单段（整组门锁密码挂在它上面）" : "") +
                  `。${action}这一段会影响整组的门锁密码与押金/保洁口径。` +
                  "建议先解除续住关联、或联系管理员处理后再操作。"
                );
              };
              return (
                <>
                  {!isTerminal &&
                    (isGroup ? (
                      <Button
                        danger
                        icon={<CloseCircleOutlined />}
                        loading={cancelMutation.isPending}
                        disabled={cancelMutation.isPending}
                        onClick={() =>
                          Modal.confirm({
                            title: "这是续住组的一段，确认取消？",
                            content: groupWarnContent("取消"),
                            okText: "仍要取消",
                            okButtonProps: { danger: true },
                            cancelText: "再想想",
                            onOk: () => cancelMutation.mutate(order.order_id),
                          })
                        }
                      >
                        取消订单
                      </Button>
                    ) : (
                      <Popconfirm
                        title="确认取消订单?"
                        description="取消后不可恢复"
                        onConfirm={() => cancelMutation.mutate(order.order_id)}
                      >
                        <Button
                          danger
                          icon={<CloseCircleOutlined />}
                          loading={cancelMutation.isPending}
                          disabled={cancelMutation.isPending}
                        >
                          取消订单
                        </Button>
                      </Popconfirm>
                    ))}
                  {isGroup ? (
                    <Button
                      type="text"
                      danger
                      icon={<DeleteOutlined />}
                      onClick={() =>
                        Modal.confirm({
                          title: "这是续住组的一段，确认删除？",
                          content: groupWarnContent("删除"),
                          okText: "仍要删除",
                          okButtonProps: { danger: true },
                          cancelText: "再想想",
                          onOk: doDelete,
                        })
                      }
                    >
                      删除
                    </Button>
                  ) : (
                    <Popconfirm
                      title="删除订单?"
                      description="从列表中隐藏,可联系管理员恢复"
                      onConfirm={doDelete}
                    >
                      <Button type="text" danger icon={<DeleteOutlined />}>
                        删除
                      </Button>
                    </Popconfirm>
                  )}
                </>
              );
            })()}
          </Space>

          <Space wrap>
            {/* 低频次要操作收进「更多」下拉，避免和高频操作/主按钮平铺挤成一排（无主次）。
                每项按当前状态条件显示；一项都没有则不渲染「更多」按钮。 */}
            {(() => {
              const moreItems: NonNullable<React.ComponentProps<typeof Dropdown>["menu"]>["items"] = [];
              if (isCompleted && canEditOrder) {
                moreItems.push({ key: "revert-checkout", icon: <RollbackOutlined />,
                  label: <span title="撤销退房后订单回到「已入住」，关联清扫任务会被取消">撤销退房</span>,
                  onClick: () => setRevertOpen(true) });
              }
              if (currentStatus === "checked_in") {
                moreItems.push({ key: "extend-lock", icon: <LockOutlined />,
                  label: <span title="续住沿用原密码：把现有门锁密码延到新退房日，密码不变、不改订单金额">门锁密码延期</span>,
                  onClick: () => setExtendOpen(true) });
              }
              if (["checked_in", "pending_checkout"].includes(currentStatus)) {
                moreItems.push({ key: "resend-lock-card", icon: <LockOutlined />,
                  label: <span title="把本单门锁密码卡重推到飞书密码群：卡片被刷走/换房后再要一张时用，不改动门锁">重发密码卡</span>,
                  onClick: () => {
                    ordersApi.resendLockCard(order!.order_id)
                      .then(() => message.success("密码卡已重新推送到飞书密码群"))
                      .catch((e) => message.error(extractErrorMessage(e, "重发失败")));
                  } });
              }
              // 与后端 _LINKABLE_STATUSES 对齐（checkout_continuation.py）：可关联的态都要能点，
              // 否则从「携程续住单」那侧（常是 pending_confirm 待确认 / 待入住）点不到「关联续住」，
              // 前台就会遇到「合并不了」（程鹏 1609，2026-07-26）。cancelled/rescheduled/abnormal 不列。
              if ([
                "pending_confirm", "pending_payment", "paid_pending_room",
                "roomed_pending_checkin", "checked_in", "pending_checkout", "completed",
              ].includes(currentStatus)) {
                moreItems.push({ key: "link-stay", icon: <LinkOutlined />,
                  label: <span title="续住是另一张单（如携程续住）时拴成一段连续入住：一个密码/押金保洁只算一次；历史老单也可关联做账">关联续住</span>,
                  onClick: () => setLinkStayOpen(true) });
              }
              // 误关联撤销：这张单已在续住组里才给「解除关联」（前台点错把两张单拴一起时用，
              // 生产 2026-08-06 韩佩羽/高云晴同房被误拼）。走后端幂等 unlink，二次强确认防手滑。
              if (isGroup) {
                moreItems.push({ key: "unlink-stay", icon: <DisconnectOutlined />,
                  label: <span title="把这张单从续住组里摘出、恢复独立单：门锁共享码有效期后端会自动调整；各段账目本就独立">解除续住关联</span>,
                  onClick: () => {
                    const segN = stayGroup?.segments?.length ?? 0;
                    Modal.confirm({
                      title: "确认解除这张单的续住关联？",
                      content:
                        `这张单当前在续住组里（共 ${segN} 段）。解除后它会从组里摘出、恢复成独立单` +
                        "（组内若只剩一张，另一张也一并恢复独立）。门锁共享码有效期会自动调整；各段账目本就独立，不受影响。",
                      okText: "确认解除",
                      cancelText: "再想想",
                      onOk: () => unlinkMutation.mutateAsync(),
                    });
                  } });
              }
              if (currentStatus === "checked_in" && canEditOrder) {
                moreItems.push({ key: "revert-checkin", icon: <RollbackOutlined />,
                  label: <span title="误点入住时撤回：订单回到「待入住」，房态占用→预留（门锁/押金不动）">撤销入住</span>,
                  onClick: () => setRevertCheckinOpen(true) });
              }
              // 「修改订单」是前台高频操作，2026-07-19 前台反馈别埋进「更多」里，
              // 已提到主操作区（紧跟「确认订单」）单独成按钮，见下方。
              return moreItems.length > 0 ? (
                <Dropdown menu={{ items: moreItems }} trigger={["click"]}>
                  <Button icon={<MoreOutlined />}>更多</Button>
                </Dropdown>
              ) : null;
            })()}
            {/* 组单不给整单级「记录收款」：每段房费/佣金各自独立，钱记错段直接坑 OTA 对账
                （与列表隐藏「记收款」快捷项同口径，王总 2026-07-17 拍板）→ 走分段明细点具体段。 */}
            {!isCancelled && !isGroup && (
              <Button icon={<DollarOutlined />} onClick={onOpenPayment}>
                记录收款
              </Button>
            )}
            {/* 部分入住补办：订单已「在住」但还剩待入住的房（多房单逐间入住的中间态）→
                给一个次要按钮把剩余房间办理入住，走同一个选房弹窗。 */}
            {currentStatus === "checked_in" && waitingCheckinRooms.length > 0 && (
              <Button
                icon={<CheckCircleOutlined />}
                onClick={() => setCheckinOpen(true)}
              >
                办理入住（还剩 {waitingCheckinRooms.length} 间）
              </Button>
            )}
            {/* 续住组中间段不给「发起退房」：客人明天还住，后端本来就会 400（中间段退房拦截）。
                这里只是不让前台点到那堵墙。末段（gates.canCheckout）照常显示。 */}
            {next && !isTerminal && !(currentStatus === "checked_in" && !gates.canCheckout) && (() => {
              const button = (
                <Button
                  type="primary"
                  icon={<CheckCircleOutlined />}
                  loading={transitionMutation.isPending}
                  onClick={() => {
                    if (currentStatus === "checked_in") {
                      // 「发起退房」走 staff_portal.handle_checkout：选保洁 + 退押金 → 创建带 assignee 的清扫任务 + 房态推到 pending_clean。
                      setCheckoutOpen(true);
                    } else if (currentStatus === "roomed_pending_checkin") {
                      // 「办理入住」：多房单须走弹窗选房（避免整单一起入住的老坑）；
                      // 或押金未收且 > 0 时先走收押金确认；否则单房无押金直接入住。
                      const dep = Number(order?.deposit ?? 0);
                      if (isMultiRoomCheckin || (dep > 0 && order?.deposit_status === "not_collected")) {
                        setCheckinOpen(true);
                      } else {
                        transitionMutation.mutate({ id: order.order_id, status: next.target });
                      }
                    } else {
                      transitionMutation.mutate({ id: order.order_id, status: next.target });
                    }
                  }}
                >
                  {next.label}
                </Button>
              );
              return button;
            })()}
            {/* 「修改订单」提到主操作区、紧跟「确认订单」后面单独成按钮
                （前台 2026-07-19 反馈：高频操作别埋进「更多」下拉）。 */}
            {canEditOrder && !isCancelled && (
              <Button icon={<EditOutlined />} onClick={() => setEditOrderOpen(true)}>
                修改订单
              </Button>
            )}
          </Space>
        </div>
        )
      }
    >
      <div
        hidden={isMobile && splitOpen}
        style={{ padding: isMobile ? 14 : 20, display: "flex", flexDirection: "column", gap: 16 }}
      >
        {activeTab === "detail" && (
        <>
        {manualControl?.is_relevant ? (
          <>
            {showSponsoredSettlement ? (
            <section
              ref={settlementAnchorRef}
              tabIndex={-1}
              aria-labelledby="stay-settlement-heading"
              className="stay-settlement-anchor"
            >
              <div className="stay-settlement-anchor__heading">
                <div>
                  <h2 id="stay-settlement-heading">住宿与结算</h2>
                  <p>
                    {settlementCheckIn} → {settlementCheckOut} · {settlementNights} 晚 ·{" "}
                    {(stayGroup?.rooms?.length ? stayGroup.rooms : orderRooms.map((room) => room.room_id).filter(Boolean)).join(" → ") || "待排房"}
                  </p>
                </div>
                <div className="stay-settlement-labels">
                  {settlementKinds.map((kind) => (
                    <StaySettlementLabel key={kind} kind={kind} />
                  ))}
                  {hasManualOwnership ? <StaySettlementLabel kind="manual_override" /> : null}
                </div>
              </div>
              <dl className="stay-settlement-anchor__impact">
                <div>
                  <dt>客人应付</dt>
                  <dd>¥0</dd>
                </div>
                <div>
                  <dt>公司承担</dt>
                  <dd>
                    {sponsorshipEntries.length > 0
                      ? `¥${companySponsorshipTotal.toFixed(2)}`
                      : "待服务器预览"}
                  </dd>
                </div>
                <div>
                  <dt>来源价格</dt>
                  <dd>
                    {manualControl.source_price_snapshot
                      ? `¥${Number(manualControl.source_price_snapshot.total).toFixed(2)}`
                      : "暂缺"}
                  </dd>
                </div>
              </dl>
              {sponsorshipEntries.map(({ sponsorship, channel }, index) => (
                <p
                  key={sponsorship.sponsored_stay_id || `${channel}-${index}`}
                  className="stay-settlement-anchor__formula"
                >
                  {CHANNEL_LABELS[channel] || channel}{" "}
                  ¥{Number(sponsorship.calculation_base).toFixed(2)} ×{" "}
                  {Number(sponsorship.settlement_ratio).toFixed(2)} ={" "}
                  ¥{Number(sponsorship.effective_amount).toFixed(2)}
                </p>
              ))}
              <div className="stay-settlement-anchor__actions">
                {manualControl.split.enabled === true && manualControl.split.visible ? (
                  <Button
                    type="primary"
                    disabled={!manualControl.split.eligible || !manualControl.can_write}
                    onClick={() => setSplitOpen(true)}
                  >
                    拆分住宿段
                  </Button>
                ) : null}
                {manualControl.split.enabled === true && sourcePriceCanBeCorrected ? (
                  <Button onClick={() => setSourcePriceOpen(true)}>更正来源价格</Button>
                ) : null}
                {!manualControl.split.eligible && manualControl.split.blocker_message ? (
                  <span role="note">{manualControl.split.blocker_message}</span>
                ) : null}
              </div>
            </section>
            ) : null}

            {showManualControl ? (
              <ManualOverridePanel orderId={manualControlOrderId} control={manualControl} />
            ) : null}
          </>
        ) : manualControlQuery.isError ? (
          <section className="stay-settlement-anchor" aria-labelledby="stay-settlement-heading">
            <h2 id="stay-settlement-heading">住宿与结算</h2>
            <p>暂时无法判断能否拆分</p>
            <Button onClick={() => manualControlQuery.refetch()}>重试</Button>
          </section>
        ) : null}

        {/* Guest card */}
        <div
          style={{
            background: tokens.color.bg.container,
            borderRadius: tokens.radius.lg,
            border: `1px solid ${tokens.color.bg.border}`,
            padding: 16,
            display: "flex",
            alignItems: "center",
            gap: 12,
          }}
        >
          <div
            style={{
              width: 44,
              height: 44,
              borderRadius: "50%",
              background: tokens.color.brand.primarySoft,
              color: tokens.color.brand.primary,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              fontWeight: 600,
              fontSize: 18,
              flex: "0 0 44px",
            }}
          >
            {order.guest_name?.charAt(0) ?? "?"}
          </div>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontSize: 16, fontWeight: 600 }}>{order.guest_name}</div>
            <div style={{ fontSize: 13, color: tokens.color.text.secondary }}>
              {order.guest_phone} · {CHANNEL_LABELS[order.channel] || order.channel}
            </div>
          </div>
          <StatusBadge status={order.payment_status ?? "unpaid"} size="sm" />
        </div>

        {/* 续住候选提示：关联入口原本藏在「更多」下拉里，前台得知道有这功能、还得记得去点
            → 生产上 11 组同名同房日期相连却没关联。触发条件完全交给后端（link_candidates 非空），
            前端不判断「什么时候算续住」——那个口径在 find_checkout_continuations 里，另写一份必漂。
            不自动关联：同名不同人被拴成一段 = 一个密码给两拨客人，误判代价远高于收益。 */}
        {(stayGroup?.link_candidates?.length ?? 0) > 0 && (
          <Alert
            type="info"
            showIcon
            message="看着像续住"
            description={`同房间还有一张${stayGroup!.link_candidates[0].guest_name}的单，紧接着这一段。关联后按一趟住宿算：一个门锁密码、押金不重复收。`}
            action={
              <Button size="small" type="primary" onClick={() => setLinkStayOpen(true)}>
                关联
              </Button>
            }
          />
        )}

        {/* 零收款已入住风险提示：人已入住但房费/押金没收 → 醒目提醒前台，别漏收 */}
        {checkinRisks.length > 0 && (
          <div
            role="alert"
            style={{
              background: tokens.color.status.warnSoft,
              border: `1px solid ${tokens.color.status.warn}`,
              borderRadius: tokens.radius.lg,
              padding: "10px 14px",
              display: "flex",
              alignItems: "center",
              gap: 8,
            }}
          >
            <WarningOutlined style={{ color: tokens.color.status.warn, fontSize: 16, flex: "0 0 auto" }} />
            <span style={{ fontSize: 13, color: tokens.color.status.warn, fontWeight: 600 }}>
              已入住但{checkinRisks.map((r) => r.label).join("、")}，请及时收取
            </span>
          </div>
        )}

        {/* Stay info */}
        <div
          style={{
            background: tokens.color.bg.container,
            borderRadius: tokens.radius.lg,
            border: `1px solid ${tokens.color.bg.border}`,
            padding: 16,
            display: "grid",
            gridTemplateColumns: "1fr 1fr",
            gap: 16,
          }}
        >
          {/* 续住组：整段头部。点组内任意一段，这块都长得一模一样——本功能的核心承诺。
              日期/晚数/房间序列全取后端 group_view 的口径（已排除取消段），前端不重算。 */}
          {isGroup && (
            <div style={{ gridColumn: "1 / -1", display: "flex", flexDirection: "column", gap: 8 }}>
              <Field label="整段日期">
                <span className="tabular">
                  {stayGroup!.check_in_date} → {stayGroup!.check_out_date}
                </span>
                <span style={{ marginLeft: 8, fontSize: 12, color: tokens.color.text.secondary }}>
                  {/* 段数按活段计（与 nights/金额同为活段口径），取消段注明不吞掉 */}
                  {stayGroup!.nights} 晚 · 共 {segmentCountLabel(stayGroup!.segments)}
                </span>
              </Field>
              <Field label={`房间（${stayGroup!.rooms.length} 间）`}>
                {/* 跨房间续住组（生产实例 sg_4909d1907fb8）会是 1605 → 1606 */}
                {stayGroup!.rooms.length > 0 ? (
                  stayGroup!.rooms.join(" → ")
                ) : (
                  <span style={{ color: tokens.color.text.tertiary, fontWeight: 400 }}>待排房</span>
                )}
              </Field>
            </div>
          )}

          {/* Multi-room: 每房一行；单房订单也用同一渲染逻辑，保持视觉一致。
              续住组不渲染这块：它显示的是被点那一段自己的日期与金额，正是本功能要消灭的
              「点 7/16 和点 7/17 看到不同的数」。段内改日期/换房/单日改价改走「分段明细 → 点某段」。 */}
          {!isGroup && (
          <div style={{ gridColumn: "1 / -1" }}>
            <div style={{ fontSize: 12, color: tokens.color.text.tertiary, marginBottom: 8 }}>
              房间（{orderRooms.length} 间）
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              {orderRooms.map((or, idx) => {
                const nights = or.nights ?? (
                  or.check_in_date && or.check_out_date
                    ? Math.max(0, (new Date(or.check_out_date).getTime() - new Date(or.check_in_date).getTime()) / 86400000)
                    : 0
                );
                // 生成入住区间内的日期数组（左闭右开，nights 天）。不依赖 daily_prices key 顺序。
                const dailyDates: string[] = [];
                if (or.check_in_date && or.check_out_date) {
                  let d = dayjs(or.check_in_date);
                  const end = dayjs(or.check_out_date);
                  while (d.isBefore(end)) {
                    dailyDates.push(d.format("YYYY-MM-DD"));
                    d = d.add(1, "day");
                  }
                }
                const dailyPrices: Record<string, string> = or.daily_prices || {};
                // 老订单（legacy 合成行）禁止单日改价
                const isLegacyRow = String(or.order_room_id || "").startsWith("__legacy_");
                // 兜底均价（daily_prices 没回填的天数显示用）
                const fallbackAvg = nights > 0 && or.actual_price != null
                  ? Number(or.actual_price) / nights
                  : 0;
                return (
                  <div
                    key={or.order_room_id || idx}
                    style={{
                      display: "flex",
                      flexDirection: "column",
                      gap: 8,
                      padding: "8px 12px",
                      background: tokens.color.bg.page,
                      borderRadius: tokens.radius.md,
                      border: `1px solid ${tokens.color.bg.border}`,
                    }}
                  >
                    {/* 第一行：房号 / 日期区间（可点击改） / 晚数 / 实收 / 续住 / 换房按钮 */}
                    <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
                      <span style={{ minWidth: 60, fontWeight: 600 }}>
                        {or.room_id || <span style={{ color: tokens.color.text.tertiary, fontWeight: 400 }}>待排房</span>}
                      </span>
                      {/* 内联日期编辑：点击日期文字打开 RangePicker Modal，无需跳"修改订单" */}
                      {canEditOrder && !isCancelled && !isLegacyRow ? (
                        <Tooltip title="点击调整入住 / 退房日期（续住或缩短住）">
                          <a
                            onClick={(e) => {
                              e.stopPropagation();
                              setEditDatesCtx({
                                orderRoomId: or.order_room_id,
                                oldCheckIn: or.check_in_date,
                                oldCheckOut: or.check_out_date,
                              });
                            }}
                            style={{
                              fontSize: 13,
                              color: tokens.color.brand.primary,
                              cursor: "pointer",
                              textDecoration: "underline",
                              textDecorationStyle: "dashed",
                              textUnderlineOffset: 3,
                            }}
                          >
                            {or.check_in_date} → {or.check_out_date}
                          </a>
                        </Tooltip>
                      ) : (
                        <span style={{ fontSize: 13, color: tokens.color.text.secondary }}>
                          {or.check_in_date} → {or.check_out_date}
                        </span>
                      )}
                      <span style={{ fontSize: 12, color: tokens.color.text.tertiary }}>
                        {nights} 晚
                      </span>
                      <span className="tabular" style={{ marginLeft: "auto", color: tokens.color.brand.primary, fontWeight: 600 }}>
                        ¥{Number(or.actual_price ?? 0).toLocaleString()}
                      </span>
                      {/* 晚数调整 stepper：[− N 晚 +]，能加能减 */}
                      {canEditOrder && !isCancelled && !isLegacyRow && (
                        <div
                          style={{
                            display: "inline-flex",
                            alignItems: "center",
                            border: `1px solid ${tokens.color.bg.border}`,
                            borderRadius: 6,
                            overflow: "hidden",
                            height: 24,
                          }}
                        >
                          <Tooltip title={nights <= 1 ? "至少保留 1 晚" : "减少 1 晚（保存后多出的那天会被移除）"}>
                            <Button
                              type="text"
                              size="small"
                              disabled={nights <= 1 || editDatesMutation.isPending}
                              onClick={() => {
                                const newCo = dayjs(or.check_out_date).subtract(1, "day").format("YYYY-MM-DD");
                                editDatesMutation.mutate({
                                  orderRoomId: or.order_room_id,
                                  ci: or.check_in_date,
                                  co: newCo,
                                });
                              }}
                              style={{ height: 22, padding: "0 8px", borderRadius: 0, fontSize: 14, lineHeight: 1 }}
                            >
                              −
                            </Button>
                          </Tooltip>
                          <span
                            style={{
                              padding: "0 8px",
                              fontSize: 12,
                              color: tokens.color.text.secondary,
                              borderLeft: `1px solid ${tokens.color.bg.border}`,
                              borderRight: `1px solid ${tokens.color.bg.border}`,
                              lineHeight: "22px",
                              minWidth: 38,
                              textAlign: "center",
                            }}
                          >
                            {nights}晚
                          </span>
                          <Tooltip title="续住 +1 晚（新增日期默认 ¥0，可点 chip 单独定价并自动均摊）">
                            <Button
                              type="text"
                              size="small"
                              disabled={editDatesMutation.isPending}
                              onClick={() => {
                                const newCo = dayjs(or.check_out_date).add(1, "day").format("YYYY-MM-DD");
                                editDatesMutation.mutate({
                                  orderRoomId: or.order_room_id,
                                  ci: or.check_in_date,
                                  co: newCo,
                                });
                              }}
                              style={{ height: 22, padding: "0 8px", borderRadius: 0, fontSize: 14, lineHeight: 1 }}
                            >
                              +
                            </Button>
                          </Tooltip>
                        </div>
                      )}
                      {(or.room_id ? canTransferRoom : canAssignRoom) && (
                        <Button
                          size="small"
                          type={or.room_id ? "default" : "primary"}
                          icon={<HomeOutlined />}
                          onClick={() => {
                            if (or.room_id) {
                              // 已排房 → 走统一「换房」弹窗（带原因 + 房态/门锁联动）
                              // 多房订单：传被点击行的 order_room_id，确保换的是这一行
                              setTransferRowId(or.order_room_id);
                              setTransferOpen(true);
                            } else {
                              // 待排房 → 内联排房
                              setPickedOrderRoomId(or.order_room_id);
                              setAssignOpen(true);
                            }
                          }}
                        >
                          {or.room_id ? "换房" : "排房"}
                        </Button>
                      )}
                    </div>
                    {/* 第二行：每日房价 chips（可点击单日改价） */}
                    {dailyDates.length > 0 && (
                      <div
                        style={{
                          display: "flex",
                          flexWrap: "nowrap",
                          gap: 6,
                          overflowX: "auto",
                          paddingBottom: 2,
                        }}
                      >
                        {dailyDates.map((d) => {
                          const hasValue = dailyPrices[d] != null;
                          const price = hasValue ? Number(dailyPrices[d]) : fallbackAvg;
                          const clickable = canEditOrder && !isLegacyRow && !isCancelled;
                          return (
                            <Tooltip
                              key={d}
                              title={clickable ? "点击调整房费（保存后自动均摊到所有晚数）" : (isLegacyRow ? "老订单暂不支持单日改价" : "")}
                            >
                              <div
                                onClick={() => {
                                  if (!clickable) return;
                                  setEditDailyCtx({
                                    orderRoomId: or.order_room_id,
                                    date: d,
                                    oldPrice: price,
                                  });
                                }}
                                style={{
                                  flex: "0 0 auto",
                                  minWidth: 88,
                                  padding: "6px 10px",
                                  borderRadius: tokens.radius.sm,
                                  border: `1px solid ${tokens.color.bg.border}`,
                                  background: tokens.color.bg.container,
                                  display: "flex",
                                  flexDirection: "column",
                                  alignItems: "center",
                                  cursor: clickable ? "pointer" : "default",
                                  opacity: hasValue ? 1 : 0.7,
                                }}
                              >
                                <span style={{ fontSize: 11, color: tokens.color.text.tertiary }}>{d}</span>
                                <span
                                  className="tabular"
                                  style={{
                                    fontSize: 13,
                                    fontWeight: 600,
                                    color: tokens.color.text.primary,
                                  }}
                                >
                                  ¥{price.toFixed(2)}
                                </span>
                              </div>
                            </Tooltip>
                          );
                        })}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
          )}
          {/* 任一活段价格还没从 OTA 回填（占位价 0）→ 整段房费必然少算，显示「同步中…」
              而不是那个假数。布尔或而已，金额本身仍只用后端的 total_amount。 */}
          <Field label={isGroup ? "整段房费" : "房费合计"}>
            {(isGroup
              ? stayGroup!.segments.some((s) => s.order_status !== "cancelled" && s.price_pending)
              : order.price_pending) ? (
              <span style={{ color: tokens.color.text.tertiary, fontWeight: 600 }}>
                价格同步中…
              </span>
            ) : (
              <span className="tabular" style={{ color: tokens.color.brand.primary, fontWeight: 600 }}>
                ¥{Number((isGroup ? stayGroup!.total_amount : order.actual_price) ?? 0).toLocaleString()}
              </span>
            )}
          </Field>
          {/* 净房费（业主到手）是**按单口径**：组级后端没这个字段，前端又不许累加
              （累加=在 TS 里重实现 group_view，且必漏「取消段不计入」）。所以续住组这里不显示
              ——否则会把首段一家的到手价摆在「整段房费」旁边，业主以为整段就到手这么点
              （王总 2026-07-18 真机抓到：整段 ¥693.28 旁写着首段的 ¥258.37）。
              续住组的净房费下沉到「分段明细」每行，各段各显示自己的。 */}
          {!isGroup && formatExpectedRevenue(order.expected_revenue) && (
            <Field label="净房费">
              <span className="tabular" style={{ color: tokens.color.brand.primary, fontWeight: 600 }}>
                {formatExpectedRevenue(order.expected_revenue)}
              </span>
            </Field>
          )}
          {/* 押金已下线（王总 2026-07-22）：改走线下 POS + 飞书小票，新单不再有押金。
              仅当历史单真收/退过押金（金额 > 0 或已有退还记录）才只读展示，供查账；
              新单 deposit=0 整行隐藏，不再出现「¥0 未收」。 */}
          {(() => {
            const depositAmt = Number((isGroup ? stayGroup!.deposit : order.deposit) ?? 0);
            const depositReturned = isGroup ? stayGroup?.deposit_returned : order.deposit_returned;
            if (depositAmt <= 0 && depositReturned == null) return null;
            const withheld = Number(depositAmt) - Number(depositReturned);
            return (
              <Field label={isGroup ? "整段押金" : "押金"}>
                <span className="tabular">¥{depositAmt.toLocaleString()}</span>{" "}
                <StatusBadge
                  status={(isGroup ? stayGroup!.deposit_status : order.deposit_status) ?? "not_collected"}
                  size="sm"
                />
                {depositReturned != null && (
                  <span style={{ marginLeft: 8, fontSize: 12, color: tokens.color.text.secondary }}>
                    实退 ¥{Number(depositReturned).toLocaleString()}
                    {withheld > 0 && (
                      <span style={{ color: tokens.color.status.warn }}>
                        {" "}· 扣款 ¥{withheld.toLocaleString()}
                      </span>
                    )}
                  </span>
                )}
              </Field>
            );
          })()}
          {/* issue#6: 业主应付预览仅在单房 trial/owner_self 显示 */}
          {ownerAmount != null && ownerShareRatio != null && (
            <Field label="业主应付">
              <span className="tabular" style={{ color: tokens.color.text.primary }}>
                ¥{Number(ownerAmount).toLocaleString(undefined, { maximumFractionDigits: 2 })}
              </span>
              <span style={{ marginLeft: 6, fontSize: 11, color: tokens.color.text.tertiary }}>
                = 房费 × {(ownerShareRatio * 100).toFixed(0)}%
              </span>
            </Field>
          )}
          {/* 分段明细：默认收起。前台不展开就完全看不到底下是多张单——本功能的全部意义。
              展开是给做账用的（各段渠道/平台单号/金额分开列），点某一段编辑那一段。 */}
          {stayGroup && (
            <div style={{ gridColumn: "1 / -1" }}>
              <StaySegmentBreakdown
                group={stayGroup}
                onSegmentClick={(oid) => {
                  const seg = stayGroup.segments.find((s) => s.order_id === oid);
                  if (seg) setEditSegment(seg);
                }}
              />
            </div>
          )}
        </div>

        {/* Payments */}
        <div
          style={{
            background: tokens.color.bg.container,
            borderRadius: tokens.radius.lg,
            border: `1px solid ${tokens.color.bg.border}`,
            padding: 16,
          }}
        >
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              marginBottom: 12,
            }}
          >
            <span style={{ fontWeight: 600 }}>收款记录</span>
            <span
              className="tabular"
              style={{
                color: tokens.color.status.active,
                fontWeight: 600,
                fontSize: 14,
              }}
            >
              已收 ¥{totalPaid.toLocaleString()}
            </span>
          </div>
          {paymentList.length === 0 ? (
            <div style={{ padding: "8px 0", fontSize: 13, color: tokens.color.text.tertiary }}>
              暂无收款
            </div>
          ) : (
            <Timeline
              style={{ marginBottom: -12, marginTop: 4 }}
              items={paymentList.map((p: any) => ({
                color: p.is_deposit ? tokens.color.status.pending : tokens.color.brand.primary,
                children: (
                  <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "flex-start" }}>
                    <div>
                      <div style={{ fontWeight: 500, fontSize: 13 }}>
                        {PAYMENT_METHOD_LABELS[p.method] || p.method}
                        {/* 组单：标注这笔钱记在哪一段（各段账独立，记错段直接坑 OTA 对账） */}
                        {p.__segLabel && (
                          <span
                            style={{
                              marginLeft: 8,
                              fontSize: 11,
                              color: tokens.color.text.tertiary,
                              fontWeight: 400,
                            }}
                          >
                            {p.__segLabel}
                          </span>
                        )}
                        {p.is_deposit && (
                          <span style={{ marginLeft: 8 }}>
                            <StatusBadge status="pending" label="押金" size="sm" />
                          </span>
                        )}
                      </div>
                      <div style={{ fontSize: 12, color: tokens.color.text.tertiary, marginTop: 2 }}>
                        {dayjs(p.paid_at).format("YYYY-MM-DD HH:mm")}
                      </div>
                      {p.notes && (
                        <div style={{ fontSize: 12, color: tokens.color.text.secondary, marginTop: 2 }}>
                          {p.notes}
                        </div>
                      )}
                    </div>
                    <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
                      <span
                        className="tabular"
                        style={{ color: tokens.color.status.active, fontWeight: 600 }}
                      >
                        +¥{Number(p.amount).toLocaleString()}
                      </span>
                      {canEditPayment && !isCancelled && (
                        <Dropdown
                          trigger={["click"]}
                          menu={{
                            items: [
                              {
                                key: "edit",
                                icon: <EditOutlined />,
                                label: "编辑",
                                onClick: () => setEditingPayment(p),
                              },
                              {
                                key: "delete",
                                icon: <DeleteOutlined />,
                                label: "删除",
                                danger: true,
                                onClick: () => {
                                  Modal.confirm({
                                    title: "删除这条收款？",
                                    content: `${PAYMENT_METHOD_LABELS[p.method] || p.method} · ¥${Number(p.amount).toLocaleString()}。删除后订单已收金额会同步减少。`,
                                    okText: "删除",
                                    okButtonProps: { danger: true },
                                    cancelText: "取消",
                                    onOk: () => deletePaymentMutation.mutateAsync(p.payment_id),
                                  });
                                },
                              },
                            ],
                          }}
                        >
                          <Button
                            type="text"
                            size="small"
                            icon={<MoreOutlined />}
                            style={{ minWidth: 24, padding: "0 4px" }}
                          />
                        </Dropdown>
                      )}
                    </div>
                  </div>
                ),
              }))}
            />
          )}
        </div>

        {/* 门锁密码兜底：飞书密码卡发不出/被刷走时，前台在这里直接看+复制转发给客人。
            仅在住/待退房 + admin/operator 可见（口径同「重发密码卡」）。 */}
        {showLockCodeSection && (
          <div
            style={{
              background: tokens.color.bg.container,
              borderRadius: tokens.radius.lg,
              border: `1px solid ${tokens.color.bg.border}`,
              padding: 16,
            }}
          >
            <div
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                marginBottom: lockCodes && lockCodes.length > 0 ? 10 : 0,
              }}
            >
              <span style={{ fontSize: 12, color: tokens.color.text.tertiary }}>
                <LockOutlined style={{ marginRight: 6 }} />
                门锁密码
              </span>
              <Button
                size="small"
                type="text"
                icon={<LockOutlined />}
                onClick={() => {
                  ordersApi
                    .resendLockCard(order!.order_id)
                    .then(() => message.success("密码卡已重新推送到飞书密码群"))
                    .catch((e) => message.error(extractErrorMessage(e, "重发失败")));
                }}
              >
                重发到飞书
              </Button>
            </div>
            {lockCodes === undefined ? (
              <div style={{ fontSize: 13, color: tokens.color.text.tertiary }}>加载中…</div>
            ) : lockCodes.length === 0 ? (
              lockCodesIssuing ? (
                // 码正在下发/重试中（锁一时离线）：诚实告知会自愈，别再吓前台说「未办理入住/失效」。
                <div style={{ fontSize: 13, color: tokens.color.brand.primary }}>
                  <LoadingOutlined style={{ marginRight: 6 }} />
                  门锁密码正在下发中（锁可能一时离线），系统会自动重推，稍候即到，无需手动操作。
                </div>
              ) : (
                <div style={{ fontSize: 13, color: tokens.color.text.tertiary }}>
                  暂无可用密码（未办理入住 / 房间未绑锁 / 码已失效）
                </div>
              )
            ) : (
              <Space direction="vertical" size={8} style={{ width: "100%" }}>
                {lockCodes.map((c) => (
                  <div
                    key={c.room_id}
                    style={{
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "space-between",
                      gap: 12,
                    }}
                  >
                    <span style={{ fontSize: 13, color: tokens.color.text.secondary }}>
                      {c.room_name}
                    </span>
                    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                      <span
                        className="tabular"
                        style={{
                          fontSize: 18,
                          fontWeight: 700,
                          letterSpacing: 2,
                          color: tokens.color.text.primary,
                        }}
                      >
                        {c.password}
                      </span>
                      <Button
                        size="small"
                        icon={<CopyOutlined />}
                        onClick={() => {
                          navigator.clipboard
                            ?.writeText(c.password)
                            .then(() => message.success(`已复制 ${c.room_name} 密码`))
                            .catch(() => message.error("复制失败，请手动长按选择"));
                        }}
                      >
                        复制
                      </Button>
                    </div>
                  </div>
                ))}
              </Space>
            )}
          </div>
        )}

        {order.notes && (
          <div
            style={{
              background: tokens.color.bg.container,
              borderRadius: tokens.radius.lg,
              border: `1px solid ${tokens.color.bg.border}`,
              padding: 16,
            }}
          >
            <div style={{ fontSize: 12, color: tokens.color.text.tertiary, marginBottom: 6 }}>
              备注
            </div>
            <div style={{ fontSize: 14, lineHeight: 1.6, whiteSpace: "pre-wrap" }}>
              {order.notes}
            </div>
          </div>
        )}

        </>
        )}
        {activeTab === "audit" && (
          <div
            style={{
              background: tokens.color.bg.container,
              borderRadius: tokens.radius.lg,
              border: `1px solid ${tokens.color.bg.border}`,
              padding: 16,
            }}
          >
            <OrderAuditTimeline orderId={order.order_id} />
          </div>
        )}
      </div>

      {isMobile && manualControl && splitOpen ? (
        <div className="split-stay-child-view">
          <SplitStayDialog
            open
            embedded
            order={order}
            control={manualControl}
            rooms={(roomList as RoomOut[] | undefined) ?? []}
            autoPreviewKey={autoPreviewKey || undefined}
            onBack={closeSplit}
            onComplete={() => undefined}
            onPendingChange={setSplitPending}
          />
        </div>
      ) : manualControl && splitOpen ? (
        <SplitStayDialog
          open
          order={order}
          control={manualControl}
          rooms={(roomList as RoomOut[] | undefined) ?? []}
          autoPreviewKey={autoPreviewKey || undefined}
          onBack={closeSplit}
          onComplete={() => undefined}
          onPendingChange={setSplitPending}
        />
      ) : null}

      {manualControl ? (
        <SourcePriceOverrideDialog
          open={sourcePriceOpen}
          orderId={manualControlOrderId}
          canAdminister={manualControl.can_administer}
          checkInDate={order.check_in_date}
          checkOutDate={order.check_out_date}
          currentSnapshot={manualControl.source_price_snapshot ?? null}
          onClose={() => setSourcePriceOpen(false)}
          onSaved={async () => {
            setSourcePriceOpen(false);
            const refreshed = await manualControlQuery.refetch();
            if (refreshed.data?.split.eligible) {
              setAutoPreviewKey((current) => current + 1);
              setSplitOpen(true);
            }
          }}
        />
      ) : null}

      <AssignRoomModal
        open={assignOpen}
        order={order}
        orderRooms={orderRooms}
        orderRoomId={pickedOrderRoomId}
        onClose={() => {
          setAssignOpen(false);
          setPickedOrderRoomId(undefined);
        }}
      />

      <CheckoutModal
        open={checkoutOpen}
        order={order}
        onClose={() => setCheckoutOpen(false)}
      />

      {/* 办理入住：收押金确认 */}
      <CheckinDepositModal
        open={checkinOpen}
        order={order}
        onClose={() => setCheckinOpen(false)}
      />

      <RevertCheckoutModal
        open={revertOpen}
        order={order}
        onClose={() => setRevertOpen(false)}
      />

      <RevertCheckinModal
        open={revertCheckinOpen}
        order={order}
        onClose={() => setRevertCheckinOpen(false)}
      />

      <ExtendLockModal
        open={extendOpen}
        order={order}
        onClose={() => setExtendOpen(false)}
      />

      <LinkStayModal
        open={linkStayOpen}
        order={order}
        onClose={() => setLinkStayOpen(false)}
      />

      <EditOrderModal
        open={editOrderOpen}
        order={order}
        onClose={() => setEditOrderOpen(false)}
      />

      {/* 段内操作：从分段明细点进来，编辑的是被点的那一段（不是当前这段）。
          续住组的房行区块不再渲染，改日期/改价的入口收敛到这里。 */}
      {editSegment && (
        <EditOrderModal
          open={!!editSegment}
          order={editSegment}
          onClose={() => setEditSegment(null)}
        />
      )}

      {/* 统一换房弹窗（免费升级 / 房间故障 / 客人要求）——已排房订单走此处 */}
      <TransferRoomModal
        order={transferOpen ? order : null}
        orderRoomId={transferRowId}
        onClose={() => {
          setTransferOpen(false);
          setTransferRowId(undefined);
        }}
      />

      {/* 内联调整入住/退房日期 — 续住 / 缩短住 都走这里，无需跳"修改订单" */}
      <EditDatesModal
        ctx={editDatesCtx}
        mutation={editDatesMutation}
        onClose={() => setEditDatesCtx(null)}
      />

      {/* 单日改价 */}
      <DailyPriceModal
        ctx={editDailyCtx}
        order={order}
        orderRooms={orderRooms}
        totalPaid={totalPaid}
        onClose={() => setEditDailyCtx(null)}
      />

      <PaymentModal
        open={!!editingPayment}
        order={order}
        payment={editingPayment}
        payments={paymentList}
        onClose={() => setEditingPayment(null)}
        onFinish={(values) => updatePaymentMutation.mutate(values)}
        loading={updatePaymentMutation.isPending}
      />
    </Drawer>
  );
}
