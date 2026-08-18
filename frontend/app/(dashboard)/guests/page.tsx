"use client";

import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { guestsApi } from "@/lib/api";
import type { GuestOut, GuestOrderHistory } from "@/lib/types";
import {
  Table, Typography, Input, Card, Tag, Space, Modal,
  Descriptions, List, Statistic, Row, Col, Form, message,
} from "antd";
import { SearchOutlined, UserOutlined, StarOutlined, RiseOutlined } from "@ant-design/icons";
import type { ColumnsType } from "antd/es/table";
import dayjs from "dayjs";
import { useIsMobile } from "@/lib/responsive";
import { PageHeader } from "@/components/ui/PageHeader";
import { StatCard } from "@/components/ui/StatCard";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { tokens } from "@/lib/design-tokens";
import { orderStatusTagColor } from "@/lib/status-display";

const { Title, Text } = Typography;

const ORDER_STATUS_LABELS: Record<string, string> = {
  pending_confirm: "待确认", paid_pending_room: "待排房",
  roomed_pending_checkin: "待入住", checked_in: "在住", pending_checkout: "已退房待收款",
  pending_payment: "待完成",
  completed: "已完成", cancelled: "已取消", rescheduled: "已改期", abnormal: "异常",
};

import { CHANNEL_LABELS } from "@/lib/channels";

export default function GuestsPage() {
  const isMobile = useIsMobile();
  const qc = useQueryClient();
  const [keyword, setKeyword] = useState("");
  const [page, setPage] = useState(1);
  const [selectedGuest, setSelectedGuest] = useState<GuestOut | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const [editOpen, setEditOpen] = useState(false);
  const [editForm] = Form.useForm();

  const { data: guests, isLoading } = useQuery({
    queryKey: ["guests", keyword, page],
    queryFn: () => guestsApi.list({ keyword: keyword || undefined, page }).then((r) => r.data),
  });

  const { data: summary } = useQuery({
    queryKey: ["guests", "summary"],
    queryFn: () => guestsApi.summary().then((r) => r.data),
  });

  const { data: guestOrders } = useQuery({
    queryKey: ["guest-orders", selectedGuest?.guest_id],
    queryFn: () => guestsApi.orders(selectedGuest!.guest_id).then((r) => r.data),
    enabled: !!selectedGuest && detailOpen,
  });

  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: string; data: Record<string, string> }) =>
      guestsApi.update(id, data),
    onSuccess: () => {
      message.success("客人信息已更新");
      qc.invalidateQueries({ queryKey: ["guests"] });
      setEditOpen(false);
    },
  });

  const columns: ColumnsType<GuestOut> = [
    {
      title: "客人", key: "name", width: 140, fixed: "left" as const,
      render: (_, r) => (
        <Space direction="vertical" size={0}>
          <Text strong>{r.name}</Text>
          <Text type="secondary" style={{ fontSize: 11 }}>{r.phone}</Text>
        </Space>
      ),
    },
    {
      title: "入住次数", dataIndex: "visit_count", key: "visits", width: 90,
      sorter: (a, b) => a.visit_count - b.visit_count,
      render: (v) => (
        <Space>
          <Text strong style={{ color: v > 1 ? "#1677ff" : undefined }}>{v}</Text>
          {v > 2 && <Tag color="gold" style={{ fontSize: 10 }}>回头客</Tag>}
        </Space>
      ),
    },
    {
      title: "累计消费", dataIndex: "total_spent", key: "spent", width: 110,
      render: (v) => <Text>¥{Number(v).toLocaleString()}</Text>,
    },
    {
      title: "累计晚数", dataIndex: "total_nights", key: "nights", width: 80,
      render: (v) => `${v} 晚`,
    },
    {
      title: "常住房间", dataIndex: "preferred_room", key: "room", width: 80,
      render: (v) => v || <Text type="secondary">—</Text>,
    },
    {
      title: "最近入住", dataIndex: "last_check_in", key: "last", width: 110,
      render: (v) => v ? dayjs(v).format("YYYY-MM-DD") : "—",
    },
    {
      title: "标签", dataIndex: "tags", key: "tags", width: 120,
      render: (v) => v ? v.split(",").map((t: string) => <Tag key={t} style={{ fontSize: 11 }}>{t}</Tag>) : null,
    },
    {
      title: "操作", key: "action", width: 80,
      render: (_, r) => (
        <Space>
          <a onClick={() => { setSelectedGuest(r); setDetailOpen(true); }}>详情</a>
          <a onClick={() => {
            setSelectedGuest(r);
            editForm.setFieldsValue({ notes: r.notes, tags: r.tags, wechat: r.wechat, id_number: r.id_number });
            setEditOpen(true);
          }}>编辑</a>
        </Space>
      ),
    },
  ];

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <PageHeader title="客人档案" subtitle="客人信息 · 入住历史 · 回头客追踪" />

      {summary && (
        <Row gutter={[16, 16]}>
          <Col xs={12} sm={8}>
            <StatCard title="总客人数" value={summary.total_guests} icon={<UserOutlined />} tone="brand" />
          </Col>
          <Col xs={12} sm={8}>
            <StatCard title="回头客" value={summary.repeat_guests} icon={<StarOutlined />} tone="warn" />
          </Col>
          <Col xs={12} sm={8}>
            <StatCard
              title="复购率"
              value={Number(summary.repeat_rate ?? 0).toFixed(1)}
              suffix="%"
              icon={<RiseOutlined />}
              tone="success"
            />
          </Col>
        </Row>
      )}

      {/* Search + Table / Card list */}
      <Card
        bordered={false}
        style={{
          borderRadius: tokens.radius.lg,
          border: `1px solid ${tokens.color.bg.border}`,
        }}
        styles={{ body: { padding: isMobile ? 12 : 0 } }}
      >
        <div style={{ padding: isMobile ? "0 0 12px" : "16px 20px 0" }}>
          <Input
            placeholder="搜索客人姓名 / 手机号"
            prefix={<SearchOutlined style={{ color: "#bfbfbf" }} />}
            allowClear
            style={{ maxWidth: isMobile ? "100%" : 320 }}
            onChange={(e) => { setKeyword(e.target.value); setPage(1); }}
          />
        </div>
        {isMobile ? (
          <List
            dataSource={guests || []}
            loading={isLoading}
            pagination={{ current: page, pageSize: 20, onChange: setPage, showSizeChanger: false, size: "small" }}
            renderItem={(item: GuestOut) => (
              <div
                key={item.guest_id}
                onClick={() => { setSelectedGuest(item); setDetailOpen(true); }}
                style={{
                  padding: "12px 0",
                  borderBottom: "1px solid #f0f0f0",
                  cursor: "pointer",
                }}
              >
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
                  <div>
                    <Text strong style={{ fontSize: 15 }}>{item.name}</Text>
                    <Text type="secondary" style={{ fontSize: 12, marginLeft: 8 }}>{item.phone}</Text>
                  </div>
                  <Space size={4}>
                    <Tag color="blue" style={{ fontSize: 11, margin: 0 }}>{item.visit_count}次</Tag>
                    {item.visit_count > 2 && <Tag color="gold" style={{ fontSize: 10, margin: 0 }}>回头客</Tag>}
                  </Space>
                </div>
                <div style={{ display: "flex", justifyContent: "space-between", marginTop: 6 }}>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    累计消费: <Text strong style={{ fontSize: 12 }}>¥{Number(item.total_spent).toLocaleString()}</Text>
                  </Text>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    最近入住: {item.last_check_in ? dayjs(item.last_check_in).format("YYYY-MM-DD") : "—"}
                  </Text>
                </div>
              </div>
            )}
          />
        ) : (
          <Table
            columns={columns}
            dataSource={guests}
            rowKey="guest_id"
            loading={isLoading}
            scroll={{ x: 800 }}
            pagination={{ current: page, pageSize: 20, onChange: setPage, showSizeChanger: false, style: { padding: "12px 20px" } }}
            size="middle"
          />
        )}
      </Card>

      {/* Detail Modal */}
      <Modal
        open={detailOpen}
        onCancel={() => setDetailOpen(false)}
        footer={null}
        width={600}
        title={<Space><UserOutlined /><Text strong>客人详情 — {selectedGuest?.name}</Text></Space>}
      >
        {selectedGuest && (
          <>
            <Descriptions column={2} size="small" style={{ marginTop: 8 }}>
              <Descriptions.Item label="姓名">{selectedGuest.name}</Descriptions.Item>
              <Descriptions.Item label="手机">{selectedGuest.phone}</Descriptions.Item>
              <Descriptions.Item label="微信">{selectedGuest.wechat || "—"}</Descriptions.Item>
              <Descriptions.Item label="身份证">{selectedGuest.id_number || "—"}</Descriptions.Item>
              <Descriptions.Item label="入住次数"><Text strong style={{ color: "#1677ff" }}>{selectedGuest.visit_count} 次</Text></Descriptions.Item>
              <Descriptions.Item label="累计消费"><Text strong>¥{Number(selectedGuest.total_spent).toLocaleString()}</Text></Descriptions.Item>
              <Descriptions.Item label="累计晚数">{selectedGuest.total_nights} 晚</Descriptions.Item>
              <Descriptions.Item label="常住房间">{selectedGuest.preferred_room || "—"}</Descriptions.Item>
              {selectedGuest.tags && (
                <Descriptions.Item label="标签" span={2}>
                  {selectedGuest.tags.split(",").map((t) => <Tag key={t}>{t}</Tag>)}
                </Descriptions.Item>
              )}
              {selectedGuest.notes && (
                <Descriptions.Item label="备注" span={2}>{selectedGuest.notes}</Descriptions.Item>
              )}
            </Descriptions>

            <div style={{ marginTop: 16 }}>
              <Text strong style={{ fontSize: 13 }}>入住记录</Text>
              {guestOrders && guestOrders.length > 0 ? (
                <List
                  size="small"
                  dataSource={guestOrders}
                  style={{ marginTop: 8 }}
                  renderItem={(o: GuestOrderHistory) => (
                    <List.Item style={{ padding: "8px 0" }}>
                      <Space style={{ width: "100%", justifyContent: "space-between" }}>
                        <Space>
                          <Text code style={{ fontSize: 11 }}>{o.order_id}</Text>
                          <Tag>{CHANNEL_LABELS[o.channel] || o.channel}</Tag>
                          <Text style={{ fontSize: 12 }}>{o.check_in_date} → {o.check_out_date} ({o.nights}晚)</Text>
                        </Space>
                        <Space>
                          <Text strong>¥{o.actual_price.toLocaleString()}</Text>
                          <Tag color={orderStatusTagColor(o.order_status)}>
                            {ORDER_STATUS_LABELS[o.order_status] || o.order_status}
                          </Tag>
                        </Space>
                      </Space>
                    </List.Item>
                  )}
                />
              ) : (
                <Text type="secondary" style={{ display: "block", marginTop: 8, fontSize: 12 }}>暂无入住记录</Text>
              )}
            </div>
          </>
        )}
      </Modal>

      {/* Edit Modal */}
      <Modal
        open={editOpen}
        onCancel={() => setEditOpen(false)}
        title="编辑客人信息"
        onOk={() => editForm.submit()}
        confirmLoading={updateMutation.isPending}
        okText="保存" cancelText="取消"
      >
        <Form
          form={editForm}
          layout="vertical"
          onFinish={(values) => selectedGuest && updateMutation.mutate({ id: selectedGuest.guest_id, data: values })}
        >
          <Form.Item label="微信号" name="wechat"><Input placeholder="可选" /></Form.Item>
          <Form.Item label="身份证号" name="id_number"><Input placeholder="可选" /></Form.Item>
          <Form.Item label="标签" name="tags"><Input placeholder="逗号分隔，如: VIP,回头客,长租" /></Form.Item>
          <Form.Item label="备注" name="notes"><Input.TextArea rows={3} placeholder="客人偏好、特殊需求等" /></Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
