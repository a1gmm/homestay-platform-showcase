"use client";

import React from "react";
import { useQuery } from "@tanstack/react-query";
import { roomsApi } from "@/lib/api";
import { Form, Select, DatePicker, Button, Row, Col, Typography, Tag, Tooltip, Popconfirm } from "antd";
import type { RoomOut } from "@/lib/types";
import { isPhonePriceExempt } from "@/lib/types";
import { MobileInputNumber } from "@/components/ui/MobileInputNumber";
import { PlusOutlined, DeleteOutlined, QuestionCircleOutlined } from "@ant-design/icons";
import dayjs, { Dayjs } from "dayjs";
import { roomNights, prorateOrderNet, type RoomFormRow } from "@/lib/order-rooms";
import { ROOM_STATUS_COLOR, ROOM_STATUS_LABEL } from "@/lib/status-display";

const { Text } = Typography;
const { RangePicker } = DatePicker;

/** 单行选房下拉：按本行日期实时查可订房，不可订的禁选。
 *  excludeOrderId：编辑单场景传本单 id，本单自己的占用不算冲突。 */
function RoomSelectField({
  name, restField, rooms, form, excludeOrderId,
}: {
  name: number;
  restField: any;
  rooms: RoomOut[] | undefined;
  form: any;
  excludeOrderId?: string;
}) {
  const rowDates = Form.useWatch(["rooms", name, "dates"], form) as
    | [Dayjs | null, Dayjs | null]
    | undefined;
  const rci = rowDates?.[0]?.format("YYYY-MM-DD");
  const rco = rowDates?.[1]?.format("YYYY-MM-DD");
  const { data: rowAvail } = useQuery({
    queryKey: ["rooms", "availability", rci, rco, excludeOrderId ?? null],
    queryFn: () => roomsApi.availabilityList(rci!, rco!, excludeOrderId).then((r) => r.data),
    enabled: !!rci && !!rco,
  });
  const rowAvailSet = React.useMemo(
    () => new Set(rowAvail?.available_room_ids ?? []),
    [rowAvail]
  );

  return (
    <Form.Item {...restField} name={[name, "room_id"]} label="房间" style={{ marginBottom: 8 }}>
      <Select
        allowClear
        size="large"
        placeholder="暂不分配（待排房）"
        showSearch
        optionFilterProp="label"
        options={[
          { value: "", label: "暂不分配（待排房）" },
          ...(rooms || []).map((r) => ({
            value: r.room_id,
            label: `${r.room_id} · ${r.room_name}`,
            disabled: (rci && rco) ? !rowAvailSet.has(r.room_id) : false,
            room: r,
          })),
        ]}
        optionRender={(option) => {
          const r = (option.data as { room?: RoomOut }).room;
          if (!r) return <Text type="secondary">{option.label}</Text>;
          return (
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <span>
                <Text strong>{r.room_id}</Text>
                <Text type="secondary" style={{ marginLeft: 8, fontSize: 12 }}>{r.room_name}</Text>
              </span>
              <Tag color={ROOM_STATUS_COLOR[r.room_status]} style={{ fontSize: 11 }}>
                {ROOM_STATUS_LABEL[r.room_status]}
              </Tag>
            </div>
          );
        }}
      />
    </Form.Item>
  );
}

/**
 * 多房房间行（Form.List name="rooms"）—— /orders/new 完整页与甘特图
 * QuickCreateOrderModal 共用同一套录入逻辑，避免两份各自维护。
 *
 * 父级需提供 `form`、房间列表 `rooms`、以及 `phonePriceExempt`（试住/自住单豁免必填星号）。
 * 校验器内部直接读 form 的 booking_type，故无需额外联动。
 */
export function OrderRoomsField({
  form, rooms, phonePriceExempt, showPerRoomNetFee = false,
  excludeOrderId, allowPastDates = false, confirmRoomRemoval = false,
  allowAdd = true,
}: {
  form: any;
  rooms: RoomOut[] | undefined;
  phonePriceExempt: boolean;
  // 平台渠道多房单：房间行显示「本房净房费」手填框（B 方案 2026-07-04）。
  // 父级按渠道传入；非平台渠道恒 false，行为与旧版完全一致。
  showPerRoomNetFee?: boolean;
  // ↓ 编辑单（EditOrderModal）专用三件套；新建单一律走默认值。
  // 编辑单查可订房要排除本单自己的占用，否则本单的房会被误禁选。
  excludeOrderId?: string;
  // 需要能选过去的日期：编辑历史/在住单，以及**新建时补录老单**（建单页/甘特快捷建单
  // 都传 true）。建单不放开的话补录无路可走——「先建今天再改成过去」会被编辑守卫拦死
  // （orders.py「入住日期不能早于今天」），后端建单本身是放行过去日期的。
  allowPastDates?: boolean;
  // 编辑已有订单删房是破坏性动作，套 Popconfirm 预览要删哪间。
  confirmRoomRemoval?: boolean;
  // 「添加房间」按钮开关。新建/快速建单允许加房（多间后自动拆成多张单）；
  // 编辑单传 false 隐藏——一间一单，编辑不加房（见 editRoomAddBlockMessage）。
  allowAdd?: boolean;
}) {
  return (
    <Form.List name="rooms">
      {(fields, { add, remove }) => (
        <>
          {fields.map(({ key, name, ...restField }) => (
            <div
              key={key}
              style={{
                border: "0.5px solid var(--linen)",
                borderRadius: 8,
                padding: 12,
                marginBottom: 12,
                background: "var(--sand)",
              }}
            >
              {/* 第一行：房间 + 入住退房（各占一半，标签不会被挤窄）。
                  断点用 md：576-767px 视口下 sm 两列会把日期列压到 260px 以下裁剪 */}
              <Row gutter={12}>
                <Col xs={24} md={12}>
                  <RoomSelectField
                    name={name} restField={restField} rooms={rooms} form={form}
                    excludeOrderId={excludeOrderId}
                  />
                </Col>
                <Col xs={24} md={12}>
                  <Form.Item
                    {...restField}
                    name={[name, "dates"]}
                    label="入住 / 退房"
                    rules={[
                      { required: true, message: "请选日期" },
                      {
                        validator: (_, value) =>
                          !value?.[0] || !value?.[1] || value[1].isAfter(value[0])
                            ? Promise.resolve()
                            : Promise.reject(new Error("退房须晚于入住")),
                      },
                    ]}
                    style={{ marginBottom: 8 }}
                  >
                    <RangePicker
                      style={{ width: "100%" }}
                      size="large"
                      placeholder={["入住", "退房"]}
                      disabledDate={allowPastDates ? undefined : (d) => d.isBefore(dayjs().subtract(1, "day"))}
                      format="YYYY-MM-DD"
                    />
                  </Form.Item>
                </Col>
              </Row>
              {/* 第二行：客人实付 + 晚数（只读）+ 本房房费 + 删除 */}
              <Row gutter={12}>
                <Col xs={12} sm={7}>
                  <Form.Item
                    {...restField}
                    name={[name, "list_price"]}
                    label="客人实付 (¥)"
                    tooltip="客人实际支付的价格；OTA 单填平台订单金额。选填，默认带入房费。"
                    style={{ marginBottom: 8 }}
                  >
                    <MobileInputNumber
                      style={{ width: "100%" }}
                      size="large"
                      min={0}
                      precision={2}
                      placeholder="0.00"
                      inputMode="decimal"
                      controls={false}
                    />
                  </Form.Item>
                </Col>
                <Col xs={12} sm={4}>
                  <Form.Item noStyle shouldUpdate={(p, c) => p.rooms?.[name]?.dates !== c.rooms?.[name]?.dates}>
                    {() => {
                      const r = form.getFieldValue(["rooms", name]) as RoomFormRow | undefined;
                      const n = roomNights(r?.dates);
                      return (
                        <Form.Item label="晚数" style={{ marginBottom: 8 }}>
                          {/* 无框文字展示，数字用衬线（岸屿：价格/数字 serif），与旁边 40px 输入框对齐 */}
                          <div style={{ height: 40, display: "flex", alignItems: "baseline", gap: 4 }}>
                            {n != null ? (
                              <>
                                <span style={{ fontFamily: "var(--font-serif)", fontSize: 20, color: "var(--ink)", lineHeight: "40px" }}>
                                  {n}
                                </span>
                                <span style={{ fontSize: 13, color: "var(--stone)" }}>晚</span>
                              </>
                            ) : (
                              <span style={{ color: "var(--driftwood)", lineHeight: "40px" }}>—</span>
                            )}
                          </div>
                        </Form.Item>
                      );
                    }}
                  </Form.Item>
                </Col>
                <Col xs={12} sm={7}>
                  <Form.Item
                    {...restField}
                    name={[name, "actual_price"]}
                    label="本房房费 (¥)"
                    required={!phonePriceExempt}
                    rules={[
                      {
                        validator: (_, value) => {
                          const bt = form.getFieldValue("booking_type");
                          if (isPhonePriceExempt(bt)) return Promise.resolve();
                          if (value == null || value === "") return Promise.reject(new Error("必填"));
                          return Promise.resolve();
                        },
                      },
                    ]}
                    style={{ marginBottom: 8 }}
                  >
                    <MobileInputNumber
                      style={{ width: "100%" }}
                      size="large"
                      min={0}
                      precision={2}
                      placeholder="0.00"
                      inputMode="decimal"
                      controls={false}
                    />
                  </Form.Item>
                </Col>
                {/* 入住人无业务消费方，不再展示；隐藏注册保留原值往返，避免保存时被清零 */}
                <Form.Item {...restField} name={[name, "guests_count"]} hidden>
                  <MobileInputNumber />
                </Form.Item>
                <Col
                  xs={12}
                  sm={6}
                  style={{ display: "flex", alignItems: "flex-end", justifyContent: "flex-end", paddingBottom: 8 }}
                >
                  {fields.length > 1 && (confirmRoomRemoval ? (() => {
                    // 删除前预览：取当前那一行的 room_id / 房价，给用户看清楚要删哪一间
                    const row = (form.getFieldValue(["rooms", name]) || {}) as RoomFormRow;
                    const roomLabel = row.room_id || "待排房";
                    const priceText = row.actual_price != null
                      ? `¥${Number(row.actual_price).toLocaleString()}`
                      : "未填价";
                    return (
                      <Popconfirm
                        title="删除这一间房？"
                        description={`将从订单中移除「${roomLabel}」(${priceText})，订单合计会同步减少。保存后生效。`}
                        okText="删除"
                        okButtonProps={{ danger: true }}
                        cancelText="取消"
                        onConfirm={() => remove(name)}
                      >
                        <Button
                          type="text"
                          danger
                          icon={<DeleteOutlined />}
                          aria-label="删除此房间"
                          style={{ minWidth: 44, height: 44 }}
                        >
                          删除
                        </Button>
                      </Popconfirm>
                    );
                  })() : (
                    <Button
                      type="text"
                      danger
                      icon={<DeleteOutlined />}
                      onClick={() => remove(name)}
                      aria-label="删除此房间"
                      style={{ minWidth: 44, height: 44 }}
                    >
                      删除
                    </Button>
                  ))}
                </Col>
              </Row>
              {/* 第三行（仅平台多房单）：本房净房费。携程关联房各房到手可不同（不同促销/
                  佣金率），前台照账单填；留空则结算按房费比例分摊（占位显示分摊值）。 */}
              {showPerRoomNetFee && fields.length > 1 && (
                <Row gutter={12}>
                  <Col xs={18} sm={12}>
                    <Form.Item
                      noStyle
                      shouldUpdate={(p, c) =>
                        p.rooms !== c.rooms || p.ota_owner_revenue !== c.ota_owner_revenue
                      }
                    >
                      {() => {
                        const allRows = (form.getFieldValue("rooms") || []) as RoomFormRow[];
                        const row = allRows[name];
                        const totalFee = allRows.reduce(
                          (acc, r) => acc + (r?.actual_price ?? 0), 0);
                        const prorated = prorateOrderNet(
                          form.getFieldValue("ota_owner_revenue"), row?.actual_price, totalFee);
                        return (
                          <Form.Item
                            {...restField}
                            name={[name, "ota_owner_revenue"]}
                            label={
                              <span>
                                本房净房费 (¥)
                                <Text type="secondary" style={{ fontSize: 12, fontWeight: 400, marginLeft: 6 }}>
                                  选填 · 留空自动分摊
                                </Text>
                                <Tooltip title="默认不用填！整单净房费在上面填一个总数即可，系统会按房费自动分摊到各房。只有平台账单明确写了各房到手金额、且与自动分摊不同时，才需要在这里逐房手填（要填就几间一起填）。">
                                  <QuestionCircleOutlined style={{ marginLeft: 4, color: "var(--driftwood)" }} />
                                </Tooltip>
                              </span>
                            }
                            style={{ marginBottom: 8 }}
                          >
                            <MobileInputNumber
                              style={{ width: "100%" }}
                              size="large"
                              min={0}
                              precision={2}
                              inputMode="decimal"
                              placeholder={prorated != null ? `留空即按 ¥${prorated} 分摊` : "留空自动分摊"}
                              controls={false}
                            />
                          </Form.Item>
                        );
                      }}
                    </Form.Item>
                  </Col>
                </Row>
              )}
            </div>
          ))}
          {allowAdd && (
            <Button
              type="dashed"
              icon={<PlusOutlined />}
              onClick={() => add({ guests_count: 0 })}
              block
              size="large"
            >
              添加房间
            </Button>
          )}
        </>
      )}
    </Form.List>
  );
}
