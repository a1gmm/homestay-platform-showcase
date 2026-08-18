"use client";

import { useMemo } from "react";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { Button, Skeleton, Tag, Empty, Carousel } from "antd";
import { LeftOutlined } from "@ant-design/icons";
import dayjs from "dayjs";
import { bookingApi } from "@/lib/customer-api";

export default function RoomDetailPage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const search = useSearchParams();
  const roomId = params.id;
  const checkIn = search.get("in");
  const checkOut = search.get("out");

  const start = dayjs().startOf("day").format("YYYY-MM-DD");
  const end = dayjs().add(60, "day").format("YYYY-MM-DD");

  const { data: room, isLoading } = useQuery({
    queryKey: ["booking", "room", roomId],
    queryFn: async () => (await bookingApi.getRoom(roomId)).data,
  });

  const { data: calendar } = useQuery({
    queryKey: ["booking", "room", roomId, "calendar", start, end],
    queryFn: async () => (await bookingApi.calendar(roomId, start, end)).data,
  });

  const nights = useMemo(() => {
    if (!checkIn || !checkOut) return 0;
    return dayjs(checkOut).diff(dayjs(checkIn), "day");
  }, [checkIn, checkOut]);

  const totalPrice = useMemo(() => {
    if (!calendar || !checkIn || !checkOut) return null;
    const from = dayjs(checkIn);
    const to = dayjs(checkOut);
    let sum = 0;
    for (const d of calendar) {
      const day = dayjs(d.date);
      if ((day.isSame(from) || day.isAfter(from)) && day.isBefore(to)) {
        sum += Number(d.price || 0);
      }
    }
    return sum > 0 ? sum : null;
  }, [calendar, checkIn, checkOut]);

  if (isLoading || !room) {
    return <div style={{ padding: 20 }}><Skeleton active /></div>;
  }

  return (
    <div style={{ paddingBottom: 80 }}>
      {/* Hero: carousel if multiple images, single hero if 0/1 */}
      <div style={{ position: "relative" }}>
        <button
          onClick={() => router.back()}
          style={{
            position: "absolute",
            top: 12,
            left: 12,
            width: 36,
            height: 36,
            borderRadius: "50%",
            background: "rgba(0,0,0,0.4)",
            color: "#fff",
            border: "none",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            fontSize: 16,
            cursor: "pointer",
            zIndex: 2,
          }}
        >
          <LeftOutlined />
        </button>

        {room.images.length > 1 ? (
          <Carousel autoplay autoplaySpeed={4000} dots>
            {room.images.map((src) => (
              <div key={src}>
                <div
                  style={{
                    width: "100%",
                    aspectRatio: "16 / 10",
                    background: `url("${src}") center/cover`,
                  }}
                />
              </div>
            ))}
          </Carousel>
        ) : (
          <div
            style={{
              width: "100%",
              aspectRatio: "16 / 10",
              background: room.images[0]
                ? `url("${room.images[0]}") center/cover`
                : "linear-gradient(135deg, #E5DDCB 0%, #F5F1EA 100%)",
              position: "relative",
            }}
          >
            {!room.images[0] && (
              <div
                style={{
                  position: "absolute",
                  inset: 0,
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  color: "#A89680",
                  fontSize: 56,
                  fontWeight: 400,
                  letterSpacing: 4,
                  fontFamily: "var(--font-serif)",
                }}
              >
                {room.room_id}
              </div>
            )}
          </div>
        )}
      </div>

      {/* Content */}
      <div style={{ background: "#fff", margin: "-16px 12px 0", borderRadius: 16, padding: 16 }}>
        <div style={{ fontSize: 20, fontWeight: 600, marginBottom: 6 }}>{room.room_name}</div>
        <div style={{ fontSize: 13, color: "#888", marginBottom: 12 }}>
          {[room.province, room.city, room.district, room.community_name].filter(Boolean).join(" · ")}
          {room.floor ? ` · ${room.floor}F` : ""}
        </div>

        {room.base_price && (
          <div style={{ marginBottom: 16 }}>
            <span style={{ color: "#ff4d4f", fontSize: 24, fontWeight: 700 }}>¥{room.base_price}</span>
            <span style={{ color: "#999", fontSize: 13, marginLeft: 4 }}>/晚起</span>
            {room.min_stay_nights > 1 && (
              <Tag color="orange" style={{ marginLeft: 8 }}>
                最少住 {room.min_stay_nights} 晚
              </Tag>
            )}
          </div>
        )}

        {room.amenities.length > 0 && (
          <div style={{ marginBottom: 16 }}>
            <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 8 }}>设施</div>
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
              {room.amenities.map((a) => (
                <Tag key={a}>{a}</Tag>
              ))}
            </div>
          </div>
        )}

        {room.description && (
          <div style={{ marginBottom: 16 }}>
            <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 8 }}>简介</div>
            <div style={{ fontSize: 14, color: "#444", lineHeight: 1.7 }}>{room.description}</div>
          </div>
        )}
      </div>

      {/* Calendar (next 30 days mini) */}
      <div style={{ background: "#fff", margin: "12px 12px 0", borderRadius: 16, padding: 16 }}>
        <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 12 }}>未来 30 天房态</div>
        {calendar ? (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(7, 1fr)", gap: 4 }}>
            {calendar.slice(0, 35).map((d) => {
              const day = dayjs(d.date);
              const isWeekend = day.day() === 0 || day.day() === 6;
              return (
                <div
                  key={d.date}
                  style={{
                    textAlign: "center",
                    padding: "8px 2px",
                    borderRadius: 6,
                    background: d.available ? (isWeekend ? "#fff7e6" : "#e6f7ff") : "#f5f5f5",
                    color: d.available ? "#333" : "#bbb",
                    fontSize: 11,
                    lineHeight: 1.3,
                  }}
                >
                  <div style={{ fontWeight: 600 }}>{day.format("D")}</div>
                  <div style={{ fontSize: 10 }}>
                    {d.available ? (d.price ? `¥${Math.round(Number(d.price))}` : "可订") : "已订"}
                  </div>
                </div>
              );
            })}
          </div>
        ) : (
          <Empty description="暂无日历数据" />
        )}
      </div>

      {/* Footer: booking action */}
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
          {checkIn && checkOut && nights > 0 ? (
            <>
              <div style={{ fontSize: 12, color: "#999" }}>
                {dayjs(checkIn).format("M月D日")} – {dayjs(checkOut).format("M月D日")} · {nights} 晚
              </div>
              <div style={{ color: "#ff4d4f", fontWeight: 700, fontSize: 18 }}>
                {totalPrice != null ? `合计 ¥${totalPrice.toFixed(2)}` : "—"}
              </div>
            </>
          ) : (
            <div style={{ fontSize: 13, color: "#999" }}>请在首页选择入住日期</div>
          )}
        </div>
        <Button
          type="primary"
          size="large"
          disabled={!checkIn || !checkOut || nights < (room.min_stay_nights || 1)}
          onClick={() =>
            router.push(
              `/booking/new?room_id=${roomId}&in=${checkIn}&out=${checkOut}&total=${totalPrice ?? ""}`
            )
          }
        >
          立即预订
        </Button>
      </div>
    </div>
  );
}
