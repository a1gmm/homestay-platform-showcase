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

export function RevertCheckoutModal({ open, order, onClose }: Props) {
  const queryClient = useQueryClient();
  const [revertReason, setRevertReason] = useState("");

  const close = () => {
    setRevertReason("");
    onClose();
  };

  const revertCheckoutMutation = useMutation({
    mutationFn: (reason: string) => ordersApi.revertCheckout(order.order_id, reason),
    onSuccess: (resp: any) => {
      const data = resp?.data || {};
      const taskNote = data.cancelled_task_ids?.length
        ? `，已取消 ${data.cancelled_task_ids.length} 条清扫任务`
        : "";
      const roomNote = data.room_restored ? "，房态恢复占用" : "";
      message.success(`已撤销退房${taskNote}${roomNote}`);
      invalidateOrderWithAudit(queryClient, order?.order_id);
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
      close();
    },
    onError: (e: any) => message.error(extractErrorMessage(e, "撤销失败")),
  });

  return (
    <Modal
      open={open}
      title="撤销退房"
      onCancel={close}
      onOk={() => {
        if (!revertReason.trim()) {
          message.warning("请填写撤销原因");
          return;
        }
        revertCheckoutMutation.mutate(revertReason.trim());
      }}
      okText="确认撤销"
      okButtonProps={{ danger: true }}
      cancelText="取消"
      confirmLoading={revertCheckoutMutation.isPending}
    >
      <div style={{ marginBottom: 12, fontSize: 13, color: tokens.color.text.secondary }}>
        撤销后订单将从「已完成」回到「已入住」，关联的待办清扫任务会被取消（保洁不再来打扫该房间）。
        房间状态若仍空闲会自动恢复为占用，若已被新订单占用则不变。
      </div>
      <Input.TextArea
        rows={3}
        value={revertReason}
        onChange={(e) => setRevertReason(e.target.value)}
        placeholder="必填：撤销原因（如：客户信息错误需修改、补录收款后重走流程）"
        maxLength={200}
        showCount
        autoFocus
      />
    </Modal>
  );
}
