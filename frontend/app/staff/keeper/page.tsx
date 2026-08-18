"use client";

import { useEffect, useState, useMemo, Suspense } from "react";
import { extractErrorMessage } from "@/lib/api-errors";
import { useRouter, useSearchParams } from "next/navigation";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Empty, Skeleton, Tag, Segmented, Spin, Drawer, Button,
  Input, InputNumber, Select, message, Tag as AntTag,
} from "antd";
import dayjs from "dayjs";
import { staffDataApi, type StaffOrderItem, type CleanerBrief, type StaffTask } from "@/lib/staff-api";
import { useStaffStore } from "@/lib/staff-store";
import { CHANNEL_LABEL } from "@/lib/channels";

const ROOM_STATUS_COLOR: Record<string, string> = {
  available: "#7B8578",     // sage
  occupied: "#2B2721",      // ink
  reserved: "#6B665B",      // stone
  pending_clean: "#C19B6E", // 暖棕 — 待清扫
  cleaning: "#D4A017",      // 琥珀 — 清扫中
  maintenance: "#8A6E5A",   // clay
  locked: "#A89680",        // driftwood
};

const ROOM_STATUS_LABEL: Record<string, string> = {
  available: "空闲",
  occupied: "在住",
  reserved: "预订",
  pending_clean: "待清扫",
  cleaning: "清扫中",
  maintenance: "维护",
  locked: "锁定",
};

export default function KeeperPage() {
  return (
    <Suspense
      fallback={
        <div style={{ minHeight: "100dvh", display: "flex", alignItems: "center", justifyContent: "center" }}>
          <Spin />
        </div>
      }
    >
      <KeeperInner />
    </Suspense>
  );
}

function KeeperInner() {
  const router = useRouter();
  const search = useSearchParams();
  const qc = useQueryClient();
  const isLoggedIn = useStaffStore((s) => s.isLoggedIn());
  const user = useStaffStore((s) => s.user);

  const [tab, setTab] = useState<"today" | "rooms" | "inspect">(() => {
    const t = search.get("tab");
    if (t === "rooms") return "rooms";
    if (t === "inspect") return "inspect";
    return "today";
  });

  const [actionOrder, setActionOrder] = useState<
    { order: StaffOrderItem; kind: "in" | "out" } | null
  >(null);
  const [actionNotes, setActionNotes] = useState("");
  const [pickedCleanerId, setPickedCleanerId] = useState<string | undefined>();
  // 退房押金结算：实退金额（默认全退）+ 扣款原因（实退<押金时必填）
  const [refundAmount, setRefundAmount] = useState<number>(0);
  const [withholdReason, setWithholdReason] = useState("");

  const { data: cleaners } = useQuery({
    queryKey: ["staff", "cleaners"],
    queryFn: async () => (await staffDataApi.listCleaners()).data,
    enabled: isLoggedIn,
    staleTime: 5 * 60 * 1000,
  });

  const checkinMutation = useMutation({
    mutationFn: (orderId: string) => staffDataApi.handleCheckin(orderId, actionNotes || undefined),
    onSuccess: () => {
      message.success("已办理入住");
      qc.invalidateQueries({ queryKey: ["staff", "checkIns"] });
      qc.invalidateQueries({ queryKey: ["staff", "calendar"] });
      qc.invalidateQueries({ queryKey: ["rooms"] });
      closeAction();
    },
    onError: (e: any) => message.error(extractErrorMessage(e, "办理失败")),
  });

  const checkoutMutation = useMutation({
    mutationFn: (orderId: string) => {
      // cleaner 选填（批3 item1「暂不派单」）：留空 = 建未分配清扫任务进待派池，稍后再派。
      const dep = Number(actionOrder?.order.deposit ?? 0);
      const settle =
        actionOrder?.order.deposit_status === "collected" && dep > 0
          ? {
              deposit_refund_amount: refundAmount,
              deposit_withhold_reason:
                refundAmount < dep ? withholdReason.trim() : undefined,
            }
          : undefined;
      return staffDataApi.handleCheckout(orderId, pickedCleanerId, actionNotes || undefined, settle);
    },
    onSuccess: () => {
      message.success(pickedCleanerId ? "已办理退房,清扫任务已派发" : "已办理退房（暂不派单,任务进待派池）");
      qc.invalidateQueries({ queryKey: ["staff", "checkOuts"] });
      qc.invalidateQueries({ queryKey: ["staff", "calendar"] });
      qc.invalidateQueries({ queryKey: ["rooms"] });
      closeAction();
    },
    onError: (e: any) => message.error(extractErrorMessage(e, "办理失败")),
  });

  const reviewMutation = useMutation({
    mutationFn: ({ taskId, approved, reason }: { taskId: string; approved: boolean; reason?: string }) =>
      staffDataApi.reviewTask(taskId, approved, reason),
    onSuccess: (_, vars) => {
      message.success(vars.approved ? "查房通过,房间已恢复可入住" : "已驳回,需重新打扫");
      qc.invalidateQueries({ queryKey: ["staff", "tasks"] });
      qc.invalidateQueries({ queryKey: ["staff", "calendar"] });
      qc.invalidateQueries({ queryKey: ["rooms"] });
    },
    onError: (e: any) => message.error(extractErrorMessage(e, "操作失败")),
  });

  const openAction = (order: StaffOrderItem, kind: "in" | "out") => {
    setActionOrder({ order, kind });
    setActionNotes("");
    setWithholdReason("");
    setRefundAmount(Number(order.deposit ?? 0)); // 退房默认全退押金
    // 默认选上第一个保洁
    if (kind === "out" && cleaners && cleaners.length > 0) {
      setPickedCleanerId(cleaners[0].user_id);
    } else {
      setPickedCleanerId(undefined);
    }
  };

  const closeAction = () => {
    setActionOrder(null);
    setActionNotes("");
    setPickedCleanerId(undefined);
    setWithholdReason("");
  };

  useEffect(() => {
    if (!isLoggedIn) {
      router.replace(`/staff/login?next=${encodeURIComponent("/staff/keeper")}`);
    }
  }, [isLoggedIn, router]);

  const today = dayjs().format("YYYY-MM-DD");
  const now = dayjs();

  const { data: checkIns, isLoading: l1 } = useQuery({
    queryKey: ["staff", "checkIns", today],
    queryFn: async () =>
      (await staffDataApi.ordersList({
        check_in_from: today,
        check_in_to: today,
        page: 1,
        page_size: 50,
      })).data,
    enabled: isLoggedIn && tab === "today",
  });

  const { data: checkOuts, isLoading: l2 } = useQuery({
    queryKey: ["staff", "checkOuts", today],
    queryFn: async () =>
      (await staffDataApi.ordersList({
        check_out_from: today,
        check_out_to: today,
        page: 1,
        page_size: 50,
      })).data,
    enabled: isLoggedIn && tab === "today",
  });

  const { data: calendar, isLoading: l3 } = useQuery({
    queryKey: ["staff", "calendar", now.year(), now.month() + 1],
    queryFn: async () =>
      (await staffDataApi.roomsCalendar(now.year(), now.month() + 1)).data,
    enabled: isLoggedIn && tab === "rooms",
  });

  const { data: pendingTasks } = useQuery({
    queryKey: ["staff", "tasks", "pending"],
    queryFn: async () => (await staffDataApi.tasks({ status: "pending" })).data,
    enabled: isLoggedIn && tab === "today",
  });

  const { data: inspectTasks, isLoading: l4 } = useQuery({
    queryKey: ["staff", "tasks", "inspect"],
    queryFn: async () => (await staffDataApi.tasks()).data,
    enabled: isLoggedIn && tab === "inspect",
  });

  const pendingInspectTasks = useMemo<StaffTask[]>(() => {
    if (!inspectTasks) return [];
    return inspectTasks.filter(
      (t) => t.task_type === "cleaning" && t.review_status === "pending_review"
    );
  }, [inspectTasks]);

  const maintenanceReminders = useMemo<StaffTask[]>(() => {
    if (!pendingTasks) return [];
    return pendingTasks.filter((t) => t.title.startsWith("[维护提醒]"));
  }, [pendingTasks]);

  const todayKey = today;

  const roomsToday = useMemo(() => {
    if (!calendar) return [];
    return calendar.map((r) => {
      const todayBooking = r.days?.[todayKey];
      const displayStatus = todayBooking
        ? todayBooking.status.startsWith("block:")
          ? "maintenance"
          : "occupied"
        : r.room_status;
      return { ...r, displayStatus, todayBooking };
    });
  }, [calendar, todayKey]);

  if (!isLoggedIn) return null;

  return (
    <div>
      <div
        style={{
          padding: "32px 20px 24px",
          background: "#2B2721",
          color: "#FBF8F1",
        }}
      >
        <div style={{ fontSize: 12, color: "#A89680", marginBottom: 6 }}>
          管家 · {user?.display_name || "员工"}
        </div>
        <div className="serif" style={{ fontSize: 22, fontWeight: 400, letterSpacing: 0 }}>
          {now.format("M月D日")}
        </div>
        <div style={{ fontSize: 12, color: "#A89680", marginTop: 4 }}>日运营视图</div>
      </div>

      <div style={{ padding: "12px 12px 0" }}>
        <Segmented
          block
          size="large"
          value={tab}
          onChange={(v) => setTab(v as "today" | "rooms" | "inspect")}
          options={[
            { value: "today", label: "今日入退" },
            { value: "inspect", label: "待查房" },
            { value: "rooms", label: "房态" },
          ]}
        />
      </div>

      {tab === "inspect" ? (
        <div style={{ padding: 12 }}>
          <div style={{ fontSize: 13, color: "#5C5547", marginBottom: 12, padding: "0 4px" }}>
            保洁员提交完工后,需要管家或运营查房确认。
            <span style={{ color: "#7B8578" }}>查房通过</span>后房间将自动恢复为
            <span style={{ color: "#7B8578" }}>「可入住」</span>状态。
          </div>
          {l4 ? (
            <Skeleton active />
          ) : pendingInspectTasks.length === 0 ? (
            <Empty description="暂无待查房任务" />
          ) : (
            pendingInspectTasks.map((t) => (
              <InspectCard
                key={t.task_id}
                task={t}
                onApprove={() => reviewMutation.mutate({ taskId: t.task_id, approved: true })}
                onReject={() => {
                  const reason = window.prompt("请输入驳回原因(将退回保洁重做)", "");
                  if (reason !== null) {
                    reviewMutation.mutate({ taskId: t.task_id, approved: false, reason: reason || undefined });
                  }
                }}
                pending={reviewMutation.isPending}
              />
            ))
          )}
        </div>
      ) : tab === "today" ? (
        <div style={{ padding: 12 }}>
          {maintenanceReminders.length > 0 && (
            <div style={{ marginBottom: 20 }}>
              <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 8, paddingLeft: 4, color: "#8A6E5A" }}>
                维护提醒 ({maintenanceReminders.length})
              </div>
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                {maintenanceReminders.map((t) => {
                  const daysMatch = t.title.match(/(\d+)\s*天/);
                  const days = daysMatch ? daysMatch[1] : "?";
                  return (
                    <div
                      key={t.task_id}
                      onClick={() => {
                        setTab("rooms");
                        router.push("/staff/keeper?tab=rooms");
                      }}
                      className="card-hoverable"
                      style={{
                        background: "#F0E4D6",
                        border: "0.5px solid #8A6E5A",
                        borderRadius: 10,
                        padding: "10px 12px",
                        minWidth: 84,
                        textAlign: "center",
                        cursor: "pointer",
                      }}
                    >
                      <div className="serif" style={{ fontSize: 17, color: "#8A6E5A", letterSpacing: 0 }}>
                        {t.room_id || "—"}
                      </div>
                      <div style={{ fontSize: 10, color: "#5C5547", marginTop: 2 }}>
                        闲置 {days} 天
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          {/* 今日入住 */}
          <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 8, paddingLeft: 4 }}>
            今日入住 {checkIns ? `(${checkIns.items.length})` : ""}
          </div>
          {l1 ? (
            <Skeleton active />
          ) : !checkIns || checkIns.items.length === 0 ? (
            <Empty description="今日无入住" style={{ marginBottom: 20 }} />
          ) : (
            checkIns.items.map((o) => (
              <OrderCard
                key={o.order_id}
                order={o}
                kind="in"
                onClick={() => openAction(o, "in")}
              />
            ))
          )}

          {/* 今日退房 */}
          <div style={{ fontSize: 14, fontWeight: 600, margin: "20px 4px 8px" }}>
            今日退房 {checkOuts ? `(${checkOuts.items.length})` : ""}
          </div>
          {l2 ? (
            <Skeleton active />
          ) : !checkOuts || checkOuts.items.length === 0 ? (
            <Empty description="今日无退房" />
          ) : (
            checkOuts.items.map((o) => (
              <OrderCard
                key={o.order_id}
                order={o}
                kind="out"
                onClick={() => openAction(o, "out")}
              />
            ))
          )}
        </div>
      ) : (
        <div style={{ padding: 12 }}>
          {l3 ? (
            <Skeleton active />
          ) : !calendar || calendar.length === 0 ? (
            <Empty description="暂无房态" />
          ) : (
            <>
              <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginBottom: 12, paddingLeft: 4, fontSize: 11 }}>
                {Object.entries(ROOM_STATUS_LABEL).map(([k, v]) => (
                  <span key={k} style={{ display: "flex", alignItems: "center", gap: 4 }}>
                    <span
                      style={{
                        display: "inline-block",
                        width: 10,
                        height: 10,
                        borderRadius: "50%",
                        background: ROOM_STATUS_COLOR[k],
                      }}
                    />
                    {v}
                  </span>
                ))}
              </div>
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "repeat(3, 1fr)",
                  gap: 8,
                }}
              >
                {roomsToday.map((r: any) => (
                  <div
                    key={r.room_id}
                    style={{
                      background: "#F5F1EA",
                      border: `0.5px solid ${ROOM_STATUS_COLOR[r.displayStatus] || "#E5DDCB"}`,
                      borderRadius: 10,
                      padding: 10,
                      textAlign: "center",
                    }}
                  >
                    <div className="serif" style={{ fontSize: 17, fontWeight: 400, color: ROOM_STATUS_COLOR[r.displayStatus], letterSpacing: 0 }}>
                      {r.room_id}
                    </div>
                    <div style={{ fontSize: 10, color: "#5C5547", marginTop: 2 }}>
                      {r.room_name}
                    </div>
                    <div style={{ fontSize: 10, color: "#A89680", marginTop: 4 }}>
                      {ROOM_STATUS_LABEL[r.displayStatus] || r.displayStatus}
                    </div>
                    {r.todayBooking?.guest_name && (
                      <div style={{ fontSize: 10, color: "#2B2721", marginTop: 2 }}>
                        {r.todayBooking.guest_name}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </>
          )}
        </div>
      )}

      <Drawer
        open={!!actionOrder}
        onClose={closeAction}
        placement="bottom"
        height="auto"
        title={
          actionOrder
            ? actionOrder.kind === "in"
              ? `办理入住 · 房间 ${actionOrder.order.room_id}`
              : `办理退房 · 房间 ${actionOrder.order.room_id}`
            : ""
        }
      >
        {actionOrder && (
          <div style={{ paddingBottom: 8 }}>
            <div style={{ background: "#f5f5f7", borderRadius: 10, padding: 12, marginBottom: 16 }}>
              <div style={{ fontSize: 14, fontWeight: 600 }}>{actionOrder.order.guest_name}</div>
              <div style={{ fontSize: 12, color: "#888", marginTop: 4 }}>
                {actionOrder.order.guest_phone}
                <span style={{ margin: "0 6px", color: "#ddd" }}>·</span>
                {dayjs(actionOrder.order.check_in_date).format("M/D")} →{" "}
                {dayjs(actionOrder.order.check_out_date).format("M/D")}
                <span style={{ margin: "0 6px", color: "#ddd" }}>·</span>
                {actionOrder.order.nights} 晚
              </div>
            </div>

            {/* 办理入住：收押金提示（押金 > 0 且未收时） */}
            {actionOrder.kind === "in" &&
              Number(actionOrder.order.deposit ?? 0) > 0 &&
              actionOrder.order.deposit_status === "not_collected" && (
                <div
                  style={{
                    background: "#E8ECE4",
                    borderRadius: 10,
                    padding: 12,
                    marginBottom: 16,
                    fontSize: 14,
                  }}
                >
                  办理入住时收取押金{" "}
                  <span style={{ fontWeight: 600 }}>
                    ¥{Number(actionOrder.order.deposit ?? 0).toLocaleString()}
                  </span>
                </div>
              )}

            {/* 办理退房：押金结算（已收押金且 > 0 时），默认全退、可少退扣款 */}
            {actionOrder.kind === "out" &&
              actionOrder.order.deposit_status === "collected" &&
              Number(actionOrder.order.deposit ?? 0) > 0 &&
              (() => {
                const dep = Number(actionOrder.order.deposit ?? 0);
                const withhold = Math.max(0, dep - (refundAmount || 0));
                return (
                  <div
                    style={{
                      background: "#F5F1EA",
                      borderRadius: 10,
                      padding: 12,
                      marginBottom: 14,
                    }}
                  >
                    <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 8 }}>
                      退还押金（收取 ¥{dep.toLocaleString()}）
                    </div>
                    <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                      <span style={{ fontSize: 13, color: "#666" }}>实退</span>
                      <InputNumber
                        min={0}
                        max={dep}
                        value={refundAmount}
                        onChange={(v) => setRefundAmount(Number(v ?? 0))}
                        prefix="¥"
                        size="large"
                        style={{ width: 150 }}
                      />
                      {withhold > 0 && (
                        <span style={{ fontSize: 12, color: "#8A6E5A" }}>
                          扣款 ¥{withhold.toLocaleString()}
                        </span>
                      )}
                    </div>
                    {withhold > 0 && (
                      <Input
                        placeholder="扣款原因（如：损坏房间物品）"
                        value={withholdReason}
                        onChange={(e) => setWithholdReason(e.target.value)}
                        style={{ marginTop: 8 }}
                        size="large"
                      />
                    )}
                  </div>
                );
              })()}

            {actionOrder.kind === "out" && (
              <div style={{ marginBottom: 14 }}>
                <div style={{ fontSize: 13, color: "#666", marginBottom: 6 }}>
                  派给保洁（可留空＝暂不派单）
                </div>
                <Select
                  allowClear
                  value={pickedCleanerId}
                  onChange={(v) => setPickedCleanerId(v)}
                  placeholder="选择保洁员，或留空稍后再派"
                  style={{ width: "100%" }}
                  size="large"
                  options={
                    cleaners?.map((c: CleanerBrief) => ({
                      value: c.user_id,
                      label: `${c.display_name}${c.phone ? ` · ${c.phone}` : ""}`,
                    })) || []
                  }
                />
                {(!cleaners || cleaners.length === 0) && (
                  <div style={{ fontSize: 11, color: "#ff4d4f", marginTop: 4 }}>
                    还没有保洁员,请管理员先在后台创建保洁账号
                  </div>
                )}
              </div>
            )}

            <div style={{ marginBottom: 20 }}>
              <div style={{ fontSize: 13, color: "#666", marginBottom: 6 }}>
                备注 <span style={{ color: "#bbb" }}>(选填)</span>
              </div>
              <Input.TextArea
                value={actionNotes}
                onChange={(e) => setActionNotes(e.target.value)}
                rows={2}
                maxLength={200}
                placeholder={
                  actionOrder.kind === "in"
                    ? "如:已核验身份、已告知房间密码"
                    : "如:物品完好、水电已关"
                }
              />
            </div>

            <Button
              type="primary"
              size="large"
              block
              loading={checkinMutation.isPending || checkoutMutation.isPending}
              onClick={() => {
                if (actionOrder.kind === "in") {
                  checkinMutation.mutate(actionOrder.order.order_id);
                } else {
                  const dep = Number(actionOrder.order.deposit ?? 0);
                  if (actionOrder.order.deposit_status === "collected" && dep > 0) {
                    if (refundAmount < 0 || refundAmount > dep) {
                      message.warning(`实退金额需在 0 ~ ${dep} 之间`);
                      return;
                    }
                    if (refundAmount < dep && !withholdReason.trim()) {
                      message.warning("实退少于押金时必须填写扣款原因");
                      return;
                    }
                  }
                  checkoutMutation.mutate(actionOrder.order.order_id);
                }
              }}
            >
              {actionOrder.kind === "in" ? "确认办理入住" : "确认办理退房并派清扫"}
            </Button>
          </div>
        )}
      </Drawer>
    </div>
  );
}

function InspectCard({
  task,
  onApprove,
  onReject,
  pending,
}: {
  task: StaffTask;
  onApprove: () => void;
  onReject: () => void;
  pending: boolean;
}) {
  return (
    <div
      style={{
        background: "#F5F1EA",
        border: "0.5px solid #E5DDCB",
        borderRadius: 10,
        padding: 12,
        marginBottom: 8,
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
        <div style={{ fontSize: 15, fontWeight: 600 }}>
          房间 {task.room_id || "—"}
        </div>
        <Tag color="orange" style={{ margin: 0 }}>待查房</Tag>
      </div>
      <div style={{ fontSize: 12, color: "#5C5547", marginBottom: 4 }}>
        {task.title}
      </div>
      {task.notes && (
        <div style={{ fontSize: 12, color: "#888", marginBottom: 6 }}>
          保洁备注: {task.notes}
        </div>
      )}
      <div style={{ fontSize: 11, color: "#A89680", marginBottom: 10 }}>
        提交于 {task.submitted_at ? dayjs(task.submitted_at).format("M/D HH:mm") : "—"}
      </div>
      <div style={{ display: "flex", gap: 8 }}>
        <Button danger size="middle" disabled={pending} onClick={onReject} style={{ flex: 1 }}>
          驳回重做
        </Button>
        <Button type="primary" size="middle" loading={pending} onClick={onApprove} style={{ flex: 2 }}>
          查房通过
        </Button>
      </div>
    </div>
  );
}

function OrderCard({
  order, kind, onClick,
}: { order: any; kind: "in" | "out"; onClick?: () => void }) {
  return (
    <div
      onClick={onClick}
      className="card-hoverable"
      style={{
        background: "#F5F1EA",
        border: "0.5px solid #E5DDCB",
        borderRadius: 10,
        padding: 12,
        marginBottom: 8,
        cursor: onClick ? "pointer" : "default",
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div style={{ fontSize: 15, fontWeight: 600 }}>
          房间 {order.room_id || "待排房"}
        </div>
        <Tag color={kind === "in" ? "green" : "orange"} style={{ margin: 0 }}>
          {kind === "in" ? "入住" : "退房"}
        </Tag>
      </div>
      <div style={{ fontSize: 13, color: "#555", marginTop: 4 }}>
        {order.guest_name} · {order.guest_phone}
      </div>
      <div style={{ fontSize: 12, color: "#888", marginTop: 4 }}>
        {CHANNEL_LABEL[order.channel] || order.channel}
        <span style={{ margin: "0 6px", color: "#ddd" }}>·</span>
        {dayjs(order.check_in_date).format("M/D")} → {dayjs(order.check_out_date).format("M/D")}
        <span style={{ margin: "0 6px", color: "#ddd" }}>·</span>
        {order.nights} 晚
      </div>
    </div>
  );
}
