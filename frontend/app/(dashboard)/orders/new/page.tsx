"use client";

import React, { useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { roomsApi, extractErrorMessage, isDuplicateOrderError } from "@/lib/api";
import { createOrdersAndMarkSeen } from "@/lib/create-orders";
import { useNewOrderStore } from "@/lib/new-order-store";
import {
  Form, Input, Select, Button,
  Card, Row, Col, Typography, Space, Alert, message, Radio, Modal,
} from "antd";
import type { BookingType, RoomOut } from "@/lib/types";
import { isPhonePriceExempt } from "@/lib/types";
import { MobileInput } from "@/components/ui/MobileInput";
import { MobileInputNumber } from "@/components/ui/MobileInputNumber";
import { OrderRoomsField } from "@/components/orders/OrderRoomsField";
import { roomFormRowsToPayload, summarizePerRoomNets, perRoomNetActive, syncRoomRowsListPrice, splitOrderPayloadByRoom, type RoomFormRow } from "@/lib/order-rooms";
import {
  ArrowLeftOutlined, SaveOutlined, HomeOutlined,
  UserOutlined, PhoneOutlined, CalendarOutlined, DollarOutlined,
} from "@ant-design/icons";
import { ACTIVE_CHANNELS, PLATFORM_CHANNELS } from "@/lib/channels";
import { perRoomOrOrderReversedWarning } from "@/lib/caliber";

const { Title, Text } = Typography;
const { TextArea } = Input;

const CHANNELS = ACTIVE_CHANNELS.map((c) => ({ value: c.code, label: c.label }));

export default function NewOrderPage() {
  const router = useRouter();
  const qc = useQueryClient();
  const [form] = Form.useForm();
  const [channel, setChannel] = useState<string>("self_acquired");
  // 订单类型决定必填星号：试住/自住单豁免手机号、实收（客人实付已全局选填）。
  const bookingType = Form.useWatch("booking_type", form) as BookingType | undefined;
  const phonePriceExempt = isPhonePriceExempt(bookingType);

  const { data: rooms } = useQuery({
    queryKey: ["rooms"],
    queryFn: () => roomsApi.list().then((r) => r.data),
  });

  // 「创建并确认排房」按钮置位；onFinish 读取后并入 payload.auto_confirm。
  // 用 ref 而非 state：点击 → form.submit() 是同步链，无需重渲染。
  const autoConfirmRef = useRef(false);
  const markSeen = useNewOrderStore((s) => s.markSeen);

  const createMutation = useMutation({
    // 同客多间「自动拆单」：payloads 为 N 张单房 payload，逐张顺序建单。兄弟单已带
    // allow_duplicate，只有第一张可能触发「同客重复单」拦截（见 mutateWithDupConfirm）。
    // markSeen 在 helper 的循环内逐单调（不是等 onSuccess）——否则多房单建到一半时
    // 新订单轮询醒来，会把已建好的单弹给刚录完它的前台自己。
    mutationFn: (payloads: any[]) => createOrdersAndMarkSeen(payloads, markSeen),
    onSuccess: (_data, payloads: any[]) => {
      const n = payloads?.length ?? 1;
      message.success(
        n > 1
          ? `已按房间拆成 ${n} 张订单创建成功`
          : payloads?.[0]?.auto_confirm
            ? "订单已创建并确认排房"
            : "订单创建成功"
      );
      qc.invalidateQueries({ queryKey: ["orders"] });
      qc.invalidateQueries({ queryKey: ["rooms"] });
      qc.invalidateQueries({ queryKey: ["staff", "calendar"] });
      router.push("/orders");
    },
  });

  // 同客同日期重复单：后端 409（duplicate_order）拦下 → 弹确认，
  // 用户确认「同名不同客」后第一张也带 allow_duplicate 重发整批。
  const mutateWithDupConfirm = (payloads: any[]) =>
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
            createMutation.mutate(
              payloads.map((p) => ({ ...p, allow_duplicate: true }))
            ),
        });
      },
    });

  const onFinish = (values: any) => {
    // Multi-room: 把每行 RoomFormRow 转成后端 OrderRoomCreate（与甘特图快速建单共用同一逻辑）
    const { rooms, totalActual } = roomFormRowsToPayload(values.rooms as RoomFormRow[]);
    const autoConfirm = autoConfirmRef.current;
    autoConfirmRef.current = false;

    if (rooms.length === 0) {
      message.error("请至少添加一个房间");
      return;
    }

    // 每房净房费（多房平台单手填，B 方案 2026-07-04）：仅「平台 + 多于1间」生效。
    // 要么全填要么全不填；全填时整单净房费 = Σ每房（后端 422 兜底）。
    const submitIsPlatform = PLATFORM_CHANNELS.has(values.channel);
    const perRoomActive = perRoomNetActive(submitIsPlatform, rooms.length);
    const { anyNet, allNet, sum: netSum } = summarizePerRoomNets(rooms, perRoomActive);
    if (anyNet && !allNet) {
      message.error("每房净房费要么全填要么全不填（缺的房间也要填）");
      return;
    }
    // 不生效时（非平台/单房）剥离残留的每房净值，避免切渠道/删房后旧值泄漏进 payload。
    if (!perRoomActive) rooms.forEach((r) => { r.ota_owner_revenue = null; });

    const payload = {
      auto_confirm: autoConfirm || undefined,
      channel: values.channel,
      platform_order_id: values.platform_order_id || null,
      guest_name: values.guest_name,
      guest_phone: values.guest_phone || null,
      booking_type: (values.booking_type || "normal") as BookingType,
      rooms,
      actual_price: totalActual,
      deposit: 0, // 押金已下线（王总 2026-07-22）：走线下 POS + 飞书小票，建单不再录押金
      // 平台渠道填「到手价」→ 后端倒推佣金率;非平台/未填不带。(#44 口径重做 2026-07-01)
      // 每房手填时整单值 = Σ每房，覆盖整单输入框里的值。
      ota_owner_revenue: anyNet
        ? netSum
        : submitIsPlatform && values.ota_owner_revenue != null
          ? values.ota_owner_revenue
          : undefined,
      notes: values.notes || null,
    };

    // 防呆：平台单「净房费 > 房费」几乎一定是填反了 → 弹确认，不硬拦（留负佣金边界）。
    const reversedWarn = perRoomOrOrderReversedWarning({
      isPlatform: submitIsPlatform,
      perRoomActive: anyNet,
      rooms,
      orderOwnerRevenue: values.ota_owner_revenue,
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
  const availableCount = (rooms as any[])?.filter((r: any) => r.room_status === "available").length ?? 0;

  return (
    <div style={{ maxWidth: 760 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 24 }}>
        <Button icon={<ArrowLeftOutlined />} type="text" onClick={() => router.back()} />
        <div>
          <Title level={4} style={{ margin: 0 }}>新建订单</Title>
          <Text type="secondary">录入新的民宿预订</Text>
        </div>
      </div>

      {createMutation.isError && (
        <Alert
          type="error"
          message={extractErrorMessage(createMutation.error)}
          style={{ marginBottom: 16, borderRadius: 8 }}
          showIcon closable
        />
      )}

      <Form
        form={form}
        layout="vertical"
        onFinish={onFinish}
        initialValues={{
          channel: "self_acquired",
          booking_type: "normal",
          discount_amount: 0,
          deposit: 0,
          // 多房：默认起一行待填的房间
          rooms: [{ guests_count: 0 }],
        }}
        onValuesChange={(changed, _all) => {
          if (changed.channel) setChannel(changed.channel);
          // issue#103 Step 3: 客人价默认带入实收，仅当该行 list_price 为空时（不覆盖用户已填值）
          if (changed.rooms) syncRoomRowsListPrice(changed.rooms as any[], form);
        }}
      >
        {/* 来源信息 */}
        <Card bordered={false}
          style={{ borderRadius: 12, boxShadow: "0 2px 8px rgba(0,0,0,0.06)", marginBottom: 16 }}
          title={<Space><HomeOutlined style={{ color: "#1677ff" }} /><Text strong>来源信息</Text></Space>}>
          <Row gutter={16}>
            {/* issue#3: 平台订单号已删除（携程等平台用客人姓名+手机号即可查到） */}
            <Col xs={24} sm={12}>
              <Form.Item name="channel" label="预订渠道" rules={[{ required: true }]}>
                <Select options={CHANNELS} size="large" />
              </Form.Item>
            </Col>
            {/* issue#6: 订单类型 */}
            <Col xs={24} sm={12}>
              <Form.Item
                name="booking_type"
                label="订单类型"
                tooltip="试住/自住单业主出钱，按业主合同里设置的对应分成比例计算业主应付。月底业主结算时自动扣减。"
                rules={[{ required: true, message: "请选择订单类型" }]}
              >
                <Radio.Group size="large" buttonStyle="solid">
                  <Radio.Button value="normal">普通</Radio.Button>
                  <Radio.Button value="trial">试住</Radio.Button>
                  <Radio.Button value="owner_self">自住</Radio.Button>
                </Radio.Group>
              </Form.Item>
            </Col>
          </Row>
        </Card>

        {/* 客人信息 */}
        <Card bordered={false}
          style={{ borderRadius: 12, boxShadow: "0 2px 8px rgba(0,0,0,0.06)", marginBottom: 16 }}
          title={<Space><UserOutlined style={{ color: "#52c41a" }} /><Text strong>客人信息</Text></Space>}>
          <Row gutter={16}>
            <Col xs={24} sm={12}>
              <Form.Item name="guest_name" label="客人姓名" rules={[{ required: true, message: "请输入客人姓名" }]}>
                <MobileInput prefix={<UserOutlined style={{ color: "#bfbfbf" }} />} placeholder="真实姓名" size="large" />
              </Form.Item>
            </Col>
            <Col xs={24} sm={12}>
              <Form.Item name="guest_phone" label="手机号"
                rules={[
                  // 手机号选填（前台先录单后补，后端同步放行空值）。
                  // 填了就必须是 11 位中国大陆手机号。
                  {
                    validator: (_, value) => {
                      if (!value) return Promise.resolve();
                      if (/^1[3-9]\d{9}$/.test(value)) return Promise.resolve();
                      return Promise.reject(new Error("请输入有效的 11 位手机号"));
                    },
                  },
                ]}>
                <MobileInput
                  prefix={<PhoneOutlined style={{ color: "#bfbfbf" }} />}
                  placeholder="13800138000"
                  size="large"
                  maxLength={11}
                  inputMode="tel"
                  allowClear
                />
              </Form.Item>
            </Col>
          </Row>
        </Card>

        {/* 房间信息（多房）*/}
        <Card bordered={false}
          style={{ borderRadius: 12, boxShadow: "0 2px 8px rgba(0,0,0,0.06)", marginBottom: 16 }}
          title={
            <Space>
              <CalendarOutlined style={{ color: "#722ed1" }} />
              <Text strong>房间信息</Text>
              <Text type="secondary" style={{ fontSize: 12, fontWeight: 400 }}>
                · 当前空置 {availableCount} 套，可加多间房
              </Text>
            </Space>
          }>
          {/* allowPastDates：补录老单要能直接填过去的入住日（后端建单本就放行，见
              test_create_order_with_past_dates_allowed）。别指望「先建今天再改」——
              编辑守卫会拦活单改到过去。 */}
          <OrderRoomsField form={form} rooms={rooms as RoomOut[] | undefined} phonePriceExempt={phonePriceExempt} showPerRoomNetFee={isPlatform} allowPastDates />
        </Card>

        {/* 押金 / 佣金 / 总览 */}
        <Card bordered={false}
          style={{ borderRadius: 12, boxShadow: "0 2px 8px rgba(0,0,0,0.06)", marginBottom: 16 }}
          title={<Space><DollarOutlined style={{ color: "#fa8c16" }} /><Text strong>金额信息</Text></Space>}>
          {/* 押金已下线（王总 2026-07-22）：改走线下 POS + 飞书小票，建单不再录押金 */}
          <Row gutter={16}>
            {isPlatform && (
              <Col xs={24} sm={24}>
                <Form.Item
                  name="ota_owner_revenue"
                  label="净房费 · 平台结给我们 (¥)"
                  tooltip="平台扣完佣金后实际结给我们的钱（到手）。填这个即可，系统自动算佣金，无需手填佣金率。留空则不计佣金。净房费应小于房费，差额=平台佣金。多房单如各房到手不同，可在上方房间行分别手填「本房净房费」，提交时整单净房费自动 = 各房之和（此框的值以各房之和为准）。"
                >
                  <MobileInputNumber style={{ width: "100%" }} size="large" min={0} precision={2} inputMode="decimal" placeholder="平台结算的净房费" controls={false} />
                </Form.Item>
              </Col>
            )}
          </Row>

          {/* 总实收汇总（派生）*/}
          <Form.Item noStyle shouldUpdate={(p, c) => p.rooms !== c.rooms}>
            {() => {
              const formRooms = (form.getFieldValue("rooms") || []) as RoomFormRow[];
              const total = formRooms.reduce((acc, r) => acc + (r?.actual_price ?? 0), 0);
              const totalNights = formRooms.reduce((acc, r) => {
                if (r?.dates?.[0] && r?.dates?.[1]) return acc + r.dates[1].diff(r.dates[0], "day");
                return acc;
              }, 0);
              if (total === 0) return null;
              return (
                <div style={{ background: "#f6ffed", border: "1px solid #b7eb8f", borderRadius: 8, padding: "8px 14px" }}>
                  <Text style={{ fontSize: 13, color: "#52c41a" }}>
                    订单合计：¥{total.toLocaleString()}{totalNights > 0 && `（共 ${totalNights} 间夜）`}
                  </Text>
                </div>
              );
            }}
          </Form.Item>
        </Card>

        {/* 备注 */}
        <Card bordered={false}
          style={{ borderRadius: 12, boxShadow: "0 2px 8px rgba(0,0,0,0.06)", marginBottom: 24 }}>
          <Form.Item name="notes" label="备注" style={{ marginBottom: 0 }}>
            <TextArea rows={3} placeholder="特殊要求、注意事项..." showCount maxLength={500} />
          </Form.Item>
        </Card>

        <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
          <Button size="large" style={{ flex: 1, minWidth: 88 }} onClick={() => router.back()}>取消</Button>
          <Button size="large" style={{ flex: 1.5, minWidth: 132 }} icon={<SaveOutlined />}
            htmlType="submit" loading={createMutation.isPending}>
            仅创建订单
          </Button>
          <Button type="primary" size="large" style={{ flex: 2, minWidth: 168 }}
            loading={createMutation.isPending}
            onClick={() => {
              // 电话单主路径：创建后直接推进「确认订单+确认排房」，省去回列表点两次流转。
              // 有未选房的行时后端只推到「待排房」，同样省一步。
              autoConfirmRef.current = true;
              form.submit();
            }}>
            创建并确认排房
          </Button>
        </div>
      </Form>
    </div>
  );
}
