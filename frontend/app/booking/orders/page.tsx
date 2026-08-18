"use client";

import { useEffect, Suspense } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { Empty, Skeleton, Spin, Tag } from "antd";
import dayjs from "dayjs";
import { customerAuthApi } from "@/lib/customer-api";
import { useCustomerStore } from "@/lib/customer-store";
import { orderStatusTagColor } from "@/lib/status-display";

// 状态色统一走 status-display.ORDER_STATUS_TAG_COLOR；此处仅保留 C 端文案
// （rescheduled 文案在各端未统一，属 #183 待拍板，先按本页原文「已改期」）。
const STATUS_LABEL: Record<string, string> = {
  pending_confirm: "待确认",
  paid_pending_room: "待排房",
  roomed_pending_checkin: "待入住",
  checked_in: "在住",
  pending_checkout: "已退房待收款",
  pending_payment: "待完成",
  completed: "已完成",
  cancelled: "已取消",
  rescheduled: "已改期",
  abnormal: "异常",
};

export default function MyOrdersPage() {
  return (
    <Suspense
      fallback={
        <div style={{ minHeight: "100dvh", display: "flex", alignItems: "center", justifyContent: "center" }}>
          <Spin />
        </div>
      }
    >
      <MyOrdersInner />
    </Suspense>
  );
}

function MyOrdersInner() {
  const router = useRouter();
  const search = useSearchParams();
  const isLoggedIn = useCustomerStore((s) => s.isLoggedIn());
  const highlight = search.get("highlight");

  useEffect(() => {
    if (!isLoggedIn) {
      router.replace(`/booking/login?next=${encodeURIComponent("/booking/orders")}`);
    }
  }, [isLoggedIn, router]);

  const { data: orders, isLoading } = useQuery({
    queryKey: ["customer", "myOrders"],
    queryFn: async () => (await customerAuthApi.myOrders()).data,
    enabled: isLoggedIn,
  });

  if (!isLoggedIn) return null;

  return (
    <div>
      <div
        style={{
          padding: "20px 16px 16px",
          background: "#fff",
          borderBottom: "1px solid #eee",
        }}
      >
        <div style={{ fontSize: 20, fontWeight: 700 }}>我的订单</div>
      </div>

      <div style={{ padding: 12 }}>
        {isLoading ? (
          <Skeleton active />
        ) : !orders || orders.length === 0 ? (
          <Empty description="暂无订单" style={{ marginTop: 60 }} />
        ) : (
          orders.map((o) => {
            const st = { label: STATUS_LABEL[o.order_status] || o.order_status, color: orderStatusTagColor(o.order_status) };
            const isNew = highlight === o.order_id;
            return (
              <div
                key={o.order_id}
                style={{
                  background: "#F5F1EA",
                  border: `0.5px solid ${isNew ? "#2B2721" : "#E5DDCB"}`,
                  borderRadius: 12,
                  padding: 14,
                  marginBottom: 10,
                }}
              >
                <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
                  <div style={{ fontSize: 14, fontWeight: 600 }}>房间 {o.room_id || "待分配"}</div>
                  <Tag color={st.color}>{st.label}</Tag>
                </div>
                <div style={{ fontSize: 13, color: "#666", marginBottom: 4 }}>
                  {dayjs(o.check_in_date).format("M月D日")} → {dayjs(o.check_out_date).format("M月D日")}
                  <span style={{ color: "#aaa", margin: "0 6px" }}>·</span>
                  {o.nights} 晚
                </div>
                <div style={{ display: "flex", justifyContent: "space-between", marginTop: 10 }}>
                  <div style={{ fontSize: 12, color: "#aaa" }}>订单号 {o.order_id}</div>
                  <div style={{ color: "#ff4d4f", fontWeight: 600 }}>
                    {o.actual_price ? `¥${Number(o.actual_price).toFixed(2)}` : "—"}
                  </div>
                </div>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
