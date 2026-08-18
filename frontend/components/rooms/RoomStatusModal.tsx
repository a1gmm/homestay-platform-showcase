"use client";

import React from "react";
import { Modal, Button, Select, Input, Space, Tag, Typography } from "antd";
import { RollbackOutlined } from "@ant-design/icons";
import { formatBlockRange } from "@/lib/block-dates";
import {
  ROOM_STATUS, restoreTarget, RESTORABLE_STATUSES,
} from "@/components/rooms";
import {
  BLOCK_TYPE_LABELS, BLOCK_TYPE_DISPLAY, type BlockType, type useRoomBlocking,
} from "@/hooks/useRoomBlocking";
import type { RoomOut } from "@/lib/types";

const { Text } = Typography;

interface Props {
  editRoom: RoomOut | null;
  onClose: () => void;
  newStatus: string;
  setNewStatus: (v: string) => void;
  editLocation: Record<string, string | undefined>;
  setEditLocation: React.Dispatch<React.SetStateAction<Record<string, string | undefined>>>;
  updateMutation: { mutate: (v: { id: string; data: any }) => void; isPending: boolean };
  editRoomBlocks: any[] | undefined;
  blocking: ReturnType<typeof useRoomBlocking>;
  isMobile: boolean;
}

// 修改房间状态 Modal：房态枚举切换 + 房间位置 + 快捷锁房入口 + 现有锁房记录解除。
export function RoomStatusModal({
  editRoom, onClose, newStatus, setNewStatus,
  editLocation, setEditLocation, updateMutation,
  editRoomBlocks, blocking, isMobile,
}: Props) {
  const { setBlockingRoom, setBlockType, releaseBlockMutation } = blocking;

  return (
    <Modal
      open={!!editRoom}
      onCancel={onClose}
      width={isMobile ? undefined : 520}
      title={<Text strong>修改房间状态 — {editRoom?.room_id}</Text>}
      onOk={() => editRoom && updateMutation.mutate({ id: editRoom.room_id, data: { room_status: newStatus, ...editLocation } })}
      confirmLoading={updateMutation.isPending}
      okText="保存"
      cancelText="取消"
    >
      {editRoom && (
        <div style={{ padding: "16px 0" }}>
          <Text type="secondary" style={{ display: "block", marginBottom: 12, fontSize: 13 }}>
            {editRoom.room_name} · 当前：
            <Tag color={ROOM_STATUS[editRoom.room_status]?.color} style={{ marginLeft: 4 }}>
              {ROOM_STATUS[editRoom.room_status]?.label}
            </Tag>
          </Text>
          {/* 维修/锁房：一键恢复上一个状态 */}
          {RESTORABLE_STATUSES.includes(editRoom.room_status) && (
            <Button
              block
              type="primary"
              icon={<RollbackOutlined />}
              style={{ marginBottom: 12 }}
              loading={updateMutation.isPending}
              onClick={() =>
                updateMutation.mutate({
                  id: editRoom.room_id,
                  data: { room_status: restoreTarget(editRoom.previous_status) },
                })
              }
            >
              结束{ROOM_STATUS[editRoom.room_status]?.label} · 恢复为
              {ROOM_STATUS[restoreTarget(editRoom.previous_status)]?.label ?? "空置"}
            </Button>
          )}
          <Select
            value={newStatus}
            onChange={setNewStatus}
            style={{ width: "100%" }}
            size="large"
            options={Object.entries(ROOM_STATUS).map(([k, v]) => ({ value: k, label: v.label }))}
          />
          <div style={{ marginTop: 16, borderTop: "1px solid #f0f0f0", paddingTop: 16 }}>
            <Text strong style={{ fontSize: 13, display: "block", marginBottom: 8 }}>
              房间位置（选填）
            </Text>
            <Space direction="vertical" style={{ width: "100%" }}>
              <Input placeholder="省份" value={editLocation.province} onChange={(e) => setEditLocation((prev) => ({ ...prev, province: e.target.value }))} />
              <Input placeholder="城市" value={editLocation.city} onChange={(e) => setEditLocation((prev) => ({ ...prev, city: e.target.value }))} />
              <Input placeholder="区/县" value={editLocation.district} onChange={(e) => setEditLocation((prev) => ({ ...prev, district: e.target.value }))} />
              <Input placeholder="小区名称" value={editLocation.community_name} onChange={(e) => setEditLocation((prev) => ({ ...prev, community_name: e.target.value }))} />
              <Space style={{ width: "100%" }}>
                <Input placeholder="楼栋号" value={editLocation.building_no} onChange={(e) => setEditLocation((prev) => ({ ...prev, building_no: e.target.value }))} />
                <Input placeholder="单元号" value={editLocation.unit_no} onChange={(e) => setEditLocation((prev) => ({ ...prev, unit_no: e.target.value }))} />
              </Space>
            </Space>
          </div>

          {/* 快捷锁房（按时间段）— issue#5 */}
          <div style={{ marginTop: 16, borderTop: "1px solid #f0f0f0", paddingTop: 16 }}>
            <Text strong style={{ fontSize: 13, display: "block", marginBottom: 6 }}>
              时间段锁房
            </Text>
            <Text type="secondary" style={{ fontSize: 12, display: "block", marginBottom: 10 }}>
              按日期段锁定该房间、<Text strong style={{ fontSize: 12 }}>硬性拦截预订</Text>（前台/管家排房自动避让），到期或解除后自动恢复。
              与上面的「房态」是两回事：房态（维修/锁房）只是整间长期标记、不拦下单。
            </Text>
            <Space wrap>
              {(["other", "reserved", "maintenance"] as BlockType[]).map((bt) => (
                <Button
                  key={bt}
                  onClick={() => {
                    if (!editRoom) return;
                    setBlockingRoom(editRoom);
                    setBlockType(bt);
                    onClose();
                  }}
                >
                  {BLOCK_TYPE_LABELS[bt]}
                </Button>
              ))}
            </Space>

            {/* 当前锁房记录：可逐条解除（修复「锁房创建后删不掉」的 bug） */}
            {editRoomBlocks && editRoomBlocks.length > 0 && (
              <div style={{ marginTop: 14 }}>
                <Text strong style={{ fontSize: 12, display: "block", marginBottom: 6 }}>
                  当前锁房记录（{editRoomBlocks.length}）
                </Text>
                <Space direction="vertical" style={{ width: "100%" }} size={6}>
                  {editRoomBlocks.map((b) => (
                    <div
                      key={b.block_id}
                      style={{
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "space-between",
                        gap: 8,
                        padding: "6px 10px",
                        background: "#F7F4EF",
                        borderRadius: 6,
                      }}
                    >
                      <span style={{ fontSize: 12, minWidth: 0, flex: 1 }}>
                        <Tag style={{ marginRight: 4 }}>{BLOCK_TYPE_DISPLAY[b.block_type] ?? b.block_type}</Tag>
                        {formatBlockRange(b.start_date, b.end_date)}
                        {b.reason ? ` · ${b.reason}` : ""}
                      </span>
                      <Button
                        danger
                        size="small"
                        loading={releaseBlockMutation.isPending}
                        onClick={() => releaseBlockMutation.mutate(b.block_id)}
                      >
                        解除
                      </Button>
                    </div>
                  ))}
                </Space>
              </div>
            )}
          </div>
        </div>
      )}
    </Modal>
  );
}
