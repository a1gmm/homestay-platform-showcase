"use client";

import React, { useEffect, useState } from "react";
import { Modal, message } from "antd";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ordersApi } from "@/lib/api";
import { invalidateOrderRelated, invalidatePaymentWithAudit } from "@/lib/order-cache";
import { extractErrorMessage } from "@/lib/api-errors";
import { tokens } from "@/lib/design-tokens";
import { MobileInputNumber } from "@/components/ui/MobileInputNumber";

export interface EditDailyCtx {
  orderRoomId: string;
  date: string;       // YYYY-MM-DD
  oldPrice: number;
}

interface Props {
  ctx: EditDailyCtx | null;
  order: any;
  /** 展示用房间行（含 legacy 合成行），由父组件统一推导 */
  orderRooms: any[];
  /** 已收合计，用于底部提示 */
  totalPaid: number;
  onClose: () => void;
}

// 单日改价：点某天 chip 打开，InputNumber 改完确认；实时预览均摊后金额
export function DailyPriceModal({ ctx, order, orderRooms, totalPaid, onClose }: Props) {
  const queryClient = useQueryClient();
  const [value, setValue] = useState<number | null>(null);

  useEffect(() => {
    setValue(ctx ? ctx.oldPrice : null);
  }, [ctx]);

  const dailyPriceMutation = useMutation({
    mutationFn: ({ orderRoomId, date, price }: { orderRoomId: string; date: string; price: number }) =>
      ordersApi.updateDailyPrice(order.order_id, orderRoomId, date, price),
    onSuccess: () => {
      message.success("已修改该日房价");
      // 甘特图会在订单条上显示每晚 ¥ 价（来自 ["rooms","calendar-window"]）；
      // 改价必须失效 ["rooms"]，否则条上价格要手动刷新才更新。走统一 helper 收口。
      invalidateOrderRelated(queryClient);
      invalidatePaymentWithAudit(queryClient, order?.order_id);
      onClose();
    },
    onError: (e: any) => message.error(extractErrorMessage(e, "修改失败")),
  });

  return (
    <Modal
      open={!!ctx}
      title={ctx ? `调整房费（保存后自动均摊到所有晚数）` : "调整房费"}
      onCancel={onClose}
      okText="保存"
      cancelText="取消"
      confirmLoading={dailyPriceMutation.isPending}
      onOk={() => {
        if (!ctx || value == null || value < 0) {
          message.error("请填写有效价格");
          return;
        }
        dailyPriceMutation.mutate({
          orderRoomId: ctx.orderRoomId,
          date: ctx.date,
          price: value,
        });
      }}
    >
      {ctx && (() => {
        // 当前房间的 nights 和其他天的现值，用于实时预览"均摊后"金额
        const targetOr = orderRooms.find((or) => or.order_room_id === ctx.orderRoomId);
        const targetNights = targetOr?.nights ?? 0;
        const targetOldActual = Number(targetOr?.actual_price ?? 0);
        // 这次单日改价之后房间总价 = 旧总 - 旧那天价 + 新价
        const newRoomTotal =
          value != null
            ? targetOldActual - ctx.oldPrice + value
            : null;
        const avgPerNight = newRoomTotal != null && targetNights > 0 ? newRoomTotal / targetNights : null;
        return (
          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            <div style={{ color: tokens.color.text.secondary, fontSize: 13 }}>
              原价 ¥{ctx.oldPrice.toFixed(2)} → 新价
            </div>
            <MobileInputNumber
              size="large"
              style={{ width: "100%" }}
              min={0}
              precision={2}
              prefix="¥"
              inputMode="decimal"
              value={value ?? undefined}
              onChange={(v) => setValue(typeof v === "number" ? v : null)}
              autoFocus
            />
            {avgPerNight != null && newRoomTotal != null && (
              <div
                style={{
                  background: tokens.color.brand.primarySoft,
                  border: `1px dashed ${tokens.color.brand.primary}44`,
                  borderRadius: 8,
                  padding: "8px 12px",
                  fontSize: 12,
                  color: tokens.color.text.primary,
                  lineHeight: 1.6,
                }}
              >
                <div>
                  保存后房间总价：
                  <span className="tabular" style={{ fontWeight: 600 }}>
                    ¥{newRoomTotal.toLocaleString(undefined, { maximumFractionDigits: 2 })}
                  </span>
                </div>
                <div>
                  自动均摊到 {targetNights} 晚：
                  <span className="tabular" style={{ fontWeight: 600, color: tokens.color.brand.primary }}>
                    ¥{avgPerNight.toLocaleString(undefined, { maximumFractionDigits: 2 })}
                  </span>
                  <span style={{ color: tokens.color.text.tertiary }}> / 晚</span>
                </div>
              </div>
            )}
            <div style={{ color: tokens.color.text.tertiary, fontSize: 12 }}>
              修改后订单总价会自动重算，不能低于已收 ¥{totalPaid.toFixed(2)}
            </div>
          </div>
        );
      })()}
    </Modal>
  );
}
