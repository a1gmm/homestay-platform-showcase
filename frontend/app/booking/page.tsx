"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { DatePicker, Empty, Skeleton, Tag } from "antd";
import dayjs, { Dayjs } from "dayjs";
import { bookingApi } from "@/lib/customer-api";

export default function BookingHome() {
  const router = useRouter();
  const [range, setRange] = useState<[Dayjs, Dayjs] | null>(() => {
    const today = dayjs().startOf("day");
    return [today, today.add(1, "day")];
  });

  const checkIn = range?.[0]?.format("YYYY-MM-DD");
  const checkOut = range?.[1]?.format("YYYY-MM-DD");

  const { data: rooms, isLoading } = useQuery({
    queryKey: ["booking", "rooms", checkIn, checkOut],
    queryFn: async () => {
      const res = await bookingApi.listRooms({ check_in: checkIn, check_out: checkOut });
      return res.data;
    },
  });

  const nights = range ? range[1].diff(range[0], "day") : 0;

  return (
    <div>
      {/* Header */}
      <div
        style={{
          padding: "28px 16px 24px",
          background: "#2B2721",
          color: "#FBF8F1",
        }}
      >
        <div className="serif" style={{ fontSize: 26, fontWeight: 400, marginBottom: 6, letterSpacing: 0 }}>
          成家民宿
        </div>
        <div style={{ fontSize: 12, color: "#A89680", lineHeight: 1.8 }}>精选寓所 · 各自有声 · 可订即住</div>
      </div>

      {/* Date picker */}
      <div
        style={{
          margin: "16px 12px",
          background: "#F5F1EA",
          border: "0.5px solid #E5DDCB",
          borderRadius: 12,
          padding: 16,
        }}
      >
        <div style={{ fontSize: 14, color: "#666", marginBottom: 8 }}>入住 / 退房日期</div>
        <DatePicker.RangePicker
          value={range as any}
          onChange={(v) => setRange(v as [Dayjs, Dayjs] | null)}
          inputReadOnly
          allowClear={false}
          disabledDate={(d) => d && d < dayjs().startOf("day")}
          style={{ width: "100%" }}
          size="large"
        />
        {nights > 0 && (
          <div style={{ fontSize: 12, color: "#999", marginTop: 8 }}>共 {nights} 晚</div>
        )}
      </div>

      {/* Room list */}
      <div style={{ padding: "0 12px" }}>
        {isLoading ? (
          <Skeleton active />
        ) : !rooms || rooms.length === 0 ? (
          <Empty description="所选日期暂无可订房源，请调整日期" style={{ marginTop: 40 }} />
        ) : (
          rooms.map((room) => (
            <div
              key={room.room_id}
              onClick={() =>
                router.push(
                  `/booking/rooms/${room.room_id}?in=${checkIn}&out=${checkOut}`
                )
              }
              className="card-hoverable"
              style={{
                background: "#F5F1EA",
                border: "0.5px solid #E5DDCB",
                borderRadius: 12,
                overflow: "hidden",
                marginBottom: 12,
                cursor: "pointer",
              }}
            >
              <div
                style={{
                  width: "100%",
                  aspectRatio: "16 / 9",
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
                      fontSize: 32,
                      fontWeight: 400,
                      letterSpacing: 2,
                      fontFamily: "var(--font-serif)",
                    }}
                  >
                    {room.room_id}
                  </div>
                )}
              </div>
              <div style={{ padding: "12px 14px 14px" }}>
                <div style={{ fontSize: 16, fontWeight: 600, marginBottom: 4 }}>
                  {room.room_name}
                </div>
                <div style={{ fontSize: 12, color: "#999", marginBottom: 8 }}>
                  {[room.city, room.district, room.community_name].filter(Boolean).join(" · ")}
                  {room.floor ? ` · ${room.floor}F` : ""}
                </div>
                <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                  {room.amenities.slice(0, 3).map((a) => (
                    <Tag key={a} color="blue">
                      {a}
                    </Tag>
                  ))}
                  <div className="serif" style={{ marginLeft: "auto", color: "#2B2721", fontWeight: 400 }}>
                    {room.base_price ? (
                      <>
                        <span style={{ fontSize: 18 }}>¥{room.base_price}</span>
                        <span style={{ fontSize: 11, color: "#7A6F5F", fontWeight: 400, fontFamily: "var(--font-sans)" }}>
                          {" / "}晚
                        </span>
                      </>
                    ) : (
                      <span style={{ fontSize: 11, color: "#7A6F5F", fontFamily: "var(--font-sans)" }}>咨询价</span>
                    )}
                  </div>
                </div>
              </div>
            </div>
          ))
        )}
        <div style={{ height: 20 }} />
      </div>
    </div>
  );
}
