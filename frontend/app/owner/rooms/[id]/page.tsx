"use client";

import { useEffect } from "react";
import { useParams, useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { Skeleton, Empty, Tag } from "antd";
import { LeftOutlined } from "@ant-design/icons";
import dayjs from "dayjs";
import { ownerDataApi, type RoomDetail } from "@/lib/owner-api";
import { useOwnerStore } from "@/lib/owner-store";
import { useIsDesktop } from "@/lib/responsive";
import { RoomImagesPanel } from "@/components/rooms/RoomImagesPanel";
import { CHANNEL_LABEL } from "@/lib/channels";
import { orderStatusTagColor } from "@/lib/status-display";

const fmt = (n: string | number | null | undefined) => {
  if (n === null || n === undefined) return "—";
  const v = typeof n === "string" ? parseFloat(n) : n;
  if (Number.isNaN(v)) return "—";
  return v.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
};

// 状态色统一走 status-display.ORDER_STATUS_TAG_COLOR；此处仅保留文案。
const STATUS_LABEL: Record<string, string> = {
  pending_confirm: "待确认",
  paid_pending_room: "待排房",
  roomed_pending_checkin: "待入住",
  checked_in: "在住",
  pending_checkout: "已退房待收款",
  pending_payment: "待完成",
  completed: "已完成",
  cancelled: "已取消",
};

export default function OwnerRoomDetailPage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const roomId = params.id;
  const isLoggedIn = useOwnerStore((s) => s.isLoggedIn());
  const owner = useOwnerStore((s) => s.owner);
  const isMaster = !!owner?.is_master;
  const isDesktop = useIsDesktop();

  useEffect(() => {
    if (!isLoggedIn) {
      router.replace(`/owner/login?next=${encodeURIComponent(`/owner/rooms/${roomId}`)}`);
    }
  }, [isLoggedIn, roomId, router]);

  const { data, isLoading } = useQuery({
    queryKey: ["owner", "roomDetail", roomId],
    queryFn: async () => (await ownerDataApi.roomDetail(roomId)).data,
    enabled: isLoggedIn,
  });

  if (!isLoggedIn) return null;

  const statCard = data ? <StatCard data={data} /> : null;
  const imagesSection = !isMaster ? (
    <div style={{ padding: 16, background: "#F5F1EA", border: "0.5px solid #E5DDCB", borderRadius: 12 }}>
      <div style={{ fontSize: 15, fontWeight: 600, color: "#2B2721", marginBottom: 10 }}>房间相册</div>
      <RoomImagesPanel
        roomId={roomId}
        queryKey={["owner-room-images", roomId]}
        api={{
          list: () => ownerDataApi.images.list(roomId),
          upload: (f) => ownerDataApi.images.upload(roomId, f),
          patch: (imageId, body) => ownerDataApi.images.patch(roomId, imageId, body),
          delete: (imageId) => ownerDataApi.images.delete(roomId, imageId),
        }}
      />
    </div>
  ) : null;
  const ordersSection = data ? <OrdersSection orders={data.orders} /> : null;

  return (
    <div style={{ paddingBottom: 20 }}>
      {/* Top bar */}
      <div
        style={{
          padding: isDesktop ? "16px 26px" : "14px 16px",
          background: "#FBF8F1",
          borderBottom: "0.5px solid #E5DDCB",
          display: "flex",
          alignItems: "center",
          gap: 8,
        }}
      >
        <button
          onClick={() => router.back()}
          aria-label="返回"
          style={{
            background: "transparent",
            border: "none",
            fontSize: 18,
            color: "#2B2721",
            width: 44,
            height: 44,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            marginLeft: -12,
            cursor: "pointer",
          }}
        >
          <LeftOutlined />
        </button>
        <div className="serif" style={{ fontSize: isDesktop ? 20 : 18, fontWeight: 400, letterSpacing: 0 }}>
          {data?.room_name || "房间详情"}
        </div>
      </div>

      {isLoading ? (
        <div style={{ padding: 20 }}>
          <Skeleton active />
        </div>
      ) : !data ? (
        <Empty description="无数据" style={{ marginTop: 40 }} />
      ) : isDesktop ? (
        // ── 桌面版：左（数据 + 订单）右（相册）双栏；master 无相册则单栏居中 ──
        <div style={{ padding: "22px 26px 12px" }}>
          {imagesSection ? (
            <div style={{ display: "grid", gridTemplateColumns: "1.4fr 1fr", gap: 20, alignItems: "start" }}>
              <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
                {statCard}
                {ordersSection}
              </div>
              <div style={{ position: "sticky", top: 22 }}>{imagesSection}</div>
            </div>
          ) : (
            <div style={{ maxWidth: 720, display: "flex", flexDirection: "column", gap: 16 }}>
              {statCard}
              {ordersSection}
            </div>
          )}
        </div>
      ) : (
        // ── 移动版：单列堆叠（沿用原有体验，不动）──────────────────────────
        <>
          <div style={{ margin: 12 }}>{statCard}</div>
          {imagesSection && <div style={{ margin: 12 }}>{imagesSection}</div>}
          <div style={{ padding: "0 12px" }}>{ordersSection}</div>
        </>
      )}
    </div>
  );
}

function StatCard({ data }: { data: RoomDetail }) {
  return (
    <div style={{ padding: 18, background: "#F5F1EA", border: "0.5px solid #E5DDCB", borderRadius: 12 }}>
      <div style={{ fontSize: 12, color: "#A89680" }}>{`${data.year} 年 ${data.month} 月`}</div>
      <div className="serif" style={{ fontSize: 30, fontWeight: 400, color: "#2B2721", marginTop: 6, letterSpacing: 0 }}>
        ¥{fmt(data.stat.net_revenue)}
      </div>
      <div style={{ fontSize: 11, color: "#A89680", marginTop: 2 }}>毛利润（已扣平台佣金）</div>

      <div
        style={{
          marginTop: 16,
          paddingTop: 14,
          borderTop: "0.5px solid #E5DDCB",
          display: "grid",
          gridTemplateColumns: "1fr 1fr",
          gap: 12,
        }}
      >
        <DetailRow label="房费流水" value={`¥${fmt(data.stat.revenue)}`} />
        <DetailRow label="订单数" value={String(data.stat.order_count)} />
        <DetailRow label="入住晚数" value={`${data.stat.occupancy_nights} 晚`} span={2} />
      </div>
    </div>
  );
}

function OrdersSection({ orders }: { orders: RoomDetail["orders"] }) {
  return (
    <div>
      <div style={{ fontSize: 15, fontWeight: 600, color: "#2B2721", margin: "0 4px 10px" }}>
        本月订单 ({orders.length})
      </div>
      {orders.length === 0 ? (
        <Empty description="本月暂无订单" style={{ marginTop: 20 }} />
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {orders.map((o) => {
            const st = { label: STATUS_LABEL[o.order_status] || o.order_status, color: orderStatusTagColor(o.order_status) };
            return (
              <div key={o.order_id} style={{ background: "#F5F1EA", border: "0.5px solid #E5DDCB", borderRadius: 10, padding: 12 }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
                    <Tag style={{ margin: 0, background: "#FBF8F1", borderColor: "#E5DDCB", color: "#5C5547" }}>
                      {CHANNEL_LABEL[o.channel] || o.channel}
                    </Tag>
                    <Tag color={st.color} style={{ margin: 0 }}>{st.label}</Tag>
                  </div>
                  <div className="serif" style={{ color: "#2B2721", fontWeight: 400, letterSpacing: 0 }}>
                    ¥{fmt(o.actual_price)}
                  </div>
                </div>
                <div style={{ fontSize: 13, marginTop: 8, color: "#2B2721" }}>{o.guest_name}</div>
                <div style={{ fontSize: 12, color: "#7A6F5F", marginTop: 4 }}>
                  {dayjs(o.check_in_date).format("M月D日")} → {dayjs(o.check_out_date).format("M月D日")}
                  <span style={{ margin: "0 6px", color: "#E5DDCB" }}>·</span>
                  {o.nights} 晚
                  {Number(o.commission_rate) > 0 && (
                    <>
                      <span style={{ margin: "0 6px", color: "#E5DDCB" }}>·</span>
                      佣金 {(Number(o.commission_rate) * 100).toFixed(1)}%
                    </>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function DetailRow({ label, value, span = 1 }: { label: string; value: string; span?: number }) {
  return (
    <div style={{ gridColumn: span === 2 ? "span 2" : "span 1" }}>
      <div style={{ fontSize: 11, color: "#A89680" }}>{label}</div>
      <div style={{ fontSize: 14, fontWeight: 500, color: "#2B2721", marginTop: 2 }}>{value}</div>
    </div>
  );
}
