"use client";

import React, { useState } from "react";
import { Modal, Input, message } from "antd";
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

// 撤销入住（批3 item5）：误点入住时把订单从「在住」退回「待入住」，房态占用→预留。
export function RevertCheckinModal({ open, order, onClose }: Props) {
  const queryClient = useQueryClient();
  const [reason, setReason] = useState("");

  const close = () => {
    setReason("");
    onClose();
  };

  const revertCheckinMutation = useMutation({
    mutationFn: (r: string) => ordersApi.revertCheckin(order.order_id, r),
    onSuccess: (resp: any) => {
      const roomNote = resp?.data?.room_reverted ? "，房态回退为预留" : "";
      const depNote = resp?.data?.deposit_reverted ? "，押金已撤回" : "";
      message.success(`已撤销入住${roomNote}${depNote}`);
      invalidateOrderWithAudit(queryClient, order?.order_id);
      close();
    },
    onError: (e: any) => message.error(extractErrorMessage(e, "撤销失败")),
  });

  return (
    <Modal
      open={open}
      title="撤销入住"
      onCancel={close}
      onOk={() => {
        if (!reason.trim()) {
          message.warning("请填写撤销原因");
          return;
        }
        revertCheckinMutation.mutate(reason.trim());
      }}
      okText="确认撤销"
      okButtonProps={{ danger: true }}
      cancelText="取消"
      confirmLoading={revertCheckinMutation.isPending}
    >
      <div style={{ marginBottom: 12, fontSize: 13, color: tokens.color.text.secondary }}>
        撤销后订单退回入住前状态，房间占用会回退为预留（若已被别的客人占用则不动）。
        办理入住时下发的门锁客人码会被撤回，自动记录的押金收取也会回退（误点入住并未真实收款）。
      </div>
      <Input.TextArea
        rows={3}
        value={reason}
        onChange={(e) => setReason(e.target.value)}
        placeholder="必填：撤销原因（如：误点入住、客人未实际到店）"
        maxLength={200}
        showCount
        autoFocus
      />
    </Modal>
  );
}
