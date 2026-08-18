"use client";

import React from "react";
import {
  Modal, Tabs, Form, Input, InputNumber, AutoComplete, DatePicker, Switch, Tag, Typography,
} from "antd";
import type { FormInstance } from "antd";
import dayjs from "dayjs";
import { roomsApi } from "@/lib/api";
import { RoomImagesPanel } from "@/components/rooms/RoomImagesPanel";
import { tokens } from "@/lib/design-tokens";
import type { RoomOut } from "@/lib/types";

const { Text } = Typography;

interface Props {
  open: boolean;
  onClose: () => void;
  editingRoom: RoomOut | null;
  roomForm: FormInstance;
  saveRoomMutation: { mutate: (payload: Record<string, unknown>) => void; isPending: boolean };
  roomTypeOptions: { value: string }[];
  isMobile: boolean;
}

// 创建 / 编辑房间 Modal —— 基本信息表单 + 图片 Tab。
// 业主关联 / 分成比例 / 费用占比已统一到 /system/share-config，这里只读展示。
export function RoomEditModal({
  open, onClose, editingRoom, roomForm, saveRoomMutation, roomTypeOptions, isMobile,
}: Props) {
  return (
    <Modal
      open={open}
      width={isMobile ? undefined : 640}
      title={editingRoom ? `编辑房间 · ${editingRoom.room_id}` : "新建房间"}
      onCancel={onClose}
      onOk={() => roomForm.submit()}
      confirmLoading={saveRoomMutation.isPending}
      okText="保存"
      cancelText="取消"
    >
      <Tabs
        items={[
          {
            key: "info",
            label: "基本信息",
            children: (
              <Form
                form={roomForm}
                layout="vertical"
                onFinish={(values) => {
                  // 业主关联 / 分成比例已挪到 /system/share-config 配置,
                  // 这里不再提交 owner_id 和 owner_share_ratio,避免覆盖在合并页改的真实值。
                  const {
                    share_ratio_preset: _spreset,
                    share_ratio_custom: _scustom,
                    owner_id: _ownerId,
                    is_disabled,
                    contract_signed_date,
                    sale_date,
                    ...rest
                  } = values as Record<string, unknown>;

                  // 日期序列化
                  const c = contract_signed_date as dayjs.Dayjs | null | undefined;
                  const s = sale_date as dayjs.Dayjs | null | undefined;

                  const payload: Record<string, unknown> = {
                    ...rest,
                    contract_signed_date: c ? c.format("YYYY-MM-DD") : null,
                    sale_date: s ? s.format("YYYY-MM-DD") : null,
                  };
                  // 停用开关:仅当开关相对初值真的变化时才提交 room_status (#45)。
                  // 否则编辑房名/价格会把 occupied/maintenance/pending_clean 等真实房态
                  // 静默冲成 available,并清掉用于"恢复上一个状态"的 previous_status。
                  if (editingRoom) {
                    const wasDisabled = editingRoom.room_status === "locked";
                    if (Boolean(is_disabled) !== wasDisabled) {
                      payload.room_status = is_disabled ? "locked" : "available";
                    }
                  }
                  saveRoomMutation.mutate(payload);
                }}
              >
                <Form.Item label="房间编号" name="room_id" rules={[{ required: true, message: "请输入房间编号" }]}>
                  <Input placeholder="例如:101, 1A" disabled={!!editingRoom} />
                </Form.Item>
                <Form.Item label="房间名称" name="room_name" rules={[{ required: true, message: "请输入房间名称" }]}>
                  <Input placeholder="例如:海景大床房" />
                </Form.Item>
                <Form.Item label="房间分组（选填）" name="room_type" tooltip="同分组的房间会在甘特图中归在一起。可输入新分组名或从下拉选择已有分组。">
                  <AutoComplete
                    options={roomTypeOptions}
                    placeholder="如：海之梦·高级海景大床房"
                    filterOption={(input, option) =>
                      (option?.value as string)?.toLowerCase().includes(input.toLowerCase())
                    }
                    allowClear
                  />
                </Form.Item>

                {/* 业主 / 分成比例 / 费用占比规则 已统一到 /system/share-config 管理。
                    此处只读展示当前值,改动请到合并页。 */}
                {editingRoom && (
                  <Form.Item label="房东业主 · 分成">
                    <div
                      style={{
                        padding: "10px 12px",
                        background: tokens.color.bg.subtle,
                        border: `1px solid ${tokens.color.bg.border}`,
                        borderRadius: 6,
                        display: "flex",
                        flexDirection: "column",
                        gap: 4,
                        fontSize: 13,
                      }}
                    >
                      <div>
                        <Text type="secondary">业主:</Text>{" "}
                        {editingRoom.owner_id ? (
                          <Tag>{editingRoom.owner_id}</Tag>
                        ) : (
                          <Text type="secondary">未关联</Text>
                        )}
                      </div>
                      <div>
                        <Text type="secondary">普通分成:</Text>{" "}
                        <Text strong>
                          {editingRoom.owner_share_ratio != null
                            ? `${(Number(editingRoom.owner_share_ratio) * 100).toFixed(0)}%`
                            : "—"}
                        </Text>
                        {"  "}
                        <Text type="secondary" style={{ marginLeft: 12 }}>试住:</Text>{" "}
                        <Text>
                          {editingRoom.share_ratio_trial != null
                            ? `${(Number(editingRoom.share_ratio_trial) * 100).toFixed(0)}%`
                            : "—"}
                        </Text>
                        {"  "}
                        <Text type="secondary" style={{ marginLeft: 12 }}>自住:</Text>{" "}
                        <Text>
                          {editingRoom.share_ratio_owner_self != null
                            ? `${(Number(editingRoom.share_ratio_owner_self) * 100).toFixed(0)}%`
                            : "—"}
                        </Text>
                      </div>
                      <a
                        href={`/system/share-config?tab=rooms&room_id=${editingRoom.room_id}`}
                        style={{ fontSize: 12, marginTop: 2 }}
                      >
                        → 去「业主与分成」页修改业主关联 / 三类比例 / 费用占比规则
                      </a>
                    </div>
                  </Form.Item>
                )}
                <Form.Item label="签约日期" name="contract_signed_date">
                  <DatePicker style={{ width: "100%" }} placeholder="选择签约日期" />
                </Form.Item>
                <Form.Item label="售卖日期" name="sale_date">
                  <DatePicker style={{ width: "100%" }} placeholder="选择售卖日期" />
                </Form.Item>
                <Form.Item
                  label="停用"
                  name="is_disabled"
                  valuePropName="checked"
                  tooltip="开启后该房间状态变为「锁定」，新订单选房列表里不会出现"
                >
                  <Switch />
                </Form.Item>
                <Form.Item label="备注" name="remarks">
                  <Input.TextArea rows={3} placeholder="（选填）" maxLength={500} showCount />
                </Form.Item>

                <Form.Item label="楼层" name="floor">
                  <InputNumber style={{ width: "100%" }} placeholder="例如:1" />
                </Form.Item>
                <Form.Item
                  label="床位数"
                  name="beds"
                  tooltip="每间房床位数。洗涤费 = 洗涤单价 × 床位数（两床房算两份）。留空默认 1。"
                >
                  <InputNumber style={{ width: "100%" }} min={1} precision={0} placeholder="例如:1（大床房）/ 2（两床房）" />
                </Form.Item>
                <Form.Item label="基础价格 (每晚)" name="base_price">
                  <InputNumber style={{ width: "100%" }} min={0} precision={2} prefix="¥" placeholder="例如:300" />
                </Form.Item>
                <Form.Item label="省份" name="province"><Input placeholder="请输入省份" /></Form.Item>
                <Form.Item label="城市" name="city"><Input placeholder="请输入城市" /></Form.Item>
                <Form.Item label="区/县" name="district"><Input placeholder="请输入区/县" /></Form.Item>
                <Form.Item label="小区名称" name="community_name"><Input placeholder="请输入小区名称" /></Form.Item>
                <Form.Item label="楼栋号" name="building_no"><Input placeholder="例如:3号楼" /></Form.Item>
                <Form.Item label="单元号" name="unit_no"><Input placeholder="例如:1单元" /></Form.Item>
              </Form>
            ),
          },
          {
            key: "images",
            label: "图片",
            disabled: !editingRoom,
            children: editingRoom ? (
              <RoomImagesPanel
                roomId={editingRoom.room_id}
                queryKey={["room-images", editingRoom.room_id]}
                api={{
                  list: () => roomsApi.images.list(editingRoom.room_id),
                  upload: (f) => roomsApi.images.upload(editingRoom.room_id, f),
                  patch: (imageId, body) => roomsApi.images.patch(editingRoom.room_id, imageId, body),
                  delete: (imageId) => roomsApi.images.delete(editingRoom.room_id, imageId),
                }}
              />
            ) : (
              <div style={{ padding: 20, color: "#7A6F5F" }}>先保存房间后才能上传图片</div>
            ),
          },
          // 「费用占比」Tab 已挪到 /system/share-config?tab=rooms 行展开里,
          // 在那边和三类分成比例同处一页,避免分散。
        ]}
      />
    </Modal>
  );
}
