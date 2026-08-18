"use client";

import React, { useState, useMemo } from "react";
import { Modal, Select, message, Tag, Typography, Alert, DatePicker } from "antd";
import { MobileInputNumber } from "@/components/ui/MobileInputNumber";
import dayjs, { Dayjs } from "dayjs";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { ordersApi, roomsApi, extractErrorMessage } from "@/lib/api";
import type { OrderOut, RoomOut } from "@/lib/types";
import { tokens } from "@/lib/design-tokens";
import { ROOM_STATUS_COLOR, ROOM_STATUS_LABEL } from "@/lib/status-display";

const { Text } = Typography;

type Reason = "free_upgrade" | "room_defect" | "guest_request" | "other";
const REASON_OPTIONS: { value: Reason; label: string }[] = [
  { value: "free_upgrade", label: "免费升级" },
  { value: "room_defect", label: "房间故障" },
  { value: "guest_request", label: "客人要求" },
  { value: "other", label: "其他" },
];

interface Props {
  order: OrderOut | null;
  /** 要换的具体房间行（多房订单必传点击行的 order_room_id）；缺省回退到 order.room_id 对应行/首行 */
  orderRoomId?: string;
  onClose: () => void;
  onSuccess?: () => void;
}

/**
 * 换房 Modal — 免费升级 / 房间故障 / 客人要求。S1：入住前换房（整段移动）。
 * 后端 /orders/{id}/transfer-room 做事务级冲突校验 + 房态联动；门锁码 Phase 1 手动换。
 */
export function TransferRoomModal({ order, orderRoomId: orderRoomIdProp, onClose, onSuccess }: Props) {
  const qc = useQueryClient();
  const [reason, setReason] = useState<Reason>("free_upgrade");
  const [pickedRoom, setPickedRoom] = useState<string | undefined>(undefined);
  const [markup, setMarkup] = useState<number>(0);
  const [transferDate, setTransferDate] = useState<Dayjs | null>(null);

  const open = !!order;
  const isCheckedIn = order?.order_status === "checked_in";

  const reset = () => {
    setPickedRoom(undefined);
    setReason("free_upgrade");
    setMarkup(0);
    setTransferDate(null);
  };

  // 取要换的房间行：优先用调用方传入的具体行（多房订单点击行），否则匹配当前 room_id 的行/首行
  const orderRoomId = useMemo(() => {
    if (orderRoomIdProp) return orderRoomIdProp;
    const rooms = order?.rooms ?? [];
    if (rooms.length === 0) return undefined;
    return (rooms.find((r) => r.room_id === order?.room_id) ?? rooms[0]).order_room_id;
  }, [order, orderRoomIdProp]);

  // 当前要换的那一行（用于显示其原房号，多房订单不一定等于 order.room_id）
  const currentRow = useMemo(
    () => (order?.rooms ?? []).find((r) => r.order_room_id === orderRoomId),
    [order, orderRoomId],
  );
  const currentRoomId = currentRow?.room_id ?? order?.room_id;

  const { data: rooms } = useQuery({
    queryKey: ["rooms"],
    queryFn: () => roomsApi.list().then((r) => r.data),
    enabled: open,
    staleTime: 30 * 1000,
  });

  const effMarkup = reason === "room_defect" ? 0 : markup; // 故障不加价

  const pickedRoomStatus = useMemo(
    () => (rooms as RoomOut[] | undefined)?.find((r) => r.room_id === pickedRoom)?.room_status,
    [rooms, pickedRoom],
  );
  const pickedNotReady =
    pickedRoomStatus === "pending_clean" || pickedRoomStatus === "cleaning";

  const transferMutation = useMutation({
    mutationFn: () =>
      ordersApi
        .transferRoom(order!.order_id, {
          order_room_id: orderRoomId!,
          new_room_id: pickedRoom!,
          reason,
          markup_amount: effMarkup || undefined,
          transfer_date:
            isCheckedIn && transferDate ? transferDate.format("YYYY-MM-DD") : undefined,
        })
        .then((r) => r.data),
    onSuccess: () => {
      message.success("已换房，请把新房密码发给客人");
      qc.invalidateQueries({ queryKey: ["orders"] });
      qc.invalidateQueries({ queryKey: ["order", order!.order_id] }); // 刷新打开中的订单详情
      qc.invalidateQueries({ queryKey: ["rooms"] });
      qc.invalidateQueries({ queryKey: ["staff", "calendar"] });
      reset();
      onSuccess?.();
      onClose();
    },
    onError: (e) => message.error(extractErrorMessage(e)),
  });

  const options = useMemo(() => {
    if (!rooms) return [];
    return (rooms as RoomOut[])
      .filter(
        (r) =>
          r.room_status !== "maintenance" &&
          r.room_status !== "locked" &&
          r.room_id !== currentRoomId,
      )
      .sort((a, b) => {
        if (a.room_status === "available" && b.room_status !== "available") return -1;
        if (a.room_status !== "available" && b.room_status === "available") return 1;
        return (Number(a.base_price) || 0) - (Number(b.base_price) || 0);
      })
      .map((r) => ({ value: r.room_id, label: `${r.room_id} · ${r.room_name}`, room: r }));
  }, [rooms, order]);

  return (
    <Modal
      open={open}
      title="换房"
      onCancel={() => {
        reset();
        onClose();
      }}
      onOk={() => {
        if (!orderRoomId) {
          message.warning("该订单无可换房间行");
          return;
        }
        if (!pickedRoom) {
          message.warning("请选择目标房间");
          return;
        }
        transferMutation.mutate();
      }}
      okText="确认换房"
      cancelText="取消"
      confirmLoading={transferMutation.isPending}
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
              {currentRoomId && (
                <Text type="secondary" style={{ marginLeft: 8 }}>
                  当前房间: {currentRoomId}
                </Text>
              )}
            </div>
          </div>

          <div style={{ marginBottom: 12 }}>
            <div style={{ fontSize: 12, color: tokens.color.text.secondary, marginBottom: 4 }}>
              换房原因
            </div>
            <Select
              style={{ width: "100%" }}
              size="large"
              value={reason}
              onChange={(v) => {
                const next = v as Reason;
                setReason(next);
                if (next === "room_defect") setMarkup(0); // 故障不加价，清掉残留值避免切回来误收费
              }}
              options={REASON_OPTIONS}
            />
          </div>

          {reason === "room_defect" && (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 12 }}
              message="确认后旧房将转「维修」并自动生成维修任务。"
            />
          )}

          <Select
            showSearch
            placeholder="选择目标房间（按价格升序，空置优先）"
            style={{ width: "100%" }}
            size="large"
            value={pickedRoom}
            optionFilterProp="label"
            onChange={setPickedRoom}
            options={options}
            optionRender={(option) => {
              const r = (option.data as { room: RoomOut }).room;
              return (
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <span>
                    <Text strong>{r.room_id}</Text>
                    <Text type="secondary" style={{ marginLeft: 8, fontSize: 12 }}>
                      {r.room_name}
                    </Text>
                    {r.base_price != null && (
                      <Text type="secondary" style={{ marginLeft: 8, fontSize: 12 }} className="tabular">
                        ¥{Number(r.base_price).toLocaleString()}
                      </Text>
                    )}
                  </span>
                  <Tag color={ROOM_STATUS_COLOR[r.room_status]} style={{ fontSize: 11 }}>
                    {ROOM_STATUS_LABEL[r.room_status]}
                  </Tag>
                </div>
              );
            }}
          />
          {pickedNotReady && (
            <Alert
              type="info"
              showIcon
              style={{ marginTop: 12 }}
              message="目标房尚未打扫，换入前请确认房间可用。"
            />
          )}

          <div style={{ display: "flex", gap: 12, marginTop: 12, flexWrap: "wrap" }}>
            {isCheckedIn && (
              <div>
                <div style={{ fontSize: 12, color: tokens.color.text.secondary, marginBottom: 4 }}>
                  换房日（入住中按此日拆账）
                </div>
                <DatePicker
                  size="large"
                  value={transferDate}
                  onChange={setTransferDate}
                  placeholder="默认今天"
                  disabledDate={(d) =>
                    !!order &&
                    (d.isBefore(dayjs(order.check_in_date), "day") ||
                      !d.isBefore(dayjs(order.check_out_date), "day"))
                  }
                />
              </div>
            )}
            <div>
              <div style={{ fontSize: 12, color: tokens.color.text.secondary, marginBottom: 4 }}>
                加价金额（0 = 免费）
              </div>
              <MobileInputNumber
                size="large"
                min={0}
                value={effMarkup}
                onChange={(v) => setMarkup(Number(v) || 0)}
                disabled={reason === "room_defect"}
                prefix="¥"
                inputMode="decimal"
                style={{ width: 160 }}
              />
            </div>
          </div>

          <div style={{ marginTop: 12, fontSize: 12, color: tokens.color.text.tertiary }}>
            系统会原子地校验日期冲突 + 房态联动；加价后订单变「待收」，退房时收款。门锁密码需手动换发给客人。
          </div>
        </>
      )}
    </Modal>
  );
}
