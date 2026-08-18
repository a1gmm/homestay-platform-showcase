"use client";

/**
 * 「业主与分成」合并页 · Tab 2: 按房间看
 *
 * - 表格 inline 编辑三类 share_ratio(普通 / 试住 / 自住)
 * - 行展开 → 该房的 CostShareEditor(14 类目 × 3 订单类型矩阵)
 * - 顶部按钮:批量设置选中房间的三类比例
 * - 支持 ?room_id={id} URL 高亮指定行(从其他页跳转过来时定位)
 */
import { useEffect, useMemo, useState } from "react";
import { extractErrorMessage } from "@/lib/api-errors";
import { useSearchParams } from "next/navigation";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Card, Table, Typography, Button, InputNumber, Modal, Checkbox,
  Space, message, Tag,
} from "antd";
import { roomsApi } from "@/lib/api";
import CostShareEditor from "@/components/rooms/CostShareEditor";
import type { RoomOut } from "@/lib/types";
import { tokens } from "@/lib/design-tokens";

const { Text } = Typography;

export default function RoomsTab() {
  const qc = useQueryClient();
  const searchParams = useSearchParams();
  const highlightRoomId = searchParams.get("room_id") || undefined;

  const [selectedRowKeys, setSelectedRowKeys] = useState<string[]>([]);
  const [bulkModalOpen, setBulkModalOpen] = useState(false);
  const [bulkNormal, setBulkNormal] = useState<number | null>(0.6);
  const [bulkTrial, setBulkTrial] = useState<number | null>(1.0);
  const [bulkOwnerSelf, setBulkOwnerSelf] = useState<number | null>(1.0);
  const [coverNormal, setCoverNormal] = useState(true);
  const [coverTrial, setCoverTrial] = useState(true);
  const [coverOwnerSelf, setCoverOwnerSelf] = useState(true);
  const [expandedRowKeys, setExpandedRowKeys] = useState<string[]>([]);

  const { data: rooms, isLoading } = useQuery({
    queryKey: ["rooms"],
    queryFn: () => roomsApi.list().then((r) => r.data),
  });

  // 从 URL 进来时,自动展开并滚动到目标房
  useEffect(() => {
    if (highlightRoomId && rooms?.some((r) => r.room_id === highlightRoomId)) {
      setExpandedRowKeys([highlightRoomId]);
      // 让浏览器有时间渲染再滚动
      setTimeout(() => {
        const el = document.querySelector(`[data-row-key="${highlightRoomId}"]`);
        el?.scrollIntoView({ behavior: "smooth", block: "center" });
      }, 200);
    }
  }, [highlightRoomId, rooms]);

  const inlineUpdate = useMutation({
    mutationFn: ({ id, data }: { id: string; data: Record<string, unknown> }) =>
      roomsApi.update(id, data as never),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["rooms"] });
    },
    onError: (e: any) => message.error(extractErrorMessage(e, "保存失败")),
  });

  const bulkUpdate = useMutation({
    mutationFn: (vars: {
      room_ids: string[];
      ratios: { normal?: number; trial?: number; owner_self?: number };
    }) => roomsApi.bulkShareRatios(vars.room_ids, vars.ratios).then((r) => r.data),
    onSuccess: (data) => {
      message.success(`已批量更新 ${data.updated} 套房间的分成比例`);
      qc.invalidateQueries({ queryKey: ["rooms"] });
      setBulkModalOpen(false);
      setSelectedRowKeys([]);
    },
    onError: (e: any) => message.error(extractErrorMessage(e, "批量保存失败")),
  });

  const stats = useMemo(() => {
    const list = (rooms as RoomOut[] | undefined) ?? [];
    const total = list.length;
    const unconfigured = list.filter((r) => {
      const a = Number(r.owner_share_ratio);
      const b = Number(r.share_ratio_trial);
      const c = Number(r.share_ratio_owner_self);
      return Number.isNaN(a) || Number.isNaN(b) || Number.isNaN(c);
    }).length;
    return { total, configured: total - unconfigured, unconfigured };
  }, [rooms]);

  const renderRatioCell = (
    field: "owner_share_ratio" | "share_ratio_trial" | "share_ratio_owner_self"
  ) => {
    const RatioCell = (value: any, record: RoomOut) => (
      <InputNumber
        size="small"
        min={0}
        max={1}
        step={0.05}
        precision={3}
        style={{ width: 96 }}
        defaultValue={value != null ? Number(value) : undefined}
        onBlur={(e) => {
          const v = e.target.value === "" ? null : Number(e.target.value);
          if (v == null || Number.isNaN(v)) return;
          if (Math.abs(v - Number(value ?? 0)) < 0.0005) return; // 没改不发请求
          inlineUpdate.mutate({ id: record.room_id, data: { [field]: v } });
        }}
      />
    );
    RatioCell.displayName = `RatioCell(${field})`;
    return RatioCell;
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <Space wrap style={{ width: "100%", justifyContent: "space-between" }}>
        <Space>
          <Tag color="blue">总计 {stats.total}</Tag>
          <Tag color="green">已配置 {stats.configured}</Tag>
          {stats.unconfigured > 0 && <Tag color="orange">未配置 {stats.unconfigured}</Tag>}
        </Space>
        <Button
          type="primary"
          disabled={selectedRowKeys.length === 0}
          onClick={() => setBulkModalOpen(true)}
        >
          批量设置选中 {selectedRowKeys.length} 套房间分成
        </Button>
      </Space>

      <Table
        rowKey="room_id"
        loading={isLoading}
        dataSource={(rooms as RoomOut[] | undefined) ?? []}
        pagination={{ pageSize: 50 }}
        size="small"
        scroll={{ x: 900 }}
        rowSelection={{
          selectedRowKeys,
          onChange: (keys) => setSelectedRowKeys(keys as string[]),
        }}
        expandable={{
          expandedRowKeys,
          onExpandedRowsChange: (keys) => setExpandedRowKeys(keys as string[]),
          expandedRowRender: (record) => (
            <div style={{ padding: "8px 16px", background: tokens.color.bg.subtle }}>
              <CostShareEditor roomId={record.room_id} />
            </div>
          ),
        }}
        rowClassName={(record) =>
          highlightRoomId === record.room_id ? "row-highlight" : ""
        }
        columns={[
          { title: "房号", dataIndex: "room_id", width: 90, fixed: "left" as const },
          { title: "房名", dataIndex: "room_name", width: 120 },
          {
            title: "分组",
            dataIndex: "room_type",
            width: 160,
            ellipsis: true,
            render: (v) => v || "—",
          },
          {
            title: "业主",
            dataIndex: "owner_id",
            width: 120,
            render: (v) => (v ? <Tag>{v}</Tag> : <Text type="secondary">未关联</Text>),
          },
          {
            title: "普通单分成",
            dataIndex: "owner_share_ratio",
            width: 120,
            render: renderRatioCell("owner_share_ratio"),
          },
          {
            title: "试住单分成",
            dataIndex: "share_ratio_trial",
            width: 120,
            render: renderRatioCell("share_ratio_trial"),
          },
          {
            title: "自住单分成",
            dataIndex: "share_ratio_owner_self",
            width: 120,
            render: renderRatioCell("share_ratio_owner_self"),
          },
        ]}
        footer={() => (
          <Text type="secondary" style={{ fontSize: 12 }}>
            提示:直接在格子里输入数字(0–1 之间,例如 0.6 表示业主拿 60%)失焦自动保存;
            想给多套房一起设同一个比例,先勾选行再点上方按钮;
            点击行左侧 ▶ 展开可配置该房的费用占比规则(14 类目 × 3 订单类型)。
          </Text>
        )}
      />

      <Modal
        open={bulkModalOpen}
        title={`批量设置 ${selectedRowKeys.length} 套房间分成`}
        onCancel={() => setBulkModalOpen(false)}
        confirmLoading={bulkUpdate.isPending}
        okText="应用"
        cancelText="取消"
        onOk={() => {
          if (!coverNormal && !coverTrial && !coverOwnerSelf) {
            message.warning("请至少勾选一个要覆盖的字段");
            return;
          }
          const ratios: { normal?: number; trial?: number; owner_self?: number } = {};
          if (coverNormal && bulkNormal != null) ratios.normal = bulkNormal;
          if (coverTrial && bulkTrial != null) ratios.trial = bulkTrial;
          if (coverOwnerSelf && bulkOwnerSelf != null) ratios.owner_self = bulkOwnerSelf;
          bulkUpdate.mutate({ room_ids: selectedRowKeys, ratios });
        }}
      >
        <Space direction="vertical" style={{ width: "100%" }} size="middle">
          <Text type="secondary" style={{ fontSize: 12 }}>
            勾选要覆盖的字段,未勾选的字段保持原值不动。
          </Text>
          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <Checkbox checked={coverNormal} onChange={(e) => setCoverNormal(e.target.checked)} style={{ flex: "0 0 110px" }}>
              普通单分成
            </Checkbox>
            <InputNumber
              disabled={!coverNormal}
              min={0}
              max={1}
              step={0.05}
              precision={3}
              style={{ width: 140 }}
              value={bulkNormal}
              onChange={(v) => setBulkNormal(v as number | null)}
            />
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <Checkbox checked={coverTrial} onChange={(e) => setCoverTrial(e.target.checked)} style={{ flex: "0 0 110px" }}>
              试住单分成
            </Checkbox>
            <InputNumber
              disabled={!coverTrial}
              min={0}
              max={1}
              step={0.05}
              precision={3}
              style={{ width: 140 }}
              value={bulkTrial}
              onChange={(v) => setBulkTrial(v as number | null)}
            />
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <Checkbox checked={coverOwnerSelf} onChange={(e) => setCoverOwnerSelf(e.target.checked)} style={{ flex: "0 0 110px" }}>
              自住单分成
            </Checkbox>
            <InputNumber
              disabled={!coverOwnerSelf}
              min={0}
              max={1}
              step={0.05}
              precision={3}
              style={{ width: 140 }}
              value={bulkOwnerSelf}
              onChange={(v) => setBulkOwnerSelf(v as number | null)}
            />
          </div>
        </Space>
      </Modal>

      <style jsx global>{`
        .row-highlight {
          background-color: ${tokens.color.brand.primarySoft} !important;
        }
        .row-highlight:hover > td {
          background-color: ${tokens.color.brand.primarySoft} !important;
        }
      `}</style>
    </div>
  );
}
