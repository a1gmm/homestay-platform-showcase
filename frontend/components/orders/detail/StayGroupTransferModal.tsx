"use client";

import { useState } from "react";
import { Alert, Button, Modal, Select, Space, Spin, Typography } from "antd";
import { useQuery } from "@tanstack/react-query";
import { ordersApi, extractErrorMessage } from "@/lib/api";
import type { StaySegment } from "@/lib/types";
import { TransferRoomModal } from "../TransferRoomModal";

// Match the transfer endpoint; the selected segment's live status is checked again.
const transferable = (status: string) => [
  "pending_confirm", "paid_pending_room", "roomed_pending_checkin", "rescheduled", "checked_in",
].includes(status);

export function StayGroupTransferModal({ segments, onClose }: {
  segments: StaySegment[];
  onClose: () => void;
}) {
  const [segmentId, setSegmentId] = useState<string>();
  const [rowId, setRowId] = useState<string>();
  const detail = useQuery({
    queryKey: ["order", segmentId],
    queryFn: () => ordersApi.get(segmentId!).then((r) => r.data),
    enabled: !!segmentId,
    staleTime: 0,
  });
  const order = detail.data;
  // Never fall back to another room/segment when the selected row is unavailable.
  const rows = order?.rooms?.filter((row) => row.room_id && !row.checked_out_at) ?? [];
  const canTransfer = !!order && transferable(order.order_status);

  if (rowId && order) {
    return <TransferRoomModal
      order={order}
      orderRoomId={rowId}
      onClose={() => setRowId(undefined)}
      onSuccess={onClose}
    />;
  }

  return <Modal open focusTriggerAfterClose={false} title="选择要换房的住宿段" footer={null} onCancel={onClose} destroyOnHidden>
      <Space direction="vertical" size={16} style={{ width: "100%" }}>
        <Alert type="info" showIcon message="仅更换所选住宿段的房间，其他段保持不变。" />
        <Select
          aria-label="住宿段"
          placeholder="选择日期和原房间"
          style={{ width: "100%" }}
          value={segmentId}
          onChange={(id) => { setSegmentId(id); setRowId(undefined); }}
          options={segments.map((segment) => ({
            value: segment.order_id,
            label: `${segment.check_in_date} → ${segment.check_out_date} · ${(segment.room_ids ?? segment.rooms?.map((room) => room.room_id).filter(Boolean) ?? []).join(" / ") || "待排房"}${transferable(segment.order_status) ? "" : "（当前状态不可换房）"}`,
            disabled: !transferable(segment.order_status),
          }))}
        />
        {detail.isFetching ? <Spin tip="正在读取房间信息"><div style={{ height: 48 }} /></Spin> : detail.isError ? (
          <Alert type="error" message={extractErrorMessage(detail.error, "读取住宿段失败")}
            action={<Button onClick={() => detail.refetch()}>重试</Button>} />
        ) : segmentId && order ? <>
          {!canTransfer || !rows.length ? <Alert type="warning" message="该住宿段当前没有可换房的房间，请重新选择。" /> : rows.map((row) => (
            <div key={row.order_room_id} style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 }}>
              <div><Typography.Text strong>{row.room_id}</Typography.Text><br />
                <Typography.Text type="secondary">{row.check_in_date} → {row.check_out_date}</Typography.Text></div>
              <Button onClick={() => setRowId(row.order_room_id)}>更换 {row.room_id} 房</Button>
            </div>
          ))}
        </> : null}
      </Space>
    </Modal>;
}
