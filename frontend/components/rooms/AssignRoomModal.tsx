"use client";

import React, { useState, useMemo } from "react";
import { Modal, message, Tag, Typography, Empty, Skeleton } from "antd";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { ordersApi, roomsApi, extractErrorMessage } from "@/lib/api";
import type { OrderOut, RoomOut } from "@/lib/types";
import { tokens } from "@/lib/design-tokens";
import { buildAssignableRooms } from "@/lib/assignable-rooms";
import { ROOM_STATUS_COLOR, ROOM_STATUS_LABEL } from "@/lib/status-display";

const { Text } = Typography;

interface Props {
  order: OrderOut | null;
  onClose: () => void;
  onSuccess?: () => void;
}

/**
 * 选房 Modal — 待排房/换房专用。
 *
 * 2026-07-04 改版：此前列出全部房（仅剔维修/锁房），日期冲突留给后端 409 打回——
 * 前台看不到「这几天到底哪几间能排」，选错一次弹一次错。现在按订单日期先查
 * /rooms/availability/list，只展示真正可排的房，点卡片选择、确认即排。
 * 后端 assign-room 的事务级冲突校验仍是最终防线（并发抢房时兜底）。
 *
 * 多房订单注：这里用订单级 check_in/check_out 查可用性（多房单各行日期一般一致）；
 * 行级精确排房走甘特图拖拽 / OrderDetailModal 的行内排房。
 */
export function AssignRoomModal({ order, onClose, onSuccess }: Props) {
  const qc = useQueryClient();
  const [pickedRoom, setPickedRoom] = useState<string | undefined>(undefined);

  const open = !!order;

  const { data: rooms } = useQuery({
    queryKey: ["rooms"],
    queryFn: () => roomsApi.list().then((r) => r.data),
    enabled: open,
    staleTime: 30 * 1000,
  });

  const { data: availability, isLoading: availLoading } = useQuery({
    queryKey: ["rooms", "availability", order?.check_in_date, order?.check_out_date],
    queryFn: () =>
      roomsApi
        .availabilityList(order!.check_in_date, order!.check_out_date)
        .then((r) => r.data.available_room_ids),
    enabled: open && !!order?.check_in_date && !!order?.check_out_date,
    staleTime: 15 * 1000,
  });

  const assignMutation = useMutation({
    mutationFn: (roomId: string) => ordersApi.assignRoom(order!.order_id, roomId).then((r) => r.data),
    onSuccess: () => {
      message.success(order?.room_id ? "已换房" : "已排房");
      qc.invalidateQueries({ queryKey: ["orders"] });
      qc.invalidateQueries({ queryKey: ["rooms"] });
      qc.invalidateQueries({ queryKey: ["staff", "calendar"] });
      qc.invalidateQueries({ queryKey: ["dashboard"] });
      setPickedRoom(undefined);
      onSuccess?.();
      onClose();
    },
    onError: (e) => message.error(extractErrorMessage(e)),
  });

  const candidates = useMemo(
    () => buildAssignableRooms((rooms as RoomOut[]) ?? [], availability ?? [], order?.room_id),
    [rooms, availability, order?.room_id],
  );
  const assignableCount = candidates.filter((c) => c.assignable).length;

  return (
    <Modal
      open={open}
      title={order?.room_id ? "换房" : "排房"}
      onCancel={() => {
        setPickedRoom(undefined);
        onClose();
      }}
      onOk={() => {
        if (!pickedRoom) {
          message.warning("请点选一间房");
          return;
        }
        assignMutation.mutate(pickedRoom);
      }}
      okText="确认排房"
      cancelText="取消"
      okButtonProps={{ disabled: !pickedRoom }}
      confirmLoading={assignMutation.isPending}
      destroyOnHidden
    >
      {order && (
        <>
          <div
            style={{
              padding: 12,
              background: tokens.color.bg.subtle,
              borderRadius: tokens.radius.md,
              marginBottom: 12,
              display: "flex",
              flexDirection: "column",
              gap: 4,
            }}
          >
            <div style={{ fontSize: 13 }}>
              <Text strong>{order.guest_name}</Text>
              <Text type="secondary" style={{ marginLeft: 8 }}>
                {order.guest_phone}
              </Text>
            </div>
            <div style={{ fontSize: 12, color: tokens.color.text.secondary }}>
              {order.check_in_date} → {order.check_out_date} · {order.nights} 晚
              {order.room_id && (
                <Text type="secondary" style={{ marginLeft: 8 }}>
                  当前房间: {order.room_id}
                </Text>
              )}
            </div>
          </div>

          <div style={{ fontSize: 12, color: tokens.color.text.secondary, marginBottom: 8 }}>
            {availLoading
              ? "正在查这几天的可用房…"
              : `这 ${order.nights} 晚可排 ${assignableCount} 间，点卡片选择`}
          </div>

          {availLoading ? (
            <Skeleton active paragraph={{ rows: 3 }} />
          ) : assignableCount === 0 ? (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={
                <span style={{ fontSize: 13 }}>
                  {order.check_in_date} → {order.check_out_date} 没有可排的空房
                  <br />
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    可在甘特图查看占用情况，或与客人协商调整日期
                  </Text>
                </span>
              }
            />
          ) : (
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fill, minmax(150px, 1fr))",
                gap: 8,
                maxHeight: 320,
                overflowY: "auto",
                paddingRight: 4,
              }}
            >
              {candidates.map(({ room: r, assignable, isCurrent }) => {
                const picked = pickedRoom === r.room_id;
                return (
                  <div
                    key={r.room_id}
                    role="button"
                    aria-disabled={!assignable}
                    onClick={() => assignable && setPickedRoom(picked ? undefined : r.room_id)}
                    style={{
                      border: `2px solid ${picked ? tokens.color.brand.primary : tokens.color.bg.border}`,
                      background: picked
                        ? `${tokens.color.brand.primary}14`
                        : isCurrent
                          ? tokens.color.bg.subtle
                          : tokens.color.bg.container,
                      borderRadius: tokens.radius.md,
                      padding: "10px 12px",
                      minHeight: 44,
                      cursor: assignable ? "pointer" : "not-allowed",
                      opacity: assignable ? 1 : 0.55,
                      display: "flex",
                      flexDirection: "column",
                      gap: 4,
                      transition: "border-color 120ms ease, background 120ms ease",
                    }}
                  >
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 6 }}>
                      <Text strong style={{ fontSize: 15 }}>
                        {r.room_id}
                      </Text>
                      {isCurrent ? (
                        <Tag style={{ fontSize: 11, marginInlineEnd: 0 }}>当前</Tag>
                      ) : (
                        <Tag color={ROOM_STATUS_COLOR[r.room_status]} style={{ fontSize: 11, marginInlineEnd: 0 }}>
                          {ROOM_STATUS_LABEL[r.room_status] ?? r.room_status}
                        </Tag>
                      )}
                    </div>
                    <div
                      style={{
                        fontSize: 12,
                        color: tokens.color.text.secondary,
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {r.room_name}
                    </div>
                    {r.base_price != null && (
                      <div className="tabular" style={{ fontSize: 12, color: tokens.color.text.tertiary }}>
                        ¥{Number(r.base_price).toLocaleString()}/晚
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}

          <div style={{ marginTop: 12, fontSize: 12, color: tokens.color.text.tertiary }}>
            列表已按本单日期过滤，只显示可排的房；确认时系统仍会做冲突校验兜底。
            {order.order_status === "paid_pending_room" && (
              <span style={{ display: "block", marginTop: 4 }}>
                确认后订单状态将自动推到「已排房，待入住」。
              </span>
            )}
          </div>
        </>
      )}
    </Modal>
  );
}
