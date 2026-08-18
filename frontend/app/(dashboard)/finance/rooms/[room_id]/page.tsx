"use client";

import React, { useState } from "react";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import {
  Card, Table, Typography, Tag, Space, DatePicker, Button, Row, Col, Skeleton,
} from "antd";
import dayjs, { Dayjs } from "dayjs";
import { ArrowLeftOutlined, HomeOutlined, UserOutlined } from "@ant-design/icons";
import { financeApi, roomsApi } from "@/lib/api";
import { PageHeader } from "@/components/ui/PageHeader";
import { StatCard } from "@/components/ui/StatCard";
import { tokens } from "@/lib/design-tokens";

const { Text } = Typography;

// 全量 label（v2#7 对齐后端 EXPENSE_CATEGORY_LABELS）。历史支出含旧类目，缺项会显英文原文。
const EXPENSE_CATEGORIES: Record<string, string> = {
  cleaning: "保洁",
  maintenance: "维修费",
  utilities: "水电煤（旧）",
  supplies: "采购费",
  platform_fee: "平台手续费",
  tax: "税费",
  other: "其他",
  public_utilities: "公摊水费",
  water: "水费",
  cold_water: "冷水（旧）",
  broadband: "宽带费",
  daily_supplies: "日耗",
  laundry: "洗涤",
  hot_water: "热水（旧）",
  gas: "燃气费",
  property_fee: "物业费",
  electricity: "电费",
  kitchen_cleaning: "厨房保洁",
  property_guidance_fee: "物业引导费",
  new_linen_prewash: "新布草过水费",
};

export default function RoomFinanceDetailPage() {
  const params = useParams();
  const search = useSearchParams();
  const router = useRouter();
  const roomId = params?.room_id as string;
  // 日期区间跟随财务页下钻链接的 ?start_date&end_date；无参时兜底本月整月。
  const _qStart = search?.get("start_date");
  const _qEnd = search?.get("end_date");
  const [range, setRange] = useState<[Dayjs, Dayjs]>([
    _qStart ? dayjs(_qStart) : dayjs().startOf("month"),
    _qEnd ? dayjs(_qEnd) : dayjs().endOf("month"),
  ]);
  const startDate = range[0].format("YYYY-MM-DD");
  const endDate = range[1].format("YYYY-MM-DD");

  const rangePresets = [
    { label: "本月", value: [dayjs().startOf("month"), dayjs().endOf("month")] as [Dayjs, Dayjs] },
    { label: "上月", value: [dayjs().subtract(1, "month").startOf("month"), dayjs().subtract(1, "month").endOf("month")] as [Dayjs, Dayjs] },
    { label: "近 3 个月", value: [dayjs().subtract(2, "month").startOf("month"), dayjs().endOf("month")] as [Dayjs, Dayjs] },
    { label: "近 6 个月", value: [dayjs().subtract(5, "month").startOf("month"), dayjs().endOf("month")] as [Dayjs, Dayjs] },
    { label: "今年", value: [dayjs().startOf("year"), dayjs().endOf("year")] as [Dayjs, Dayjs] },
  ];

  const { data: room } = useQuery({
    queryKey: ["room", roomId],
    queryFn: () => roomsApi.get(roomId).then((r) => r.data),
    enabled: !!roomId,
  });

  const { data: byRoom, isLoading: summaryLoading } = useQuery({
    queryKey: ["finance", "by-room", startDate, endDate],
    queryFn: () =>
      financeApi.summaryByRoom({ start_date: startDate, end_date: endDate }).then((r) => r.data),
  });
  const summary = byRoom?.find((r) => r.room_id === roomId);

  const { data: expenses, isLoading: expLoading } = useQuery({
    queryKey: ["expenses", roomId, startDate, endDate],
    queryFn: () =>
      financeApi.expenses
        .list({ room_id: roomId, start_date: startDate, end_date: endDate })
        .then((r) => r.data),
  });

  const { data: segments } = useQuery({
    queryKey: ["finance", "room-segments", roomId, startDate, endDate],
    queryFn: () =>
      // 按 OrderRoom 段过滤（房号 + 离店日区间），口径同上方「区间收入」汇总卡。
      // 不用 ordersApi.list({room_id})：那走 Order.room_id 顶层字段，多房订单顶层为空
      // → 多房单漏行，明细与汇总卡对不上。段级返回把该房参与的多房订单也列出来。
      financeApi
        .roomRevenueSegments({
          room_id: roomId,
          start_date: startDate,
          end_date: endDate,
        })
        .then((r) => r.data),
    enabled: !!roomId,
  });

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      <PageHeader
        title={
          <Space>
            <Button
              size="small"
              icon={<ArrowLeftOutlined />}
              onClick={() => router.push("/finance?tab=by-room")}
            />
            <span>
              房号 {roomId} · {room?.room_name || ""}
            </span>
          </Space>
        }
        subtitle={
          <Space size={12}>
            <span>
              <HomeOutlined /> 楼层 {room?.floor ?? "—"}
            </span>
            {room?.owner_id && (
              <Tag color="gold" bordered={false}>
                <UserOutlined /> 业主 {room.owner_id} · 分成{" "}
                {Math.round(Number(room.owner_share_ratio ?? 0) * 100)}%
              </Tag>
            )}
          </Space>
        }
        extra={
          <Space wrap>
            <DatePicker.RangePicker
              value={range as any}
              onChange={(v) => {
                if (v && v[0] && v[1]) setRange([v[0], v[1]] as [Dayjs, Dayjs]);
              }}
              allowClear={false}
              placeholder={["起始日期", "结束日期"]}
              presets={rangePresets}
            />
          </Space>
        }
      />

      <Skeleton loading={summaryLoading} active>
        {summary && (
          <Row gutter={[16, 16]}>
            <Col xs={12} md={6}>
              <StatCard
                title="区间收入"
                value={Number(summary.revenue).toLocaleString("zh-CN")}
                prefix="¥"
                tone="brand"
              />
            </Col>
            <Col xs={12} md={6}>
              <StatCard
                title="净收入"
                value={Number(summary.net_revenue).toLocaleString("zh-CN")}
                prefix="¥"
                tone="success"
              />
            </Col>
            <Col xs={12} md={6}>
              <StatCard
                title="支出（公司+业主）"
                value={(
                  Number(summary.expense_company) + Number(summary.expense_owner)
                ).toLocaleString("zh-CN")}
                prefix="¥"
                tone="warn"
              />
            </Col>
            <Col xs={12} md={6}>
              <StatCard
                title="业主应得"
                value={Number(summary.owner_net_share).toLocaleString("zh-CN")}
                prefix="¥"
                tone="info"
              />
            </Col>
          </Row>
        )}
      </Skeleton>

      <Card
        bordered={false}
        style={{ borderRadius: 12, boxShadow: tokens.shadow.sm }}
        title={<Text strong>区间订单（收入来源）</Text>}
      >
        <Table
          dataSource={segments ?? []}
          rowKey="order_room_id"
          size="small"
          pagination={false}
          scroll={{ x: 720 }}
          columns={[
            { title: "订单号", dataIndex: "order_id", key: "order_id", width: 160 },
            { title: "客人", dataIndex: "guest_name", key: "guest_name", width: 100 },
            { title: "入住", dataIndex: "check_in_date", key: "check_in_date", width: 110 },
            { title: "退房", dataIndex: "check_out_date", key: "check_out_date", width: 110 },
            { title: "晚数", dataIndex: "nights", key: "nights", width: 60, align: "right" },
            {
              title: "渠道",
              dataIndex: "channel",
              key: "channel",
              width: 80,
              render: (v) => <Tag bordered={false}>{v}</Tag>,
            },
            {
              title: "房费",
              dataIndex: "actual_price",
              key: "actual_price",
              width: 100,
              align: "right",
              className: "col-amount",
              render: (v) =>
                v ? (
                  <Text strong>¥{Number(v).toLocaleString()}</Text>
                ) : (
                  <Text type="secondary">—</Text>
                ),
            },
          ]}
          locale={{ emptyText: "区间内无订单" }}
        />
      </Card>

      <Card
        bordered={false}
        style={{ borderRadius: 12, boxShadow: tokens.shadow.sm }}
        title={<Text strong>区间支出明细</Text>}
      >
        <Table
          dataSource={expenses ?? []}
          rowKey="expense_id"
          size="small"
          loading={expLoading}
          pagination={false}
          scroll={{ x: 720 }}
          columns={[
            { title: "日期", dataIndex: "expense_date", key: "expense_date", width: 100 },
            {
              title: "类别",
              dataIndex: "category",
              key: "category",
              width: 100,
              render: (v) => (
                <Tag bordered={false}>{EXPENSE_CATEGORIES[v] ?? v}</Tag>
              ),
            },
            {
              title: "描述",
              dataIndex: "description",
              key: "description",
            },
            {
              title: "支付方",
              dataIndex: "payer",
              key: "payer",
              width: 80,
              render: (v) =>
                v === "owner" ? (
                  <Tag color="gold" bordered={false}>业主</Tag>
                ) : (
                  <Tag color="blue" bordered={false}>公司</Tag>
                ),
            },
            {
              title: "金额",
              dataIndex: "amount",
              key: "amount",
              width: 100,
              align: "right",
              className: "col-amount",
              render: (v) => (
                <Text strong style={{ color: "#ff4d4f" }}>
                  -¥{Number(v).toLocaleString()}
                </Text>
              ),
            },
          ]}
          locale={{ emptyText: "区间内无支出" }}
        />
      </Card>
    </div>
  );
}
