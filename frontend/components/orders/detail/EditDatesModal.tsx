"use client";

import React, { useEffect, useState } from "react";
import { Modal, DatePicker, message } from "antd";
import dayjs from "dayjs";
import { tokens } from "@/lib/design-tokens";

export interface EditDatesCtx {
  orderRoomId: string;
  oldCheckIn: string;
  oldCheckOut: string;
}

interface Props {
  ctx: EditDatesCtx | null;
  /** 与晚数 stepper 共用父组件的 editDatesMutation（payload 计算见 lib/order-dates） */
  mutation: { mutate: (v: { orderRoomId: string; ci: string; co: string }) => void; isPending: boolean };
  onClose: () => void;
}

// 内联调整入住/退房日期 — 续住 / 缩短住 都走这里，无需跳"修改订单"
export function EditDatesModal({ ctx, mutation, onClose }: Props) {
  const [value, setValue] = useState<[any, any] | null>(null);

  useEffect(() => {
    if (ctx) {
      setValue([dayjs(ctx.oldCheckIn), dayjs(ctx.oldCheckOut)]);
    } else {
      setValue(null);
    }
  }, [ctx]);

  return (
    <Modal
      open={!!ctx}
      title="调整入住 / 退房日期"
      onCancel={onClose}
      okText="保存"
      cancelText="取消"
      confirmLoading={mutation.isPending}
      onOk={() => {
        if (!ctx) return;
        const [ci, co] = value || [];
        if (!ci || !co || !co.isAfter(ci)) {
          message.error("退房日期必须晚于入住日期");
          return;
        }
        mutation.mutate({
          orderRoomId: ctx.orderRoomId,
          ci: ci.format("YYYY-MM-DD"),
          co: co.format("YYYY-MM-DD"),
        });
      }}
    >
      {ctx && (
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <div style={{ color: tokens.color.text.secondary, fontSize: 13 }}>
            当前：{ctx.oldCheckIn} → {ctx.oldCheckOut}
          </div>
          <DatePicker.RangePicker
            size="large"
            style={{ width: "100%" }}
            format="YYYY-MM-DD"
            value={value as any}
            onChange={(v) => setValue(v as any)}
          />
          <div
            style={{
              background: "#FFF7E6",
              border: "1px dashed #FFD591",
              borderRadius: 8,
              padding: "8px 12px",
              fontSize: 12,
              color: "#D46B08",
              lineHeight: 1.5,
            }}
          >
            延长入住后新日期房费默认 ¥0，可在保存后点该日 chip 单独定价，
            系统会自动均摊到所有晚数。
          </div>
        </div>
      )}
    </Modal>
  );
}
