"use client";

import React, { useEffect, useState } from "react";
import { Modal, Form, Select, Input, Row, Col, message, Alert, Radio } from "antd";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { roomsApi, extractErrorMessage, isDuplicateOrderError } from "@/lib/api";
import { createOrdersAndMarkSeen } from "@/lib/create-orders";
import { useNewOrderStore } from "@/lib/new-order-store";
import type { OrderCreate, RoomOut, BookingType } from "@/lib/types";
import { isPhonePriceExempt } from "@/lib/types";
import { MobileInput } from "@/components/ui/MobileInput";
import { MobileInputNumber } from "@/components/ui/MobileInputNumber";
import { OrderRoomsField } from "@/components/orders/OrderRoomsField";
import { roomFormRowsToPayload, syncRoomRowsListPrice, splitOrderPayloadByRoom, type RoomFormRow } from "@/lib/order-rooms";
import dayjs from "dayjs";
import { useIsMobile } from "@/lib/responsive";

const { TextArea } = Input;

import { ACTIVE_CHANNELS, PLATFORM_CHANNELS } from "@/lib/channels";
import { reversedCaliberWarning } from "@/lib/caliber";

const CHANNELS = ACTIVE_CHANNELS.map((c) => ({ value: c.code, label: c.label }));

export interface QuickCreateInit {
  room_id?: string;
  check_in_date?: string;
  check_out_date?: string;
}

interface Props {
  open: boolean;
  initial?: QuickCreateInit | null;
  onClose: () => void;
  onSuccess?: () => void;
}

/**
 * 运营台 / 甘特图「+新建订单」入口的 Modal 版本。
 * 支持预填 room_id + 日期段（甘特图空白格点击/拖拽时使用），并支持「一客多房」——
 * 房间录入逻辑与 /orders/new 完整页共用 <OrderRoomsField>，保证两个入口一致、不分叉。
 */
export function QuickCreateOrderModal({ open, initial, onClose, onSuccess }: Props) {
  const isMobile = useIsMobile();
  const qc = useQueryClient();
  const [form] = Form.useForm();
  // 订单类型决定必填星号：试住/自住单豁免手机号、实收（客人实付已全局选填）。
  const bookingType = Form.useWatch("booking_type", form) as BookingType | undefined;
  const phonePriceExempt = isPhonePriceExempt(bookingType);
  const [channel, setChannel] = useState<string>("self_acquired");

  const { data: rooms } = useQuery({
    queryKey: ["rooms"],
    queryFn: () => roomsApi.list().then((r) => r.data),
    enabled: open,
  });

  // 重置 + 应用预填值（甘特图点格子带进来的房间/日期作为第一行）
  useEffect(() => {
    if (!open) return;
    form.resetFields();
    setChannel("self_acquired");
    if (initial) {
      const dates =
        initial.check_in_date && initial.check_out_date
          ? [dayjs(initial.check_in_date), dayjs(initial.check_out_date)]
          : initial.check_in_date
          ? [dayjs(initial.check_in_date), dayjs(initial.check_in_date).add(1, "day")]
          : undefined;
      form.setFieldsValue({
        channel: "self_acquired",
        deposit: 0,
        rooms: [{ room_id: initial.room_id ?? undefined, dates, guests_count: 0 }],
      });
    } else {
      form.setFieldsValue({
        channel: "self_acquired",
        deposit: 0,
        rooms: [{ guests_count: 0 }],
      });
    }
  }, [open, initial, form]);

  const markSeen = useNewOrderStore((s) => s.markSeen);

  const createMutation = useMutation({
    // 同客多间「自动拆单」：payloads 为 N 张单房 payload，逐张顺序建单。兄弟单已带
    // allow_duplicate，只有第一张可能触发「同客重复单」拦截。
    // markSeen 在 helper 的循环内逐单调（不是等 onSuccess）——否则多房单建到一半时
    // 新订单轮询醒来，会把已建好的单弹给刚录完它的前台自己。
    mutationFn: (payloads: OrderCreate[]) => createOrdersAndMarkSeen(payloads, markSeen),
    onSuccess: (_data, payloads: OrderCreate[]) => {
      const n = payloads?.length ?? 1;
      message.success(n > 1 ? `已按房间拆成 ${n} 张订单创建成功` : "订单创建成功");
      qc.invalidateQueries({ queryKey: ["orders"] });
      qc.invalidateQueries({ queryKey: ["rooms"] });
      qc.invalidateQueries({ queryKey: ["staff", "calendar"] });
      onSuccess?.();
      onClose();
    },
    // 重复单 409 走下方 mutateWithDupConfirm 的确认对话框，不再重复弹 toast。
    onError: (e) => {
      if (isDuplicateOrderError(e)) return;
      message.error(extractErrorMessage(e));
    },
  });

  // 同客同日期重复单：后端 409（duplicate_order）拦下 → 弹确认，
  // 用户确认「同名不同客」后第一张也带 allow_duplicate 重发整批。
  const mutateWithDupConfirm = (payloads: OrderCreate[]) =>
    createMutation.mutate(payloads, {
      onError: (err) => {
        if (!isDuplicateOrderError(err)) return;
        Modal.confirm({
          title: "疑似重复订单",
          content: extractErrorMessage(err),
          okText: "确为不同客人，继续创建",
          cancelText: "返回检查",
          okButtonProps: { danger: true },
          onOk: () =>
            createMutation.mutate(payloads.map((p) => ({ ...p, allow_duplicate: true }))),
        });
      },
    });

  // 试住/自住单：某行选了房且本行实收为空时，自动带入该房挂牌价（仅填空，不覆盖）。
  const fillRowBasePrice = (idx: number, roomId?: string | null) => {
    const room = (rooms as RoomOut[] | undefined)?.find((r) => r.room_id === roomId);
    if (!room?.base_price) return;
    const curActual = form.getFieldValue(["rooms", idx, "actual_price"]);
    if (curActual == null || curActual === "") {
      form.setFieldValue(["rooms", idx, "actual_price"], Number(room.base_price));
    }
    const curList = form.getFieldValue(["rooms", idx, "list_price"]);
    if (curList == null || curList === "") {
      form.setFieldValue(["rooms", idx, "list_price"], Number(room.base_price));
    }
  };

  const onFinish = (values: any) => {
    const { rooms: roomPayload, totalActual } = roomFormRowsToPayload(values.rooms as RoomFormRow[]);
    if (roomPayload.length === 0) {
      message.error("请至少添加一个房间");
      return;
    }
    const payload: OrderCreate = {
      channel: values.channel,
      guest_name: values.guest_name,
      guest_phone: values.guest_phone || undefined, // issue#3: 选填
      booking_type: (values.booking_type || "normal") as BookingType, // issue#6
      rooms: roomPayload,
      actual_price: totalActual,
      deposit: 0, // 押金已下线（王总 2026-07-22）：走线下 POS + 飞书小票
      // 平台渠道填「到手价」→ 后端倒推佣金率;非平台/未填不带。(#44 口径重做 2026-07-01)
      ota_owner_revenue:
        PLATFORM_CHANNELS.has(values.channel) && values.ota_owner_revenue != null
          ? values.ota_owner_revenue
          : undefined,
      notes: values.notes || undefined,
    };

    // 防呆：平台单「净房费 > 房费」几乎一定是填反了 → 弹确认，不硬拦（留负佣金边界）。
    const reversedWarn = reversedCaliberWarning({
      isPlatform: PLATFORM_CHANNELS.has(values.channel),
      ownerRevenue: values.ota_owner_revenue,
      roomFeeTotal: totalActual,
    });
    if (reversedWarn) {
      Modal.confirm({
        title: "金额可能填反了",
        content: reversedWarn,
        okText: "确定这样保存",
        cancelText: "返回修改",
        okButtonProps: { danger: true },
        onOk: () => mutateWithDupConfirm(splitOrderPayloadByRoom(payload)),
      });
      return;
    }
    mutateWithDupConfirm(splitOrderPayloadByRoom(payload));
  };

  const isPlatform = PLATFORM_CHANNELS.has(channel);

  return (
    <Modal
      open={open}
      title="新建订单"
      width={isMobile ? undefined : 720}
      onCancel={() => {
        if (createMutation.isPending) return;
        onClose();
      }}
      onOk={() => form.submit()}
      okText="创建订单"
      cancelText="取消"
      confirmLoading={createMutation.isPending}
      destroyOnHidden
    >
      {createMutation.isError && (
        <Alert
          type="error"
          message={extractErrorMessage(createMutation.error)}
          style={{ marginBottom: 12, borderRadius: 8 }}
          showIcon
          closable
        />
      )}

      <Form
        form={form}
        layout="vertical"
        onFinish={onFinish}
        initialValues={{
          channel: "self_acquired",
          booking_type: "normal",
          deposit: 0,
          rooms: [{ guests_count: 0 }],
        }}
        onValuesChange={(changed, all) => {
          if (changed.channel) setChannel(changed.channel);
          // issue#103 Step 3: 客人价默认带入实收（仅填空，不覆盖）
          if (changed.rooms) {
            syncRoomRowsListPrice(changed.rooms as any[], form);
            // 试住/自住单：某行换了房 → 带入该房挂牌价
            if (all.booking_type && all.booking_type !== "normal") {
              (changed.rooms as any[]).forEach((rc, idx) => {
                if (rc && rc.room_id !== undefined) fillRowBasePrice(idx, rc.room_id);
              });
            }
          }
          // issue#6: 切到试住/自住单 → 各行带入挂牌价
          if (changed.booking_type && changed.booking_type !== "normal") {
            ((all.rooms as RoomFormRow[]) || []).forEach((r, idx) => {
              if (r?.room_id) fillRowBasePrice(idx, r.room_id);
            });
          }
        }}
      >
        <Row gutter={12}>
          {/* issue#3: 平台订单号已删除 */}
          <Col xs={24} sm={12}>
            <Form.Item name="channel" label="预订渠道" rules={[{ required: true }]}>
              <Select options={CHANNELS} />
            </Form.Item>
          </Col>
          {/* issue#6: 订单类型 */}
          <Col xs={24} sm={12}>
            <Form.Item
              name="booking_type"
              label="订单类型"
              tooltip="试住/自住业主出钱，按业主合同分成比例计算业主应付"
              rules={[{ required: true, message: "请选择订单类型" }]}
            >
              <Radio.Group buttonStyle="solid">
                <Radio.Button value="normal">普通</Radio.Button>
                <Radio.Button value="trial">试住</Radio.Button>
                <Radio.Button value="owner_self">自住</Radio.Button>
              </Radio.Group>
            </Form.Item>
          </Col>
        </Row>

        <Row gutter={12}>
          <Col xs={24} sm={12}>
            <Form.Item name="guest_name" label="客人姓名" rules={[{ required: true, message: "请输入客人姓名" }]}>
              <MobileInput placeholder="真实姓名" />
            </Form.Item>
          </Col>
          <Col xs={24} sm={12}>
            <Form.Item
              name="guest_phone"
              label="手机号"
              rules={[
                // 手机号选填（前台先录单后补，后端同步放行空值）；填了就必须是 11 位中国大陆手机号。
                {
                  validator: (_, value) => {
                    if (!value) return Promise.resolve();
                    if (/^1[3-9]\d{9}$/.test(value)) return Promise.resolve();
                    return Promise.reject(new Error("请输入有效的 11 位手机号"));
                  },
                },
              ]}
            >
              <MobileInput placeholder="13800138000" maxLength={11} inputMode="tel" allowClear />
            </Form.Item>
          </Col>
        </Row>

        {/* 房间信息（多房）—— 与 /orders/new 共用同一套录入逻辑 */}
        <Form.Item label="房间信息（可加多间）" style={{ marginBottom: 12 }} required>
          {/* allowPastDates：甘特图点过去的空格建单(补录)，日期框不能把那一天灰掉 */}
          <OrderRoomsField form={form} rooms={rooms as RoomOut[] | undefined} phonePriceExempt={phonePriceExempt} allowPastDates />
        </Form.Item>

        {/* 押金已下线（王总 2026-07-22）：走线下 POS + 飞书小票，建单不再录押金 */}
        <Row gutter={12}>
          {isPlatform && (
            <Col xs={24} sm={24}>
              <Form.Item
                name="ota_owner_revenue"
                label="净房费 · 平台结给我们 (¥)"
                tooltip="平台扣完佣金后实际结给我们的钱（到手）。填这个即可，系统自动算佣金，无需手填佣金率。留空则不计佣金。净房费应小于房费，差额=平台佣金。"
              >
                <MobileInputNumber style={{ width: "100%" }} min={0} precision={2} inputMode="decimal" placeholder="平台结算的净房费" controls={false} />
              </Form.Item>
            </Col>
          )}
        </Row>

        {/* 订单合计（派生）*/}
        <Form.Item noStyle shouldUpdate={(p, c) => p.rooms !== c.rooms}>
          {() => {
            const formRooms = (form.getFieldValue("rooms") || []) as RoomFormRow[];
            const total = formRooms.reduce((acc, r) => acc + (r?.actual_price ?? 0), 0);
            if (total === 0) return null;
            return (
              <div style={{ background: "#f6ffed", border: "1px solid #b7eb8f", borderRadius: 8, padding: "8px 14px", marginBottom: 12 }}>
                <span style={{ fontSize: 13, color: "#52c41a" }}>
                  订单合计：¥{total.toLocaleString()}
                </span>
              </div>
            );
          }}
        </Form.Item>

        <Form.Item name="notes" label="备注" style={{ marginBottom: 0 }}>
          <TextArea rows={2} placeholder="特殊要求、注意事项..." showCount maxLength={500} />
        </Form.Item>
      </Form>
    </Modal>
  );
}
