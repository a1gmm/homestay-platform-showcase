"use client";

import { useState, useEffect, Suspense } from "react";
import { extractErrorMessage } from "@/lib/api-errors";
import { useRouter, useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { Button, Input, message, Skeleton, Spin } from "antd";
import { LeftOutlined } from "@ant-design/icons";
import dayjs from "dayjs";
import { bookingApi } from "@/lib/customer-api";
import { useCustomerStore } from "@/lib/customer-store";

export default function NewBookingPage() {
  return (
    <Suspense
      fallback={
        <div style={{ minHeight: "100dvh", display: "flex", alignItems: "center", justifyContent: "center" }}>
          <Spin />
        </div>
      }
    >
      <NewBookingInner />
    </Suspense>
  );
}

function NewBookingInner() {
  const router = useRouter();
  const search = useSearchParams();
  const customer = useCustomerStore((s) => s.customer);
  const isLoggedIn = useCustomerStore((s) => s.isLoggedIn());

  const roomId = search.get("room_id") || "";
  const checkIn = search.get("in") || "";
  const checkOut = search.get("out") || "";
  const totalParam = search.get("total");

  const [guestName, setGuestName] = useState("");
  const [idNumber, setIdNumber] = useState("");
  const [notes, setNotes] = useState("");
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (!isLoggedIn) {
      const here = encodeURIComponent(
        `/booking/new?room_id=${roomId}&in=${checkIn}&out=${checkOut}&total=${totalParam ?? ""}`
      );
      router.replace(`/booking/login?next=${here}`);
    }
  }, [isLoggedIn, roomId, checkIn, checkOut, totalParam, router]);

  useEffect(() => {
    if (customer?.name && !guestName) setGuestName(customer.name);
    if (customer?.id_number && !idNumber) setIdNumber(customer.id_number);
  }, [customer, guestName, idNumber]);

  const { data: room, isLoading } = useQuery({
    queryKey: ["booking", "room", roomId],
    queryFn: async () => (await bookingApi.getRoom(roomId)).data,
    enabled: !!roomId,
  });

  const nights = checkIn && checkOut ? dayjs(checkOut).diff(dayjs(checkIn), "day") : 0;

  const submit = async () => {
    if (!guestName.trim()) {
      message.error("请填写入住人姓名");
      return;
    }
    if (idNumber && !/^[0-9a-zA-Z]{6,30}$/.test(idNumber)) {
      message.error("身份证号格式不正确");
      return;
    }
    setSubmitting(true);
    try {
      const { data } = await bookingApi.createOrder({
        room_id: roomId,
        check_in_date: checkIn,
        check_out_date: checkOut,
        guest_name: guestName.trim(),
        id_number: idNumber.trim() || undefined,
        notes: notes.trim() || undefined,
      });
      message.success("预订成功，等待确认");
      router.replace(`/booking/orders?highlight=${data.order_id}`);
    } catch (e: any) {
      message.error(extractErrorMessage(e, "预订失败"));
    } finally {
      setSubmitting(false);
    }
  };

  if (!isLoggedIn) return null;

  return (
    <div style={{ paddingBottom: 120 }}>
      <div
        style={{
          padding: "16px 16px 12px",
          background: "#fff",
          borderBottom: "1px solid #eee",
          display: "flex",
          alignItems: "center",
          gap: 8,
        }}
      >
        <button
          onClick={() => router.back()}
          style={{
            background: "transparent",
            border: "none",
            fontSize: 18,
            color: "#333",
            padding: 8,
            marginLeft: -8,
            cursor: "pointer",
          }}
        >
          <LeftOutlined />
        </button>
        <div style={{ fontSize: 17, fontWeight: 600 }}>确认订单</div>
      </div>

      {isLoading || !room ? (
        <div style={{ padding: 20 }}>
          <Skeleton active />
        </div>
      ) : (
        <>
          {/* Summary */}
          <div style={{ background: "#fff", margin: "12px", padding: 16, borderRadius: 12 }}>
            <div style={{ fontSize: 16, fontWeight: 600, marginBottom: 8 }}>{room.room_name}</div>
            <div style={{ fontSize: 13, color: "#888" }}>
              {[room.city, room.district, room.community_name].filter(Boolean).join(" · ")}
              {room.floor ? ` · ${room.floor}F` : ""}
            </div>
            <div style={{ marginTop: 12, paddingTop: 12, borderTop: "1px solid #f0f0f0" }}>
              <Row label="入住" value={dayjs(checkIn).format("YYYY-MM-DD ddd")} />
              <Row label="退房" value={dayjs(checkOut).format("YYYY-MM-DD ddd")} />
              <Row label="天数" value={`${nights} 晚`} />
              {totalParam && (
                <Row
                  label="合计"
                  value={<span style={{ color: "#ff4d4f", fontWeight: 700 }}>¥{Number(totalParam).toFixed(2)}</span>}
                />
              )}
            </div>
          </div>

          {/* Form */}
          <div style={{ background: "#fff", margin: "12px", padding: 16, borderRadius: 12 }}>
            <div style={{ fontSize: 15, fontWeight: 600, marginBottom: 12 }}>入住人信息</div>

            <div style={{ marginBottom: 14 }}>
              <div style={{ fontSize: 13, color: "#666", marginBottom: 6 }}>姓名 *</div>
              <Input
                size="large"
                placeholder="入住人姓名"
                value={guestName}
                maxLength={20}
                onChange={(e) => setGuestName(e.target.value)}
              />
            </div>

            <div style={{ marginBottom: 14 }}>
              <div style={{ fontSize: 13, color: "#666", marginBottom: 6 }}>
                身份证号 <span style={{ color: "#bbb" }}>（可选，加速入住）</span>
              </div>
              <Input
                size="large"
                placeholder="身份证号"
                value={idNumber}
                maxLength={30}
                onChange={(e) => setIdNumber(e.target.value)}
              />
            </div>

            <div style={{ marginBottom: 4 }}>
              <div style={{ fontSize: 13, color: "#666", marginBottom: 6 }}>
                备注 <span style={{ color: "#bbb" }}>（如到店时间、特殊需求）</span>
              </div>
              <Input.TextArea
                placeholder="选填"
                value={notes}
                rows={3}
                maxLength={200}
                onChange={(e) => setNotes(e.target.value)}
              />
            </div>

            <div style={{ fontSize: 12, color: "#999", marginTop: 12 }}>
              登录手机：{customer?.phone} · 订单确认后将通过短信通知您
            </div>
          </div>

          {/* Footer */}
          <div
            style={{
              position: "fixed",
              left: 0,
              right: 0,
              bottom: 0,
              background: "#fff",
              borderTop: "1px solid #eee",
              padding: "12px 16px",
              paddingBottom: "calc(12px + env(safe-area-inset-bottom))",
              display: "flex",
              alignItems: "center",
              gap: 12,
              zIndex: 50,
            }}
          >
            <div style={{ flex: 1 }}>
              <div style={{ fontSize: 12, color: "#999" }}>{nights} 晚 · 提交后等待确认</div>
              {totalParam && (
                <div style={{ color: "#ff4d4f", fontWeight: 700, fontSize: 18 }}>
                  ¥{Number(totalParam).toFixed(2)}
                </div>
              )}
            </div>
            <Button type="primary" size="large" loading={submitting} onClick={submit}>
              确认预订
            </Button>
          </div>
        </>
      )}
    </div>
  );
}

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div
      style={{ display: "flex", justifyContent: "space-between", padding: "6px 0", fontSize: 14 }}
    >
      <span style={{ color: "#888" }}>{label}</span>
      <span>{value}</span>
    </div>
  );
}
