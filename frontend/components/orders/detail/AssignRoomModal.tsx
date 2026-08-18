"use client";

import React, { useEffect, useMemo, useState } from "react";
import { Modal, Select, message } from "antd";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ordersApi, roomsApi } from "@/lib/api";
import { invalidateOrderWithAudit } from "@/lib/order-cache";
import { extractErrorMessage } from "@/lib/api-errors";
import { resolveAssignCurrentRoom } from "@/lib/order-rooms";
import { tokens } from "@/lib/design-tokens";

interface Props {
  open: boolean;
  order: any;
  /** 展示用房间行（含 legacy 合成行），由父组件统一推导 */
  orderRooms: any[];
  /** Multi-room：指定排哪一行；未传则兼容旧单房入口 */
  orderRoomId?: string;
  onClose: () => void;
}

export function AssignRoomModal({ open, order, orderRooms, orderRoomId, onClose }: Props) {
  const queryClient = useQueryClient();
  const [pickedRoom, setPickedRoom] = useState<string | undefined>(undefined);

  useEffect(() => {
    if (open) setPickedRoom(undefined);
  }, [open]);

  // 换房/排房:按所选行的入住-退房日期算可订房,而非静态 room_status (#49)。
  // 静态枚举会把"今天占用但目标日期空闲"的房隐藏、把"今天空闲但目标日期已订"的房照常提供。
  const assignRow: any =
    (order?.rooms ?? []).find((x: any) => x.order_room_id === orderRoomId) ??
    (order?.rooms ?? [])[0];
  const assignCheckIn: string | undefined = assignRow?.check_in_date ?? order?.check_in_date;
  const assignCheckOut: string | undefined = assignRow?.check_out_date ?? order?.check_out_date;
  // 待排房行的「当前房」是无：不能回退顶层 room_id（那是首行的房，可能当晚已被
  // 别的订单占用，回退等于绕过可订过滤，选中必 409）。见 resolveAssignCurrentRoom。
  const assignCurrentRoomId: string | undefined = resolveAssignCurrentRoom(
    order?.rooms,
    orderRoomId,
    order?.room_id
  );

  const { data: roomList } = useQuery({
    queryKey: ["rooms"],
    queryFn: () => roomsApi.list().then((r) => r.data),
    enabled: open,
    staleTime: 30 * 1000,
  });

  const { data: assignAvail } = useQuery({
    queryKey: ["rooms", "availability", assignCheckIn, assignCheckOut],
    queryFn: () => roomsApi.availabilityList(assignCheckIn!, assignCheckOut!).then((r) => r.data),
    enabled: open && !!assignCheckIn && !!assignCheckOut,
    staleTime: 30 * 1000,
  });
  const assignAvailableSet = useMemo(
    () => new Set(assignAvail?.available_room_ids ?? []),
    [assignAvail]
  );

  // Multi-room: 用专用 /assign-room endpoint（支持 order_room_id 指定排哪一行）。
  // 之前走 ordersApi.update({room_id}) 是单房遗留路径，多房语境下会整体替换 rooms 导致丢行。
  const assignMutation = useMutation({
    mutationFn: ({ roomId }: { roomId: string }) =>
      ordersApi.assignRoom(order.order_id, roomId, orderRoomId),
    onSuccess: () => {
      message.success(assignCurrentRoomId ? "已换房" : "已排房");
      invalidateOrderWithAudit(queryClient, order?.order_id);
      setPickedRoom(undefined);
      onClose();
    },
    onError: (e: any) => message.error(extractErrorMessage(e, "操作失败")),
  });

  return (
    <Modal
      open={open}
      title={assignCurrentRoomId ? "换房" : "排房"}
      onCancel={onClose}
      onOk={() => {
        if (!pickedRoom) {
          message.warning("请选择房间");
          return;
        }
        assignMutation.mutate({ roomId: pickedRoom });
      }}
      okText="确认"
      cancelText="取消"
      confirmLoading={assignMutation.isPending}
    >
      <div style={{ marginBottom: 8, fontSize: 13, color: tokens.color.text.secondary }}>
        {(() => {
          // 显示选中的那一行的日期；如未选中（兼容旧单房入口）则用 order 顶层
          const r = orderRooms.find((x) => x.order_room_id === orderRoomId) ?? orderRooms[0];
          const ci = r?.check_in_date ?? order?.check_in_date;
          const co = r?.check_out_date ?? order?.check_out_date;
          const n = r?.nights ?? order?.nights;
          return <>入住 {ci} → 退房 {co}（{n} 晚）</>;
        })()}
      </div>
      <Select
        showSearch
        placeholder="选择房间（按该日期段可订）"
        style={{ width: "100%" }}
        size="large"
        value={pickedRoom}
        optionFilterProp="label"
        onChange={setPickedRoom}
        options={(roomList as any[] || [])
          // 按所选行日期可订的房 + 当前已排的这间房(允许保持不变)
          .filter(
            (r: any) =>
              assignAvailableSet.has(r.room_id) || r.room_id === assignCurrentRoomId
          )
          .map((r: any) => ({
            value: r.room_id,
            label: `${r.room_id} · ${r.room_name}${r.room_id === assignCurrentRoomId ? " (当前)" : ""}`,
          }))}
      />
      <div style={{ marginTop: 12, fontSize: 12, color: tokens.color.text.tertiary }}>
        仅显示所选入住-退房日期段内可订的房间;确认时后端会再次校验冲突。
      </div>
    </Modal>
  );
}
