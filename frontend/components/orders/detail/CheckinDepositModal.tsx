"use client";

import React, { useEffect, useState } from "react";
import { Modal, Select, message } from "antd";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ordersApi } from "@/lib/api";
import { invalidateOrderWithAudit } from "@/lib/order-cache";
import { extractErrorMessage } from "@/lib/api-errors";
import { tokens } from "@/lib/design-tokens";

interface Props {
  open: boolean;
  order: any;
  onClose: () => void;
}

// 办理入住：流转到 checked_in（押金已下线，王总 2026-07-22，改走线下 POS + 飞书小票，不再收押金）。
// 单间入住（镜像单间退房）：多房单可只入住其中一间，其余保持待入住；也可「全部入住」一起办。
export function CheckinDepositModal({ open, order, onClose }: Props) {
  const queryClient = useQueryClient();

  // 待入住的已排房行：有房号、未入住（checked_in_at 空）、未退房。多于一间时须让前台选入住哪间，
  // 避免「一笔 OTA 团单点确认后没法分房办理入住」的老坑。值 "__all__" = 全部一起入住。
  const waitingRooms: any[] = (Array.isArray(order?.rooms) ? order.rooms : []).filter(
    (r: any) => r?.room_id && !r?.checked_in_at && !r?.checked_out_at,
  );
  const isMultiRoom = waitingRooms.length > 1;
  // 订单是否已在住（部分入住的中间态：已有房入住、订单已 checked_in，还剩房待入住）。
  // 这种单剩下的房必须走单间入住（order_room_id），整单快路径会被后端 400「已checked_in」。
  const orderAlreadyCheckedIn = order?.order_status === "checked_in";
  const [selectedRoom, setSelectedRoom] = useState<string | undefined>(undefined);
  // 传给后端的单间标识：多房单选了具体某间才传；单房 / 选「全部入住」不传单间标识。
  const targetOrderRoomId =
    isMultiRoom && selectedRoom && selectedRoom !== "__all__" ? selectedRoom : undefined;

  useEffect(() => {
    if (open) setSelectedRoom(undefined);
  }, [open]);

  const close = () => {
    setSelectedRoom(undefined);
    onClose();
  };

  const checkinMutation = useMutation({
    // 一次原子请求：流转在住 + 下门锁码同事务完成（押金已下线，collect_deposit 恒 false）。
    // 单间入住传 order_room_id 只入住这间；「全部入住」逐间顺序办（各自置房 occupied + 下码），
    // 避免整单快路径对「已部分入住」订单报 400。
    mutationFn: async () => {
      if (targetOrderRoomId) {
        await ordersApi.handleCheckin(order.order_id, false, undefined, targetOrderRoomId);
        return;
      }
      // 多房「全部入住」或订单已在住（部分入住，逐间补办剩余房）：都逐间用 order_room_id 办。
      // 关键：订单一旦 checked_in，整单快路径(不传 order_room_id→fast_checkin)会被后端 400，
      // 所以剩余房必须走单间路径，哪怕只剩一间（P1 修复：否则最后一间永远办不了入住）。
      if (isMultiRoom || orderAlreadyCheckedIn) {
        // 逐间失败时靠 onSettled 刷新缓存，重开后 waitingRooms 已是真实剩余房，重试不会撞已入住房 400。
        for (const r of waitingRooms) {
          await ordersApi.handleCheckin(order.order_id, false, undefined, r.order_room_id);
        }
        return;
      }
      // 全新单房单（订单未入住）：整单快路径（不传单间标识），保持原行为。
      await ordersApi.handleCheckin(order.order_id, false);
    },
    onSuccess: () => {
      message.success(targetOrderRoomId ? "已办理该房间入住" : "已办理入住");
      close();
    },
    onError: (e: any) => message.error(extractErrorMessage(e, "办理入住失败")),
    // 成功/失败都刷新：部分入住失败时缓存也更新，重开弹窗 waitingRooms 反映真实剩余房，
    // 重试只办真正没入住的房、不会 400（P2 修复）。
    onSettled: () => {
      invalidateOrderWithAudit(queryClient, order?.order_id);
      queryClient.invalidateQueries({ queryKey: ["rooms"] });
    },
  });

  return (
    <Modal
      open={open}
      title="办理入住"
      onCancel={close}
      onOk={() => {
        if (isMultiRoom && !selectedRoom) {
          message.warning("请选择要入住的房间（或选「全部入住」一起办）");
          return;
        }
        checkinMutation.mutate();
      }}
      okText="办理入住"
      cancelText="取消"
      confirmLoading={checkinMutation.isPending}
    >
      <div style={{ fontSize: 13, color: tokens.color.text.secondary }}>
        客人 {order?.guest_name}
        {!isMultiRoom && <> · 房间 {waitingRooms[0]?.room_id || order?.room_id || "(未排房)"}</>}
      </div>

      {/* 单间入住：多房单先选入住哪间，或选「全部入住」。这是「OTA 团单没法分房办入住」老坑的根治入口。 */}
      {isMultiRoom && (
        <div style={{ marginTop: 12, marginBottom: 4 }}>
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 6 }}>
            这是多房间订单（{waitingRooms.length} 间待入住），请选择要入住的房间
          </div>
          <Select
            style={{ width: "100%" }}
            size="large"
            placeholder="选择要办理入住的房间"
            value={selectedRoom}
            onChange={(v) => setSelectedRoom(v)}
            options={[
              ...waitingRooms.map((r: any) => ({
                value: r.order_room_id,
                label: `只入住 房间 ${r.room_id}`,
              })),
              { value: "__all__", label: `全部入住（${waitingRooms.length} 间一起办）` },
            ]}
          />
          <div style={{ marginTop: 6, fontSize: 12, color: tokens.color.text.tertiary }}>
            {targetOrderRoomId
              ? "只办这一间：置「占用」、只下这间的门锁密码；其它房间保持待入住，订单转「在住」。"
              : selectedRoom === "__all__"
                ? "所有待入住房间一起办入住、各自下门锁密码。"
                : "订单在首间入住时即转「在住」，其余房间可稍后再逐间办理。"}
          </div>
        </div>
      )}

      <div style={{ marginTop: 12, fontSize: 12, color: tokens.color.text.tertiary }}>
        确认后订单状态推到「在住」，入住的房态置「占用」，并下发门锁密码。
      </div>
    </Modal>
  );
}
