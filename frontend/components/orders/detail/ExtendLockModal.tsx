"use client";

import React, { useEffect, useState } from "react";
import { Modal, DatePicker, message } from "antd";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import dayjs from "dayjs";
import { ordersApi } from "@/lib/api";
import { extractErrorMessage } from "@/lib/api-errors";
import { tokens } from "@/lib/design-tokens";

interface Props {
  open: boolean;
  order: any;
  onClose: () => void;
}

// 续住门锁密码延期：把现有客人码延到新退房日，密码不变、不动订单日期/金额。
export function ExtendLockModal({ open, order, onClose }: Props) {
  const queryClient = useQueryClient();
  const [extendDate, setExtendDate] = useState<any>(null);

  // 打开时默认「原退房日 +1 天」
  useEffect(() => {
    if (open) {
      const co = order?.check_out_date ? dayjs(order.check_out_date) : dayjs();
      setExtendDate(co.add(1, "day"));
    }
  }, [open, order?.check_out_date]);

  const close = () => {
    setExtendDate(null);
    onClose();
  };

  const extendLockMutation = useMutation({
    mutationFn: (newDate: string) => ordersApi.extendLockCode(order.order_id, newDate),
    onSuccess: (resp: any) => {
      const rows = resp?.data?.results || [];
      const okCount = rows.filter((r: any) => r.ok).length;
      const failed = rows.filter((r: any) => !r.ok);
      if (failed.length === 0) {
        message.success(`门锁密码已延期，${okCount} 间房沿用原密码`);
      } else {
        message.warning(`部分延期未确认（锁可能离线）：${failed.map((r: any) => r.room_id).join("、")}，可稍后重试`);
      }
      // 最小失效集是有意为之（只刷该单详情与审计），别扩成全量 helper
      queryClient.invalidateQueries({ queryKey: ["order", order?.order_id] });
      queryClient.invalidateQueries({ queryKey: ["audit-logs", "order", order?.order_id] });
      close();
    },
    onError: (e: any) => message.error(extractErrorMessage(e, "门锁延期失败")),
  });

  return (
    <Modal
      open={open}
      title="门锁密码延期（续住）"
      onCancel={close}
      onOk={() => {
        if (!extendDate) {
          message.warning("请选择新的退房日期");
          return;
        }
        extendLockMutation.mutate(extendDate.format("YYYY-MM-DD"));
      }}
      okText="确认延期"
      cancelText="取消"
      confirmLoading={extendLockMutation.isPending}
    >
      <div style={{ marginBottom: 12, fontSize: 13, color: tokens.color.text.secondary }}>
        客人续住时用这个：把当前门锁密码的有效期延到新退房日（<b>密码不变</b>，客人继续用原来的）。
        <br />
        只延门锁密码，<b>不会改订单日期和金额</b>，不影响对账。请确认这确实是同一位客人续住。
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <span style={{ fontSize: 13 }}>新退房日期：</span>
        <DatePicker
          value={extendDate}
          onChange={(d) => setExtendDate(d)}
          allowClear={false}
          disabledDate={(d) => d && order?.check_out_date ? d.isBefore(dayjs(order.check_out_date), "day") : false}
        />
      </div>
      <div style={{ marginTop: 10, fontSize: 12, color: tokens.color.text.tertiary }}>
        密码将延至该日 <b>14:00</b> 失效（与正常退房上界一致）。
      </div>
    </Modal>
  );
}
