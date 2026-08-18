"use client";

import React from "react";
import { Modal, Space, Typography, DatePicker, Checkbox, Input, message } from "antd";
import dayjs from "dayjs";
import { resolveBlockEndDate } from "@/lib/block-dates";
import { BLOCK_TYPE_LABELS, type useRoomBlocking } from "@/hooks/useRoomBlocking";

const { Text } = Typography;

interface Props {
  // 整包传入 useRoomBlocking 的返回值：state 归 page 持有，Modal 只渲染
  blocking: ReturnType<typeof useRoomBlocking>;
}

// 锁房 Modal — issue#5。三类锁房（停用/保留/维修）共用此弹窗。
export function RoomBlockModal({ blocking }: Props) {
  const {
    blockingRoom, blockType,
    blockRange, setBlockRange,
    blockNoEnd, setBlockNoEnd,
    blockReason, setBlockReason,
    closeBlockForm, blockMutation,
  } = blocking;

  return (
    <Modal
      open={!!blockingRoom && !!blockType}
      title={
        blockingRoom && blockType
          ? `${BLOCK_TYPE_LABELS[blockType]} — ${blockingRoom.room_id} ${blockingRoom.room_name}`
          : ""
      }
      onCancel={closeBlockForm}
      okText="提交锁房"
      cancelText="取消"
      confirmLoading={blockMutation.isPending}
      onOk={() => {
        if (!blockingRoom || !blockType) return;
        if (!blockRange?.[0]) {
          message.warning("请选择起始日期");
          return;
        }
        const startDate: string = (blockRange[0] as any).format("YYYY-MM-DD");
        // UI 全包含语义：选到哪天就锁到哪天晚上，提交时统一 +1 天换算成排他 end_date
        const endDate = resolveBlockEndDate(
          startDate,
          blockRange?.[1] ? (blockRange[1] as any).format("YYYY-MM-DD") : null,
          blockNoEnd
        );
        if (!endDate) {
          message.warning("请选择结束日期或勾选「无截止日期」");
          return;
        }
        blockMutation.mutate({
          room_id: blockingRoom.room_id,
          block_type: blockType,
          start_date: startDate,
          end_date: endDate,
          reason:
            blockReason ||
            (blockType === "other"
              ? "停用"
              : blockType === "reserved"
              ? "保留"
              : undefined),
        });
      }}
    >
      <Space direction="vertical" style={{ width: "100%" }} size="middle">
        <div>
          <Text strong style={{ fontSize: 13, display: "block", marginBottom: 6 }}>
            时间段
          </Text>
          <DatePicker.RangePicker
            style={{ width: "100%" }}
            value={blockRange as any}
            onChange={(v) => setBlockRange(v as any)}
            disabled={[false, blockNoEnd]}
            disabledDate={(d) => d.isBefore(dayjs().startOf("day"))}
          />
          <div style={{ marginTop: 8 }}>
            <Checkbox
              checked={blockNoEnd}
              onChange={(e) => {
                setBlockNoEnd(e.target.checked);
                if (e.target.checked && blockRange?.[1]) {
                  setBlockRange([blockRange[0], null] as any);
                }
              }}
            >
              无截止日期（手动开房才解除）
            </Checkbox>
          </div>
        </div>
        <div>
          <Text strong style={{ fontSize: 13, display: "block", marginBottom: 6 }}>
            备注（选填）
          </Text>
          <Input.TextArea
            rows={3}
            value={blockReason}
            onChange={(e) => setBlockReason(e.target.value)}
            placeholder={
              blockType === "maintenance"
                ? "如：空调维修 / 漏水返工"
                : blockType === "reserved"
                ? "如：8 月业主自住保留"
                : "如：长期停用"
            }
            maxLength={200}
            showCount
          />
        </div>
        <Text type="secondary" style={{ fontSize: 12 }}>
          提交后，该房间从开始日到结束日当晚都不接受新订单（选同一天即锁当晚一晚）。前台/管家可在房态修改弹窗里删除已有锁房记录。
        </Text>
      </Space>
    </Modal>
  );
}
