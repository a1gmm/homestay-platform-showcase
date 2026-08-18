"use client";

import React, { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Card, Row, Col, Table, Select, Tag, Statistic, Typography, Skeleton, Space,
  Alert, Button, Modal, message, Result, List, Popconfirm, Tooltip,
} from "antd";
import { ThunderboltOutlined } from "@ant-design/icons";
import type { ColumnsType } from "antd/es/table";
import dayjs from "dayjs";
import Link from "next/link";
import { reconciliationApi, roomsApi } from "@/lib/api";
import { extractErrorMessage } from "@/lib/api-errors";
import { orderStatusTagColor } from "@/lib/status-display";
import { PageHeader } from "@/components/ui/PageHeader";

const { Text } = Typography;

const yuan = (v: number | null | undefined) =>
  v == null ? "—" : `¥${Number(v).toFixed(2)}`;

const REASON: Record<string, { label: string; color: string }> = {
  cancelled_stale: { label: "我们已取消·bypms还有效", color: "volcano" },
  missing: { label: "完全漏录", color: "red" },
  short: { label: "偏少·疑漏房", color: "orange" },
  over: { label: "偏多·疑多录", color: "purple" },
};

// 内部自查异常类型（不比 bypms，查我们订单自相矛盾）
const ANOMALY_TYPE: Record<string, { label: string; hint: string }> = {
  zero_price: { label: "零价漏账", hint: "订单实收记成 ¥0，营业额被少算" },
  commission_zeroed: { label: "房东被清零", hint: "佣金率 100%，房东分成为 0（可能是活动/免房单，需人工核）" },
  internal_mismatch: { label: "金额自相矛盾", hint: "订单总额与房间/每日价之和对不上" },
  room_overlap: { label: "同房重叠双订", hint: "同一房间日期重叠，疑换房残留或误录" },
};
const SEVERITY: Record<string, { color: string; label: string }> = {
  high: { color: "red", label: "严重" },
  medium: { color: "orange", label: "待核" },
  low: { color: "gold", label: "提示" },
};

const STATUS_CN: Record<string, string> = {
  pending_confirm: "待确认",
  paid_pending_room: "待排房",
  roomed_pending_checkin: "待入住",
  checked_in: "在住",
  pending_checkout: "已退房待收款", // 与 lib/utils.ts / StatusBadge 全局一致，勿再写「待退房」
  completed: "已完成",
  cancelled: "已取消",
};

interface OurOrder {
  order_id: string; actual_price: number | null; check_in: string;
  order_status: string; price_locked: boolean;
}
interface BypmsOrder {
  platform_order_id: string; bypms_fang: number | null; check_in: string;
  room_type: string; channel_chs: string; matched: boolean;
  link_candidate: string | null;
  suggested_action: "link" | "create" | "ignore" | null;
  suggested_room: string | null;
}
interface GuestDiff {
  guest_name: string;
  our_total: number | null; bypms_total: number | null; diff: number | null;
  our_order_count: number; bypms_count: number;
  price_locked: boolean; reason: string;
  our_orders: OurOrder[]; bypms_orders: BypmsOrder[];
}
interface Duplicate {
  platform_order_id: string; order_ids: string[]; guest_name: string;
}
interface Anomaly {
  type: string; severity: string; order_id: string; guest_name: string;
  channel?: string; order_status?: string; check_in: string;
  amount: number | null; detail: string; other_order_id?: string;
}
interface RoomOpt { room_id: string; room_type: string }

export default function ReconciliationPage() {
  const [month, setMonth] = useState(dayjs().format("YYYY-MM"));
  const [createModal, setCreateModal] = useState<{
    b: BypmsOrder; guest: string; room: string | null;
  } | null>(null);
  const qc = useQueryClient();

  const { data, isLoading } = useQuery({
    queryKey: ["reconciliation", month],
    queryFn: () => reconciliationApi.get(month),
  });
  const { data: rooms } = useQuery({
    queryKey: ["rooms-for-recon"],
    queryFn: async (): Promise<RoomOpt[]> => {
      const r = await roomsApi.list();
      return r.data.map((x) => ({ room_id: x.room_id, room_type: x.room_type ?? "" }));
    },
  });

  const refetch = () => qc.invalidateQueries({ queryKey: ["reconciliation"] });
  const onErr = (e: unknown) => message.error(extractErrorMessage(e));

  const createMut = useMutation({
    mutationFn: (v: { pid: string; room: string | null }) =>
      reconciliationApi.createMissing(v.pid, v.room),
    onSuccess: (res) => {
      message.success(
        `已补建 ${res.order_id}（${res.guest_name} · ¥${res.actual_price}` +
        (res.room_id ? ` · 已排 ${res.room_id} 房）` : "，待排房）"));
      setCreateModal(null);
      refetch();
    },
    onError: onErr,
  });
  const linkMut = useMutation({
    mutationFn: (v: { pid: string; oid: string }) =>
      reconciliationApi.link(v.pid, v.oid),
    onSuccess: (res) => { message.success(`已关联到 ${res.order_id}`); refetch(); },
    onError: onErr,
  });
  const ignoreMut = useMutation({
    mutationFn: (pids: string[]) => reconciliationApi.ignore(pids),
    onSuccess: (res) => { message.success(`已忽略 ${res.ignored} 条，下次不再提醒`); refetch(); },
    onError: onErr,
  });
  const voidMut = useMutation({
    mutationFn: (oid: string) => reconciliationApi.voidDuplicate(oid),
    onSuccess: (res) => { message.success(`已作废 ${res.order_id}`); refetch(); },
    onError: onErr,
  });
  const autoFixMut = useMutation({
    mutationFn: (dry: boolean) => reconciliationApi.autoFix(month, dry),
  });

  // ── 一键修复：先预览再执行 ──
  const onAutoFix = async () => {
    try {
      const preview = await autoFixMut.mutateAsync(true);
      const { plan, counts } = preview;
      const total = counts.creates + counts.links;
      if (!total) { message.info("没有可自动修复的项"); return; }
      Modal.confirm({
        width: 620,
        title: `一键修复 ${total} 处 — 请过目`,
        content: (
          <div style={{ maxHeight: 380, overflow: "auto" }}>
            {plan.creates.length > 0 && (
              <List size="small" header={<Text strong>🆕 补建漏录单（{plan.creates.length}）</Text>}
                dataSource={plan.creates}
                renderItem={(c: { guest_name: string; check_in: string; bypms_fang: number; suggested_room: string | null }) => (
                  <List.Item>
                    {c.guest_name} · {c.check_in} · {yuan(c.bypms_fang)}
                    {c.suggested_room ? ` → 自动排 ${c.suggested_room} 房` : " → 待排房"}
                  </List.Item>
                )} />
            )}
            {plan.links.length > 0 && (
              <List size="small" header={<Text strong>🔗 关联手录单（{plan.links.length}）</Text>}
                dataSource={plan.links}
                renderItem={(l: { guest_name: string; order_id: string }) => (
                  <List.Item>{l.guest_name} → 补单号到 {l.order_id}</List.Item>
                )} />
            )}

          </div>
        ),
        okText: `确认修复 ${total} 处`,
        cancelText: "取消",
        onOk: async () => {
          const r = await autoFixMut.mutateAsync(false);
          const res = r.results;
          const ok = res.created.length + res.linked.length + res.ignored;
          if (res.errors.length) {
            message.warning(`完成 ${ok} 处，${res.errors.length} 处失败：${res.errors.map((e: { detail: string }) => e.detail).join("；")}`);
          } else {
            message.success(`🎉 全部完成：补建 ${res.created.length}、关联 ${res.linked.length}`);
          }
          refetch();
        },
      });
    } catch (e) { onErr(e); }
  };

  const months = Array.from({ length: 6 }).map((_, i) =>
    dayjs().subtract(i, "month").format("YYYY-MM"));

  const colsGuest: ColumnsType<GuestDiff> = [
    { title: "客人", dataIndex: "guest_name" },
    {
      title: "原因", dataIndex: "reason", width: 150,
      render: (r: string) => {
        const m = REASON[r] || { label: r, color: "default" };
        return <Tag color={m.color}>{m.label}</Tag>;
      },
    },
    {
      title: "怎么处理", key: "howto", width: 170,
      render: (_: unknown, row: GuestDiff) => {
        if (row.reason === "cancelled_stale") {
          return <Tag color="orange">👤需人工·可能取消错了(漏钱)</Tag>;
        }
        const c = row.bypms_orders.filter((b) => b.suggested_action === "create").length;
        const l = row.bypms_orders.filter((b) => b.suggested_action === "link").length;
        const tags: React.ReactNode[] = [];
        if (c) tags.push(<Tag key="c" color="blue">⚡自动·补建{c}单</Tag>);
        if (l) tags.push(<Tag key="l" color="cyan">⚡自动·关联{l}单</Tag>);
        if (!tags.length) return <Tag color="orange">👤需人工核</Tag>;
        return <Space size={4} wrap>{tags}</Space>;
      },
    },
    { title: "我们合计", dataIndex: "our_total", align: "right", render: yuan },
    { title: "bypms合计", dataIndex: "bypms_total", align: "right", render: yuan },
    {
      title: "差", dataIndex: "diff", align: "right",
      render: (v: number | null) =>
        v == null ? "—" : <Text type={v > 0 ? "danger" : "warning"}>{`¥${Number(v).toFixed(2)}`}</Text>,
    },
    {
      title: "", key: "rowact", width: 90,
      render: (_: unknown, row: GuestDiff) => (
        <Popconfirm title={`忽略「${row.guest_name}」整组？下次不再提醒`}
          onConfirm={(e) => { e?.stopPropagation();
            ignoreMut.mutate(row.bypms_orders.map((b) => b.platform_order_id)); }}
          onCancel={(e) => e?.stopPropagation()}>
          <Button size="small" type="text" onClick={(e) => e.stopPropagation()}>忽略</Button>
        </Popconfirm>
      ),
    },
  ];

  const expandedRow = (row: GuestDiff) => (
    <Row gutter={16}>
      <Col xs={24} lg={11}>
        <Card size="small" title="我们系统的单" styles={{ body: { padding: 8 } }}>
          <Table<OurOrder> rowKey="order_id" size="small" pagination={false}
            dataSource={row.our_orders} locale={{ emptyText: "一单都没有" }}
            columns={[
              { title: "订单", dataIndex: "order_id",
                render: (id: string) => <Link href={`/orders?keyword=${id}`}>{id}</Link> },
              { title: "入住", dataIndex: "check_in" },
              { title: "实收", dataIndex: "actual_price", align: "right", render: yuan },
              { title: "状态", dataIndex: "order_status",
                render: (s: string) => (
                  <Tag color={orderStatusTagColor(s)}>{STATUS_CN[s] || s}</Tag>
                ) },
            ]} />
        </Card>
      </Col>
      <Col xs={24} lg={13}>
        <Card size="small" title="bypms 的单" styles={{ body: { padding: 8 } }}>
          <Table<BypmsOrder> rowKey="platform_order_id" size="small" pagination={false}
            dataSource={row.bypms_orders}
            columns={[
              { title: "入住", dataIndex: "check_in", width: 100 },
              { title: "房费", dataIndex: "bypms_fang", align: "right", render: yuan, width: 90 },
              { title: "房型", dataIndex: "room_type", ellipsis: true },
              {
                title: "动作", key: "act", width: 150,
                render: (_: unknown, b: BypmsOrder) => {
                  if (b.matched) return <Tag color="green">已对上</Tag>;
                  if (b.suggested_action === "link" && b.link_candidate) {
                    return (
                      <Popconfirm
                        title={`把平台单号关联到手录单 ${b.link_candidate}？（防止重复建单）`}
                        onConfirm={() => linkMut.mutate({ pid: b.platform_order_id, oid: b.link_candidate! })}>
                        <Button size="small" type="primary">关联 {b.link_candidate}</Button>
                      </Popconfirm>
                    );
                  }
                  if (b.suggested_action === "create") {
                    return (
                      <Button size="small" type="primary" ghost
                        onClick={() => setCreateModal({ b, guest: row.guest_name, room: b.suggested_room })}>
                        补建{b.suggested_room ? `→${b.suggested_room}房` : ""}
                      </Button>
                    );
                  }
                  return (
                    <Button size="small" type="text"
                      onClick={() => ignoreMut.mutate([b.platform_order_id])}>忽略</Button>
                  );
                },
              },
            ]} />
        </Card>
      </Col>
    </Row>
  );

  const colsDup: ColumnsType<Duplicate> = [
    { title: "bypms单号", dataIndex: "platform_order_id" },
    { title: "客人", dataIndex: "guest_name" },
    {
      title: "我们的订单（自己选留哪条，作废另一条）", dataIndex: "order_ids",
      render: (ids: string[]) => (
        <Space wrap>
          {ids.map((id) => (
            <Space key={id} size={4}>
              <Link href={`/orders?keyword=${id}`}>{id}</Link>
              <Popconfirm title={`作废 ${id}？（有收款/在住会被系统拒绝，可逆）`}
                onConfirm={() => voidMut.mutate(id)}>
                <Button size="small" danger type="text">作废</Button>
              </Popconfirm>
            </Space>
          ))}
        </Space>
      ),
    },
  ];

  const colsAnomaly: ColumnsType<Anomaly> = [
    {
      title: "严重度", dataIndex: "severity", width: 90,
      render: (v: string) => {
        const m = SEVERITY[v] || { color: "default", label: v };
        return <Tag color={m.color}>{m.label}</Tag>;
      },
    },
    {
      title: "问题", dataIndex: "type", width: 130,
      render: (t: string) => {
        const m = ANOMALY_TYPE[t];
        return <Tooltip title={m?.hint}><Text strong>{m?.label || t}</Text></Tooltip>;
      },
    },
    { title: "客人", dataIndex: "guest_name", width: 110 },
    {
      title: "订单", dataIndex: "order_id", width: 190,
      render: (id: string) => <Link href={`/orders?keyword=${id}`}>{id}</Link>,
    },
    { title: "入住", dataIndex: "check_in", width: 100 },
    { title: "说明", dataIndex: "detail", ellipsis: true },
  ];

  const s = data?.summary;
  const anomalies: Anomaly[] = data?.anomalies ?? [];
  const allClean = s && s.guest_diff_count === 0 && s.dup_count === 0
    && (s.anomaly_count ?? 0) === 0;
  const roomOptions = [
    { value: "", label: "先不排房" },
    ...(rooms || []).map((r: RoomOpt) => ({ value: r.room_id, label: `${r.room_id}（${r.room_type?.slice(0, 8) ?? ""}…）` })),
  ];

  return (
    <div>
      <PageHeader title="对账管家" subtitle="我们系统 vs bypms · 能自动的一键修，剩下的每条给明确动作，处理过的有记性" />

      <Space style={{ marginBottom: 16 }} wrap>
        <Text>月份</Text>
        <Select value={month} onChange={setMonth}
          options={months.map((m) => ({ value: m, label: m }))} style={{ width: 130 }} />
        {s && s.auto_fixable > 0 && (
          <Button type="primary" icon={<ThunderboltOutlined />}
            loading={autoFixMut.isPending} onClick={onAutoFix}>
            一键修复 {s.auto_fixable} 处
          </Button>
        )}
      </Space>

      {isLoading || !data ? (
        <Skeleton active />
      ) : allClean ? (
        <Result status="success" title="✅ 本月账已对平"
          subTitle={`已忽略 ${s.ignored_count} 条历史噪音 · 系统每 3 分钟自动同步金额`} />
      ) : (
        <>
          <Alert
            type="info"
            message={
              "「怎么处理」列写明每条动作：⚡自动 = 点上方「一键修复」就搞定；👤需人工 = 点行展开逐条处理。" +
              "「我们已取消·bypms还有效」要人工核：可能是我们取消错了在漏钱（核实无误后可点行内忽略）。" +
              (s.ghost_count ? ` 已自动剔除 ${s.ghost_count} 条 bypms 已删除的旧单（改单/退订残留，不参与对账）。` : "")
            }
            style={{ marginBottom: 16 }}
          />
          <Row gutter={16} style={{ marginBottom: 16 }}>
            <Col><Statistic title="对不上的客人" value={s.guest_diff_count} suffix="人" /></Col>
            <Col><Statistic title="可自动修" value={s.auto_fixable} suffix="处"
              valueStyle={{ color: "#1677ff" }} /></Col>
            <Col><Statistic title="需人工" value={s.manual_count} suffix="处"
              valueStyle={{ color: s.manual_count ? "#fa8c16" : undefined }} /></Col>
            <Col><Statistic title="差额合计" value={s.diff_total ?? "—"}
              prefix={s.diff_total == null ? "" : "¥"} /></Col>
            <Col><Statistic title="已忽略(累计)" value={s.ignored_count} suffix="条" /></Col>
            <Col><Statistic title="系统内部异常" value={s.anomaly_count ?? 0} suffix="条"
              valueStyle={{ color: (s.anomaly_high ?? 0) ? "#cf1322" : (s.anomaly_count ?? 0) ? "#fa8c16" : undefined }} /></Col>
          </Row>

          <Card title="对不上的客人（点行展开 · 每条给明确动作）" size="small">
            <Table<GuestDiff> rowKey="guest_name" size="small" pagination={false}
              dataSource={data.guest_diff} columns={colsGuest}
              expandable={{ expandRowByClick: true, expandedRowRender: expandedRow }}
              locale={{ emptyText: "全部对得上 🎉" }} />
          </Card>
          <Card title="重复单：一个 bypms 单对应我们多条订单" size="small" style={{ marginTop: 12 }}>
            <Table<Duplicate> rowKey="platform_order_id" size="small" pagination={false}
              dataSource={data.duplicates} columns={colsDup}
              locale={{ emptyText: "无重复单" }} />
          </Card>
          {anomalies.length > 0 && (
            <Card size="small" style={{ marginTop: 12 }}
              title={<span>⚠️ 系统内部异常（{anomalies.length}）—— 不比 bypms，是我们订单自身的毛病</span>}>
              <Alert type="warning" showIcon style={{ marginBottom: 12 }}
                message="这些是对账「总额比对」抓不到的死角：记成 ¥0 的漏账、房东被清零、金额自相矛盾、同房重叠。逐条点订单号进去核实、手动改。" />
              <Table<Anomaly> rowKey={(r) => `${r.type}-${r.order_id}`} size="small"
                pagination={false} dataSource={anomalies} columns={colsAnomaly} />
            </Card>
          )}
        </>
      )}

      <Modal
        open={!!createModal}
        title="补建漏录单"
        okText="补建"
        cancelText="取消"
        confirmLoading={createMut.isPending}
        onCancel={() => setCreateModal(null)}
        onOk={() => createModal && createMut.mutate({
          pid: createModal.b.platform_order_id,
          room: createModal.room || null,
        })}
      >
        {createModal && (
          <div>
            <p>客人：{createModal.guest}</p>
            <p>入住：{createModal.b.check_in} · {createModal.b.room_type || "房型未知"}</p>
            <p>房费：{yuan(createModal.b.bypms_fang)}（按 bypms 原始，渠道/佣金自动算好）</p>
            <p>
              排到房间：
              <Select style={{ width: 240 }} value={createModal.room ?? ""}
                onChange={(v) => setCreateModal({ ...createModal, room: v || null })}
                options={roomOptions} />
            </p>
            {createModal.room && (
              <Text type="secondary">已按房型帮你选了空房，可改。</Text>
            )}
          </div>
        )}
      </Modal>
    </div>
  );
}
