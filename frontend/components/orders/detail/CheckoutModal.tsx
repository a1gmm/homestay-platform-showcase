"use client";

import React, { useEffect, useState } from "react";
import { Modal, Select, Alert, Checkbox, Button, message } from "antd";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ordersApi, staffApi } from "@/lib/api";
import { invalidateOrderWithAudit } from "@/lib/order-cache";
import { extractErrorMessage } from "@/lib/api-errors";
import { tokens } from "@/lib/design-tokens";

interface Props {
  open: boolean;
  order: any;
  onClose: () => void;
}

// 发起退房：走 handle_checkout 派清扫（押金已下线，王总 2026-07-22，改走线下 POS + 飞书小票，退房不再结算押金）。
export function CheckoutModal({ open, order, onClose }: Props) {
  const queryClient = useQueryClient();
  const [pickedCleaner, setPickedCleaner] = useState<string | undefined>(undefined);
  // 续住防呆：探测到续住续单时，须显式勾选"确认非续住"才放行退房（1613 事故 2026-07-09）
  const [ackNonContinuation, setAckNonContinuation] = useState(false);

  // 单间退房：只列已入住且尚未退房的房间，兼容无逐房入住标记的历史订单。多于一间时须让前台选退哪间，
  // 避免"点一间连带整单退"的老坑。值 "__all__" = 整单一起退。
  const allRooms: any[] = Array.isArray(order?.rooms) ? order.rooms : [];
  const hasArrivalMarkers = allRooms.some((r: any) => r.checked_in_at);
  const activeRooms = allRooms.filter(
    (r: any) => r?.room_id && !r?.checked_out_at && (!hasArrivalMarkers || r.checked_in_at),
  );
  const waitingCount = allRooms.filter(
    (r: any) => r?.room_id && !r?.checked_out_at && hasArrivalMarkers && !r.checked_in_at,
  ).length;
  const isMultiRoom = activeRooms.length > 1;
  const [selectedRoom, setSelectedRoom] = useState<string | undefined>(undefined);
  // 传给后端的单间标识：多房单选了具体某间才传；整单/单房单不传。
  const targetOrderRoomId =
    isMultiRoom && selectedRoom && selectedRoom !== "__all__" ? selectedRoom : undefined;

  // 打开时复位
  useEffect(() => {
    if (open) {
      setAckNonContinuation(false);
      setSelectedRoom(undefined);
    }
  }, [open]);

  // 退房前探测续住续单（同房同客、退房日紧接着的下一段订单）。有则提醒改用「门锁密码延期」。
  const { data: precheck, isError: precheckFailed, isFetching: precheckLoading, refetch: retryPrecheck } = useQuery({
    queryKey: ["orders", order?.order_id, "checkout-precheck"],
    queryFn: () => ordersApi.checkoutPrecheck(order.order_id).then((r) => r.data),
    enabled: open && !!order?.order_id,
    staleTime: 0,
    refetchOnWindowFocus: false,
    retry: false,
  });
  const continuations = precheck?.continuations ?? [];
  const hasContinuation = continuations.length > 0;

  const close = () => {
    setPickedCleaner(undefined);
    setAckNonContinuation(false);
    setSelectedRoom(undefined);
    onClose();
  };

  const { data: cleanerList } = useQuery({
    queryKey: ["staff", "cleaners"],
    queryFn: () => staffApi.listCleaners().then((r) => r.data),
    enabled: open,
    staleTime: 60 * 1000,
  });

  const checkoutMutation = useMutation({
    mutationFn: async (cleanerId: string | undefined) => {
      // 押金已下线：退房不再结算押金（真钱走线下 POS + 飞书小票）。
      // cleanerId 留空 = 暂不派单（批3 item1）：仍退房、房置待保洁，建未分配清扫任务进待派池。
      // targetOrderRoomId 有值 = 只退这一间；无值 = 整单退房。
      return ordersApi.handleCheckout(order.order_id, cleanerId, undefined, targetOrderRoomId);
    },
    onSuccess: (_res, cleanerId) => {
      const scope = targetOrderRoomId ? "已退该房间" : "已发起退房";
      message.success(cleanerId ? `${scope}并派单给保洁` : `${scope}（暂不派单，稍后可在任务里派）`);
      invalidateOrderWithAudit(queryClient, order?.order_id);
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
      queryClient.invalidateQueries({ queryKey: ["rooms"] });
      close();
    },
    onError: (e: any) => message.error(extractErrorMessage(e, "退房失败")),
  });

  return (
    <Modal
      open={open}
      title="发起退房"
      onCancel={close}
      onOk={() => {
        if (precheckLoading || precheckFailed || !precheck) {
          message.warning("请等待续住检查完成后再退房");
          return;
        }
        // 多房单必须先选退哪间（或整单一起退），避免误退。
        if (isMultiRoom && !selectedRoom) {
          message.warning("请选择要退房的房间（或选「全部退房」整单一起退）");
          return;
        }
        // cleaner 选填（批3 item1）：留空 = 暂不派单，不再强制。
        if (hasContinuation && !ackNonContinuation) {
          message.warning("此房有续住续单，续住请用「门锁密码延期」；如确非续住请勾选确认");
          return;
        }
        checkoutMutation.mutate(pickedCleaner);
      }}
      okText={pickedCleaner ? "确认退房并派单" : "确认退房（暂不派单）"}
      cancelText="取消"
      confirmLoading={checkoutMutation.isPending}
      okButtonProps={{ disabled: precheckLoading || precheckFailed || !precheck || (allRooms.length > 0 && activeRooms.length === 0) }}
    >
      <div style={{ marginBottom: 12, fontSize: 13, color: tokens.color.text.secondary }}>
        客人 {order?.guest_name}
        {!isMultiRoom && <> · 房间 {activeRooms[0]?.room_id || order?.room_id || "(未排房)"}</>}
      </div>

      {precheckLoading && <Alert type="info" showIcon message="正在检查是否有续住订单，请稍候" style={{ marginBottom: 12 }} />}
      {waitingCount > 0 && <Alert type="info" showIcon message={`另有 ${waitingCount} 间尚未入住，本次退房不会处理这些房间`} style={{ marginBottom: 12 }} />}
      {/* 单间退房：多房单必须先选退哪间，或选「全部退房」整单一起退。这是「点一间连带整单退」老坑的根治入口。 */}
      {isMultiRoom && (
        <div style={{ marginBottom: 12 }}>
          <div style={{ fontSize: 13, fontWeight: 500, marginBottom: 6 }}>
            这是多房间订单（{activeRooms.length} 间在住），请选择要退的房间
          </div>
          <Select
            style={{ width: "100%" }}
            size="large"
            placeholder="选择要退房的房间"
            value={selectedRoom}
            onChange={(v) => setSelectedRoom(v)}
            options={[
              ...activeRooms.map((r: any) => ({
                value: r.order_room_id,
                label: `只退 房间 ${r.room_id}`,
              })),
              { value: "__all__", label: `全部退房（${activeRooms.length} 间一起退）` },
            ]}
          />
          <div style={{ marginTop: 6, fontSize: 12, color: tokens.color.text.tertiary }}>
            {targetOrderRoomId
              ? "只退这一间，其它房间的客人不受影响、门锁密码保留，订单仍为在住。"
              : selectedRoom === "__all__"
                ? "本单已入住的房间一起退房，尚未入住的房间保留。"
                : "退最后一间时订单才整体转「已退房待收款」。"}
          </div>
        </div>
      )}

      {/* 续住防呆：此房紧接着还有同客的续住续单 → 办退房会作废客人当前门锁密码。
          续住应改用「门锁密码延期」沿用原密码（1613 事故 2026-07-09）。 */}
      {/* 校验失败时暂停退房；即使缓存中有旧结果，也必须允许重新检查。 */}
      {precheckFailed && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message="续住校验暂不可用"
          description="暂时无法确认是否有续住订单，请重试。若客人续住，请回原订单办理门锁密码延期。"
          action={<Button loading={precheckLoading} onClick={() => retryPrecheck()}>重新检查</Button>}
        />
      )}

      {hasContinuation && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message="疑似续住，别急着退房"
          description={
            <div style={{ fontSize: 13 }}>
              <div style={{ marginBottom: 6 }}>
                此房紧接着还有续住续单：
                {continuations.map((c) => (
                  <span key={c.next_order_id}>
                    {c.guest_name}（{c.check_in_date} 入住）
                  </span>
                ))}
                。办退房会作废客人当前门锁密码，续住请回原订单点<b>「门锁密码延期」</b>沿用原密码。
              </div>
              <Checkbox
                checked={ackNonContinuation}
                onChange={(e) => setAckNonContinuation(e.target.checked)}
              >
                我确认这不是续住，仍要退房
              </Checkbox>
            </div>
          }
        />
      )}
      <Select
        showSearch
        allowClear
        placeholder="选择保洁员（可留空＝暂不派单，稍后再派）"
        style={{ width: "100%" }}
        size="large"
        value={pickedCleaner}
        optionFilterProp="label"
        onChange={(v) => setPickedCleaner(v)}
        options={(cleanerList as any[] || []).map((c: any) => ({
          value: c.user_id,
          label: c.phone ? `${c.display_name} · ${c.phone}` : c.display_name,
        }))}
        notFoundContent={cleanerList === undefined ? "加载中…" : "暂无可派的保洁员"}
      />

      <div style={{ marginTop: 12, fontSize: 12, color: tokens.color.text.tertiary }}>
        {targetOrderRoomId
          ? "确认后：只把这一间房置「待保洁」、撤这间的门锁密码、派这间的清扫任务；订单其它房间照常在住。"
          : waitingCount > 0
            ? "确认后：仅已入住的房间退房并进入待保洁；订单保留在住状态，其他房间仍可继续办理入住。未分配的清扫任务可稍后在任务里指派。"
            : "确认后：订单状态推到「已退房待收款」，房态置「待保洁」。每间房创建一条清扫任务；留空则暂不派单，可稍后在任务里指派。押金请用线下 POS 收退并在飞书传小票。"}
      </div>
    </Modal>
  );
}
