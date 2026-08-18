"use client";

import React, { useEffect } from "react";
import { extractErrorMessage } from "@/lib/api-errors";
import {
  Modal,
  Form,
  Row,
  Col,
  Input,
  Select,
  Button,
  Typography,
  message,
  Tooltip,
} from "antd";
import { EditOutlined, QuestionCircleOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import dayjs, { Dayjs } from "dayjs";
import { ordersApi, roomsApi } from "@/lib/api";
import { useIsMobile } from "@/lib/responsive";
import { ACTIVE_CHANNELS, CHANNEL_META, PLATFORM_CHANNELS } from "@/lib/channels";
import type { OrderRoomCreate } from "@/lib/types";
import { isPhonePriceExempt } from "@/lib/types";
import { MobileInputNumber } from "@/components/ui/MobileInputNumber";
import { MobileInput } from "@/components/ui/MobileInput";
import { perRoomOrOrderReversedWarning } from "@/lib/caliber";
import { summarizePerRoomNets, perRoomNetActive, roomFormRowsToPayload, editRoomAddBlockMessage, type RoomFormRow } from "@/lib/order-rooms";
import { OrderRoomsField } from "@/components/orders/OrderRoomsField";
import { invalidateOrderWithAudit } from "@/lib/order-cache";

const { Text } = Typography;

// 共用全局渠道字典，禁止再硬编码。新渠道只改 lib/channels.ts。
const CHANNEL_OPTIONS = ACTIVE_CHANNELS.map((c) => ({ value: c.code, label: c.label }));

const DEPOSIT_STATUS_OPTIONS = [
  { value: "not_collected", label: "未收" },
  { value: "collected", label: "已收" },
  { value: "returned", label: "已退" },
  { value: "withheld", label: "已扣" },
];

interface Props {
  open: boolean;
  order: any;
  onClose: () => void;
}

export default function EditOrderModal({ open, order, onClose }: Props) {
  const isMobile = useIsMobile();
  const [form] = Form.useForm();
  // 平台渠道才显示净房费输入 (#44)
  const watchedChannel = Form.useWatch("channel", form);
  const isPlatform = PLATFORM_CHANNELS.has(watchedChannel ?? "");
  // 金额汇总条派生值：房费合计 / 佣金 = 合计 − 净房费。
  // 净房费默认整单一个数；多房平台单可按房手填（B 方案 2026-07-04），
  // 此时整单净房费 = Σ每房，只读派生（佣金率倒推入口保持唯一）。
  const watchedRooms = Form.useWatch("rooms", form) as RoomFormRow[] | undefined;
  const watchedNetFee = Form.useWatch("ota_owner_revenue", form) as number | null | undefined;
  const totalRoomFee = (watchedRooms || []).reduce((acc, r) => acc + (r?.actual_price ?? 0), 0);
  // 每房净房费仅「平台 + 多于1间」生效（B 方案 2026-07-04）；删房到 1 间自动回退整单口径。
  const perRoomActive = perRoomNetActive(isPlatform, (watchedRooms || []).length);
  const { anyNet: anyPerRoomNet, allNet: allPerRoomNet, sum: perRoomNetSum } =
    summarizePerRoomNets(watchedRooms, perRoomActive);
  // 有每房手填值 → 整单净房费以 Σ 为准（全填才有意义）；否则用整单输入框的值
  const effectiveNetFee = anyPerRoomNet
    ? perRoomNetSum
    : (watchedNetFee != null ? Number(watchedNetFee) : null);
  const commission =
    isPlatform && effectiveNetFee != null && totalRoomFee > 0
      ? totalRoomFee - effectiveNetFee
      : null;
  // 订单类型在编辑弹窗里不可改，取自 order；试住/自住单豁免实收必填 → 不显示星号。
  const phonePriceExempt = isPhonePriceExempt(order?.booking_type);
  const queryClient = useQueryClient();

  const { data: rooms } = useQuery({
    queryKey: ["rooms"],
    queryFn: () => roomsApi.list().then((r) => r.data),
    enabled: open,
    staleTime: 30 * 1000,
  });

  useEffect(() => {
    if (open && order) {
      const orderRooms: any[] = Array.isArray(order.rooms) && order.rooms.length > 0
        ? order.rooms
        : [{
            room_id: order.room_id,
            check_in_date: order.check_in_date,
            check_out_date: order.check_out_date,
            list_price: order.list_price,
            actual_price: order.actual_price,
            guests_count: 0,
          }];
      form.setFieldsValue({
        guest_name: order.guest_name,
        guest_phone: order.guest_phone || "",
        channel: order.channel,
        // 订单类型不可改、无可见字段；写进表单是给 OrderRoomsField 的
        // 必填校验器读的（试住/自住豁免房费必填；客人实付已全局选填）。
        booking_type: order.booking_type,
        // 平台单「到手价」预填自 expected_revenue(=metadata.ota_owner_revenue);
        // 已同步单可见并可更正,佣金率由后端自动倒推,不再手填 (#44 口径重做 2026-07-01)
        ota_owner_revenue:
          order.expected_revenue != null ? Number(order.expected_revenue) : null,
        deposit: order.deposit != null ? Number(order.deposit) : 0,
        deposit_status: order.deposit_status || "not_collected",
        notes: order.notes || "",
        rooms: orderRooms.map((r) => ({
          room_id: r.room_id || null,
          dates: [
            r.check_in_date ? dayjs(r.check_in_date) : null,
            r.check_out_date ? dayjs(r.check_out_date) : null,
          ],
          list_price: r.list_price != null ? Number(r.list_price) : null,
          actual_price: r.actual_price != null ? Number(r.actual_price) : null,
          guests_count: r.guests_count ?? 0,
          // 每房净房费回填（手填过才有值）
          ota_owner_revenue: r.ota_owner_revenue != null ? Number(r.ota_owner_revenue) : null,
        })),
      });
    }
  }, [open, order, form]);

  const updateMutation = useMutation({
    mutationFn: (payload: any) => ordersApi.update(order.order_id, payload),
    onSuccess: () => {
      message.success("订单已更新");
      invalidateOrderWithAudit(queryClient, order.order_id);
      onClose();
    },
    onError: (e: any) => message.error(extractErrorMessage(e, "更新失败")),
  });

  const handleSubmit = (values: any) => {
    const submitIsPlatform = PLATFORM_CHANNELS.has(values.channel);
    const formRooms = (values.rooms as RoomFormRow[] || [])
      .filter((r) => r?.dates?.[0] && r?.dates?.[1]);

    if (formRooms.length === 0) {
      message.error("至少保留一个房间");
      return;
    }

    // 一间一单：编辑订单不能加房（新建才拆单；拆已存在/可能在住的老单风险高）。
    // 房数不变/减少（改价/改期/换房/删房）放行；变多则拦下，引导去新建订单。
    const originalRoomCount = (Array.isArray(order.rooms) ? order.rooms.length : 0) || 1;
    const addBlock = editRoomAddBlockMessage(originalRoomCount, formRooms.length);
    if (addBlock) {
      message.error(addBlock);
      return;
    }

    // 每房净房费仅「平台 + 多于1间」生效（B 方案 2026-07-04）：要么全填要么全不填，
    // 全填时整单净房费 = Σ每房（后端 422 兜底）。删房到 1 间 / 非平台自动回退整单口径。
    const perRoomActive = perRoomNetActive(submitIsPlatform, formRooms.length);
    const { anyNet, allNet, sum: netSum } = summarizePerRoomNets(formRooms, perRoomActive);
    if (anyNet && !allNet) {
      message.error("每房净房费要么全填要么全不填（缺的房间也要填）");
      return;
    }

    // 行→payload 映射走三端共享 helper（口径单一来源）；编辑单额外语义：
    // 每房净值只在「生效」时才带，不生效显式送 null 清掉后端残留（切渠道/删房到1间）。
    const { rooms: roomsPayload, totalActual } = roomFormRowsToPayload(formRooms);
    if (!anyNet) roomsPayload.forEach((r) => { r.ota_owner_revenue = null; });

    const payload: any = {
      guest_name: values.guest_name?.trim(),
      guest_phone: values.guest_phone?.trim() || null,
      channel: values.channel,
      // 平台渠道提交「到手价」→ 后端写 metadata.ota_owner_revenue 并倒推佣金率;
      // 非平台渠道或未填时不带该字段(后端不动佣金率)。(#44 口径重做 2026-07-01)
      // 每房手填时整单值 = Σ每房，覆盖整单输入框里的旧值。
      ota_owner_revenue: anyNet
        ? netSum
        : submitIsPlatform && values.ota_owner_revenue != null
          ? values.ota_owner_revenue
          : undefined,
      rooms: roomsPayload,
      actual_price: totalActual,
      deposit: values.deposit,
      deposit_status: values.deposit_status,
      notes: values.notes?.trim() || null,
    };

    // 防呆：平台单「净房费 > 房费」几乎一定是填反了 → 弹确认，不硬拦（留负佣金边界）。
    const reversedWarn = perRoomOrOrderReversedWarning({
      isPlatform: submitIsPlatform,
      perRoomActive: anyNet,
      rooms: roomsPayload,
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
        onOk: () => updateMutation.mutate(payload),
      });
      return;
    }
    updateMutation.mutate(payload);
  };

  if (!order) return null;

  return (
    <Modal
      open={open}
      onCancel={onClose}
      title={
        <span>
          <EditOutlined style={{ marginRight: 8 }} />
          修改订单 #{order.order_id}
        </span>
      }
      footer={null}
      // 移动端用全宽 + 顶部小留白，避免默认 520px 横向溢出
      width={isMobile ? "calc(100vw - 16px)" : 760}
      style={isMobile ? { top: 12, paddingBottom: 0, maxWidth: "100vw" } : undefined}
      destroyOnHidden
    >
      <Form form={form} layout="vertical" onFinish={handleSubmit}>
        <Row gutter={16}>
          <Col xs={24} sm={12}>
            <Form.Item
              name="guest_name"
              label="客人姓名"
              rules={[{ required: true, message: "请输入客人姓名" }]}
            >
              <MobileInput size="large" placeholder="如：张先生" />
            </Form.Item>
          </Col>
          <Col xs={24} sm={12}>
            <Form.Item
              name="guest_phone"
              label="客人电话"
              rules={[
                {
                  pattern: /^1[3-9]\d{9}$/,
                  message: "请输入正确的 11 位手机号",
                  validateTrigger: "onSubmit",
                  transform: (v) => (v ? v.trim() : v),
                },
              ]}
            >
              <MobileInput size="large" inputMode="tel" placeholder="选填，11 位手机号" />
            </Form.Item>
          </Col>
        </Row>

        <Row gutter={16}>
          <Col xs={24} sm={12}>
            <Form.Item
              name="channel"
              label="渠道"
              rules={[{ required: true }]}
            >
              <Select
                size="large"
                options={(() => {
                  const current = order?.channel as string | undefined;
                  if (!current || CHANNEL_OPTIONS.some((o) => o.value === current)) {
                    return CHANNEL_OPTIONS;
                  }
                  // 老订单 channel 是 legacy 值（如 meituan/tujia/private/walk_in/direct），
                  // 下拉里临时显示一项保持可见，但仍允许改成 active 渠道。
                  return [
                    ...CHANNEL_OPTIONS,
                    { value: current, label: CHANNEL_META[current]?.label ?? current },
                  ];
                })()}
              />
            </Form.Item>
          </Col>
        </Row>

        {/* Multi-room: rooms 整体替换。每行可改/可删；不可加房（一间一单，allowAdd=false）。
            与 /orders/new、QuickCreateOrderModal 共用 OrderRoomsField——录单口径改一处三端生效。
            编辑态四件套：排除本单占用的可订禁选 / 允许选历史日期 / 删房带确认预览 / 禁止加房。 */}
        <Form.Item
          label={<Text strong>房间信息</Text>}
          style={{ marginBottom: 8 }}
          extra="一间一单：编辑不能加房。要为该客人增加房间请「新建订单」，退房互不影响。"
        >
          <OrderRoomsField
            form={form}
            rooms={rooms as any}
            phonePriceExempt={phonePriceExempt}
            showPerRoomNetFee={isPlatform}
            excludeOrderId={order?.order_id}
            allowPastDates
            confirmRoomRemoval
            allowAdd={false}
          />
        </Form.Item>

        {/* 金额汇总条：房费合计 +（平台单）净房费 → 佣金自动算。三格统一「小标签在上、值在下」，
            值行高 40 与输入框对齐。净房费默认整单一个数；多房平台单可在房间行按房手填
            （B 方案 2026-07-04），此时整单值只读 = Σ每房。 */}
        <div
          style={{
            background: "var(--sand)",
            border: "0.5px solid var(--linen)",
            borderRadius: 12,
            padding: "12px 16px",
            marginBottom: 16,
          }}
        >
          <Row gutter={[12, 8]} align="top">
            <Col xs={12} sm={isPlatform ? 7 : 24}>
              <div style={{ fontSize: 12, color: "var(--stone)", marginBottom: 4 }}>房费合计</div>
              <div
                style={{
                  fontFamily: "var(--font-serif)",
                  fontSize: 20,
                  color: "var(--ink)",
                  height: 40,
                  display: "flex",
                  alignItems: "center",
                }}
              >
                ¥{totalRoomFee.toLocaleString()}
              </div>
            </Col>
            {isPlatform && (
              <>
                <Col xs={24} sm={10} order={isMobile ? 3 : 2}>
                  <div style={{ fontSize: 12, color: "var(--stone)", marginBottom: 4 }}>
                    净房费 · 平台结给我们 (¥)
                    <Tooltip title={anyPerRoomNet
                      ? "已按房手填净房费，整单净房费自动 = 各房之和，不再单独填。要改整单口径请先清空各房的净房费。"
                      : "平台扣完佣金后实际结给我们的钱（到手）。整单一个数；多房单如各房到手不同，可在上方房间行分别手填，此处会自动变为各房之和。填这个即可，系统自动算佣金，无需手填佣金率。留空则不计佣金。净房费应小于房费合计，差额=平台佣金。"}>
                      <QuestionCircleOutlined style={{ marginLeft: 4, color: "var(--driftwood)" }} />
                    </Tooltip>
                  </div>
                  {anyPerRoomNet ? (
                    // 每房手填后整单值只读派生 = Σ每房（缺行时显示待补齐提示）
                    <div
                      style={{
                        fontFamily: "var(--font-serif)",
                        fontSize: 20,
                        color: allPerRoomNet ? "var(--ink)" : "var(--clay)",
                        height: 40,
                        display: "flex",
                        alignItems: "center",
                        gap: 8,
                      }}
                    >
                      {allPerRoomNet && perRoomNetSum != null
                        ? `¥${perRoomNetSum.toFixed(2)}`
                        : "各房还有未填"}
                      <span style={{ fontSize: 12, color: "var(--stone)", fontFamily: "inherit" }}>
                        = Σ每房净房费
                      </span>
                    </div>
                  ) : (
                    <Form.Item name="ota_owner_revenue" noStyle>
                      <MobileInputNumber
                        style={{ width: "100%" }}
                        size="large"
                        min={0}
                        precision={2}
                        inputMode="decimal"
                        placeholder="平台结算的净房费"
                        controls={false}
                      />
                    </Form.Item>
                  )}
                </Col>
                <Col xs={12} sm={7} order={isMobile ? 2 : 3}>
                  <div style={{ fontSize: 12, color: "var(--stone)", marginBottom: 4 }}>佣金 · 自动倒推</div>
                  <div
                    style={{
                      fontFamily: "var(--font-serif)",
                      fontSize: 20,
                      color: commission != null && commission < 0 ? "var(--clay)" : "var(--ink)",
                      height: 40,
                      display: "flex",
                      alignItems: "center",
                    }}
                  >
                    {commission != null ? `¥${commission.toFixed(2)}` : "—"}
                  </div>
                </Col>
              </>
            )}
          </Row>
        </div>

        <Row gutter={16}>
          <Col xs={24} sm={12}>
            <Form.Item name="deposit" label="押金 (¥)">
              <MobileInputNumber
                size="large"
                style={{ width: "100%" }}
                min={0}
                precision={2}
                prefix="¥"
                inputMode="decimal"
                controls={false}
              />
            </Form.Item>
          </Col>
          <Col xs={24} sm={12}>
            <Form.Item name="deposit_status" label="押金状态">
              <Select size="large" options={DEPOSIT_STATUS_OPTIONS} />
            </Form.Item>
          </Col>
        </Row>

        <Form.Item name="notes" label="备注">
          <Input.TextArea rows={3} placeholder="选填" />
        </Form.Item>

        <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
          <Button onClick={onClose} disabled={updateMutation.isPending}>
            取消
          </Button>
          <Button
            type="primary"
            htmlType="submit"
            loading={updateMutation.isPending}
            icon={<EditOutlined />}
          >
            保存修改
          </Button>
        </div>
      </Form>
    </Modal>
  );
}
