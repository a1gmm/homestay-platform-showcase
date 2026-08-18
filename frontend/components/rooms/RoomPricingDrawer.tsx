"use client";

import React from "react";
import { Drawer, Skeleton } from "antd";
import { useQuery } from "@tanstack/react-query";
import { roomsApi } from "@/lib/api";
import { tokens } from "@/lib/design-tokens";
import type { RoomPricingPoint } from "@/lib/types";

interface Props {
  // 打开哪个房间的 7 日定价；null 关闭
  roomId: string | null;
  onClose: () => void;
  isMobile: boolean;
}

// 7 日定价 Drawer — 甘特图行 hover 菜单触发，纯展示。自持查询。
export function RoomPricingDrawer({ roomId, onClose, isMobile }: Props) {
  const { data, isLoading } = useQuery<RoomPricingPoint[]>({
    queryKey: ["room-pricing", roomId, "7d"],
    queryFn: () => roomsApi.pricing(roomId!, 7).then((r) => r.data),
    enabled: !!roomId,
    staleTime: 5 * 60 * 1000,
  });

  return (
    <Drawer
      title={`7 日定价 · ${roomId ?? ""}`}
      open={!!roomId}
      onClose={onClose}
      width={isMobile ? "100%" : 420}
      destroyOnHidden
    >
      {isLoading ? (
        <Skeleton active paragraph={{ rows: 5 }} />
      ) : (data?.length ?? 0) === 0 ? (
        <div style={{ padding: 24, textAlign: "center", color: tokens.color.text.tertiary }}>
          暂无定价数据。可在主页面点「刷新定价」批量触发计算。
        </div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {data!.map((p) => {
            const price =
              p.recommended_price != null
                ? Number(p.recommended_price)
                : p.base_price != null
                ? Number(p.base_price)
                : null;
            return (
              <div
                key={String(p.date)}
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  padding: "10px 12px",
                  background: tokens.color.bg.subtle,
                  borderRadius: tokens.radius.md,
                }}
              >
                <div>
                  <div style={{ fontSize: 13, fontWeight: 600 }}>{String(p.date)}</div>
                  {p.source && (
                    <div style={{ fontSize: 11, color: tokens.color.text.tertiary }}>
                      {p.source}
                    </div>
                  )}
                </div>
                <div className="tabular" style={{ fontSize: 16, fontWeight: 600, color: tokens.color.brand.primary }}>
                  {price != null ? `¥${price.toLocaleString()}` : "—"}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </Drawer>
  );
}
