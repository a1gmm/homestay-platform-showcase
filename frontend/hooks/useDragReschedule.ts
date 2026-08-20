"use client";

import React, { useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Button, message, notification } from "antd";
import { ordersApi } from "@/lib/api";
import { invalidateOrderRelated } from "@/lib/order-cache";
import { extractErrorMessage } from "@/lib/api-errors";

// issue#8: 拖拽源类型 — pending 卡片 vs 已排房格子，决定 drop 行为分支
export type DragSource = "pending" | "gantt";

// 撤销快照：上一次拖拽换房/换日期前的状态，用于 5 秒撤销
// Multi-room: beforeRooms 记录完整 rooms[] 用于多房订单准确还原
export interface DragSnapshot {
  orderId: string;
  before: { room_id: string | null; check_in_date: string; check_out_date: string };
  beforeRooms?: Array<{
    room_id: string | null;
    check_in_date: string;
    check_out_date: string;
    list_price?: number | null;
    actual_price?: number | null;
    guests_count?: number;
    position?: number;
  }>;
}

// 甘特拖拽排房/换房/改期的数据层：拖拽视觉 state + 撤销快照 + 三个 mutation
// （排房 / 改期 / 对调）。drop 分发的 UI 交互仍在 page（依赖日历数据与 Modal.confirm）。
// contextHolder 需由调用方渲染（撤销 toast 走独立 notification 实例）。
export function useDragReschedule() {
  const qc = useQueryClient();
  const [api, contextHolder] = notification.useNotification();

  // dragging order id 用于 GanttView cell 视觉反馈
  const [draggingOrderId, setDraggingOrderId] = useState<string | null>(null);
  const [dragSource, setDragSource] = useState<DragSource | null>(null);
  // 原生 dragover 可能紧跟 dragstart 触发，早于 React state 重渲染。
  // 用 ref 同步记录会话，避免首个 dragover 未 preventDefault、浏览器直接吞掉 drop。
  const dragSessionRef = useRef<{ orderId: string; source: DragSource } | null>(null);
  const [lastDragSnapshot, setLastDragSnapshot] = useState<DragSnapshot | null>(null);

  const startDrag = (orderId: string, source: DragSource) => {
    dragSessionRef.current = { orderId, source };
    setDraggingOrderId(orderId);
    setDragSource(source);
  };

  const clearDrag = () => {
    dragSessionRef.current = null;
    setDraggingOrderId(null);
    setDragSource(null);
  };

  const canDropOnCell = (occupied: boolean) => {
    const session = dragSessionRef.current;
    return Boolean(session && !(session.source === "pending" && occupied));
  };

  const assignRoomMutation = useMutation({
    mutationFn: ({ orderId, roomId }: { orderId: string; roomId: string }) =>
      ordersApi.assignRoom(orderId, roomId).then((r) => r.data),
    onSuccess: (data) => {
      message.success(`已排房至 ${data.room_id}`);
      invalidateOrderRelated(qc);
      qc.invalidateQueries({ queryKey: ["orders", "pending-room"] });
      qc.invalidateQueries({ queryKey: ["rooms", "calendar"] });
    },
    onError: (e: unknown) => {
      const err = e as { response?: { data?: { detail?: string } } };
      message.error(extractErrorMessage(err, "排房失败"));
    },
  });

  // issue#8: 已排房订单拖拽换房/换日期 mutation
  const dragRescheduleMutation = useMutation({
    mutationFn: ({
      orderId,
      payload,
    }: {
      orderId: string;
      payload: {
        room_id?: string | null;
        check_in_date?: string;
        check_out_date?: string;
        rooms?: Array<Record<string, unknown>>;
        // 用户在确认弹窗点头后置 true，放行后端「入住日期不能早于今天」守卫
        allow_past_dates?: boolean;
      };
    }) => ordersApi.update(orderId, payload as never).then((r) => r.data),
    onSuccess: (_data, vars) => {
      invalidateOrderRelated(qc);
      qc.invalidateQueries({ queryKey: ["rooms", "calendar"] });
      // 撤销 toast：5 秒内可点恢复
      if (lastDragSnapshot && lastDragSnapshot.orderId === vars.orderId) {
        const snap = lastDragSnapshot;
        api.success({
          key: `drag-${vars.orderId}-${Date.now()}`,
          message: `订单 ${vars.orderId} 已迁移`,
          description: `房号 / 日期已更新。如需恢复请在 5 秒内点击撤销。`,
          duration: 5,
          btn: React.createElement(
            Button,
            {
              type: "primary",
              size: "small",
              onClick: () => {
                if (snap.before.room_id) {
                  dragRescheduleMutation.mutate({
                    orderId: snap.orderId,
                    // Multi-room: 优先用 beforeRooms 完整还原（多房订单准确恢复每一行）
                    payload: snap.beforeRooms
                      ? { rooms: snap.beforeRooms }
                      : {
                          room_id: snap.before.room_id,
                          check_in_date: snap.before.check_in_date,
                          check_out_date: snap.before.check_out_date,
                        },
                  });
                }
                setLastDragSnapshot(null);
              },
            },
            "撤销"
          ),
        });
      } else {
        message.success("订单已更新");
      }
    },
    onError: (e: unknown) => {
      const err = e as { response?: { data?: { detail?: string } } };
      message.error(extractErrorMessage(err, "迁移失败"));
    },
  });

  const swapMutation = useMutation({
    mutationFn: (body: {
      order_a_id: string;
      order_room_a_id: string;
      order_b_id: string;
      order_room_b_id: string;
    }) => ordersApi.swapRooms(body).then((r) => r.data),
    onSuccess: () => {
      invalidateOrderRelated(qc);
      qc.invalidateQueries({ queryKey: ["rooms", "calendar"] });
      message.success("两个订单已对调房间");
    },
    onError: (e: unknown) => {
      const err = e as { response?: { data?: { detail?: string } } };
      message.error(extractErrorMessage(err, "对调失败"));
    },
  });

  return {
    contextHolder,
    draggingOrderId,
    dragSource,
    startDrag,
    clearDrag,
    canDropOnCell,
    lastDragSnapshot, setLastDragSnapshot,
    assignRoomMutation,
    dragRescheduleMutation,
    swapMutation,
  };
}
