"use client";

import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { settlementsApi, exportApi } from "@/lib/api";
import { extractErrorMessage } from "@/lib/api-errors";
import { downloadBlob, fmtMille, MILLE_NOTE } from "@/lib/utils";
import type { OwnerSettlementOut } from "@/lib/types";
import {
  Table, Typography, Card, Tag, Button, Space, DatePicker, Checkbox, Popconfirm,
  Modal, message, Input, Descriptions, List, Drawer, Row, Col, Statistic, Dropdown,
} from "antd";
import { CalculatorOutlined, CheckCircleOutlined, ExclamationCircleOutlined, DownloadOutlined, DownOutlined } from "@ant-design/icons";
import type { ColumnsType } from "antd/es/table";
import dayjs from "dayjs";
import { useAuthStore } from "@/lib/auth";
import { useIsMobile } from "@/lib/responsive";

const { Title, Text } = Typography;

const STATUS_MAP: Record<string, { color: string; label: string }> = {
  pending: { color: "gold", label: "待确认" },
  confirmed: { color: "green", label: "已确认" },
  paid: { color: "blue", label: "已打款" },
  disputed: { color: "red", label: "有争议" },
};

// 金额统一：千分位 + 固定两位小数（避免 1,795.7 / 2,903.3 这类不齐）
const fmtYuan = (v: number | string) =>
  Number(v).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

export default function SettlementsPage() {
  const isMobile = useIsMobile();
  const qc = useQueryClient();
  const { user } = useAuthStore();
  const isAdmin = user?.role === "admin";
  const [selectedMonth, setSelectedMonth] = useState<string | undefined>();
  const [disputeOpen, setDisputeOpen] = useState(false);
  const [disputeId, setDisputeId] = useState<string>("");
  const [disputeNotes, setDisputeNotes] = useState("");
  const [detailOpen, setDetailOpen] = useState(false);
  const [selectedSettlement, setSelectedSettlement] = useState<OwnerSettlementOut | null>(null);
  const [exportLoading, setExportLoading] = useState(false);
  const [statementExportingId, setStatementExportingId] = useState<string | null>(null);
  const [overwrite, setOverwrite] = useState(false);
  // 生成/重算的目标月，独立于上方列表的「筛选结算月」。默认上月。
  const [genMonth, setGenMonth] = useState(() => dayjs().subtract(1, "month"));

  const handleExportSettlements = async () => {
    setExportLoading(true);
    try {
      const res = await exportApi.settlements(selectedMonth);
      downloadBlob(res.data, `settlements_${selectedMonth || "all"}.xlsx`);
      message.success("导出成功");
    } catch {
      message.error("导出失败");
    } finally {
      setExportLoading(false);
    }
  };

  // 单条结算 → 『业主分成明细 + 垫付费用明细』Excel（可直接发给业主）
  const handleExportStatement = async (r: OwnerSettlementOut) => {
    setStatementExportingId(r.settlement_id);
    try {
      const res = await exportApi.settlementStatement(r.settlement_id);
      downloadBlob(res.data, `业主分成明细_${r.owner_id}_${r.billing_month}.xlsx`);
      message.success("导出成功");
    } catch {
      message.error("导出失败");
    } finally {
      setStatementExportingId(null);
    }
  };

  const handleExportIncome = async (r: OwnerSettlementOut) => {
    setStatementExportingId(r.settlement_id);
    try {
      const res = await exportApi.settlementIncomeDetail(r.settlement_id);
      downloadBlob(res.data, `到账收入明细_${r.owner_id}_${r.billing_month}.xlsx`);
      message.success("收入明细已导出，合计与结算快照一致");
    } catch (e) {
      message.error(extractErrorMessage(e, "导出失败"));
    } finally {
      setStatementExportingId(null);
    }
  };

  const handleExportPackage = async (r: OwnerSettlementOut) => {
    setStatementExportingId(r.settlement_id);
    try {
      const res = await exportApi.settlementPackage(r.settlement_id);
      downloadBlob(res.data, `完整结算包_${r.owner_id}_${r.billing_month}.xlsx`);
      message.success("完整结算包已导出");
    } catch (e) {
      message.error(extractErrorMessage(e, "导出失败"));
    } finally {
      setStatementExportingId(null);
    }
  };

  const { data: settlements, isLoading } = useQuery({
    queryKey: ["settlements", selectedMonth],
    queryFn: () => settlementsApi.list({ billing_month: selectedMonth }).then((r) => r.data),
  });

  const { data: detail, isLoading: detailLoading } = useQuery({
    queryKey: ["settlement-detail", selectedSettlement?.settlement_id],
    queryFn: () =>
      settlementsApi.detail(selectedSettlement!.settlement_id).then((r) => r.data),
    enabled: detailOpen && !!selectedSettlement?.settlement_id,
  });

  const generateMutation = useMutation({
    mutationFn: ({ year, month, overwrite }: { year: number; month: number; overwrite?: boolean }) =>
      settlementsApi.generate(year, month, overwrite),
    onSuccess: (res, vars) => {
      const { generated, regenerated, skipped_locked } = res.data;
      const parts: string[] = [];
      if (generated) parts.push(`新建 ${generated} 条`);
      if (regenerated) parts.push(`重算 ${regenerated} 条`);
      if (skipped_locked) parts.push(`${skipped_locked} 条已确认/已打款未动`);
      if (parts.length === 0) {
        message.info(
          vars.overwrite
            ? "该月没有可重算的待确认结算单"
            : "该月已生成过结算，如需重算请勾选「覆盖重算」",
        );
      } else {
        message.success(`已完成：${parts.join("，")}`);
      }
      qc.invalidateQueries({ queryKey: ["settlements"] });
    },
    onError: () => message.error("生成失败"),
  });

  const confirmMutation = useMutation({
    mutationFn: (id: string) => settlementsApi.confirm(id),
    onSuccess: () => {
      message.success("结算已确认");
      qc.invalidateQueries({ queryKey: ["settlements"] });
    },
    onError: (e) => message.error(extractErrorMessage(e, "确认失败")),
  });

  const disputeMutation = useMutation({
    mutationFn: ({ id, notes }: { id: string; notes: string }) =>
      settlementsApi.dispute(id, notes),
    onSuccess: () => {
      message.success("已标记争议");
      qc.invalidateQueries({ queryKey: ["settlements"] });
      setDisputeOpen(false);
    },
  });

  const columns: ColumnsType<OwnerSettlementOut> = [
    { title: "结算月", dataIndex: "billing_month", key: "month", width: 100 },
    { title: "业主", dataIndex: "owner_id", key: "owner", width: 120 },
    {
      title: "净收入", dataIndex: "total_net_revenue", key: "revenue", width: 110,
      render: (v) => `¥${fmtYuan(v ?? 0)}`,
    },
    {
      title: "业主应得", key: "owner_amount", width: 120,
      render: (_, r) => (
        <Text strong style={{ color: "#10B981" }}>
          ¥{fmtMille(r.actual_owner_amount_precise, r.actual_owner_amount ?? r.owner_amount ?? 0)}
        </Text>
      ),
    },
    {
      title: "扣除支出", dataIndex: "deducted_expenses", key: "expenses", width: 100,
      render: (v) => Number(v) > 0 ? <Text type="danger">-¥{fmtYuan(v)}</Text> : "¥0.00",
    },
    {
      title: "状态", dataIndex: "status", key: "status", width: 90,
      render: (v) => {
        const cfg = STATUS_MAP[v] || { color: "default", label: v };
        return <Tag color={cfg.color}>{cfg.label}</Tag>;
      },
    },
    {
      title: "操作", key: "action", width: 230,
      render: (_, r) => (
        <Space>
          <a onClick={() => { setSelectedSettlement(r); setDetailOpen(true); }}>详情</a>
          <Dropdown
            disabled={statementExportingId === r.settlement_id}
            menu={{ items: [
              { key: "package", label: "完整结算包", onClick: () => handleExportPackage(r) },
              { key: "income", label: "到账收入明细", onClick: () => handleExportIncome(r) },
              { key: "statement", label: "业主分成表", onClick: () => handleExportStatement(r) },
            ] }}
          >
            <a onClick={(e) => e.preventDefault()}>
              {statementExportingId === r.settlement_id ? "导出中…" : <>导出 <DownOutlined /></>}
            </a>
          </Dropdown>
          {r.status === "pending" && (
            <>
              <a onClick={() => confirmMutation.mutate(r.settlement_id)}>确认</a>
              <a style={{ color: "#ff4d4f" }} onClick={() => {
                setDisputeId(r.settlement_id);
                setDisputeNotes("");
                setDisputeOpen(true);
              }}>争议</a>
            </>
          )}
        </Space>
      ),
    },
  ];

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      <div style={{ display: "flex", flexDirection: isMobile ? "column" : "row", alignItems: isMobile ? "flex-start" : "center", justifyContent: "space-between", gap: isMobile ? 12 : 0 }}>
        <div>
          <Title level={4} style={{ margin: 0 }}>业主结算</Title>
          <Text type="secondary">按月生成业主收益结算报表</Text>
          <div><Text type="secondary" style={{ fontSize: 12 }}>{MILLE_NOTE}</Text></div>
        </div>
        <Space wrap>
          <Button icon={<DownloadOutlined />} loading={exportLoading} onClick={handleExportSettlements}>
            导出Excel
          </Button>
          {isAdmin && (() => {
            const monthLabel = genMonth.format("YYYY-MM");
            const doGenerate = () =>
              generateMutation.mutate({
                year: genMonth.year(), month: genMonth.month() + 1, overwrite,
              });
            const btn = (
              <Button
                type="primary"
                icon={<CalculatorOutlined />}
                onClick={overwrite ? undefined : doGenerate}
                loading={generateMutation.isPending}
              >
                {overwrite ? `重算 ${monthLabel} 结算` : `生成 ${monthLabel} 结算`}
              </Button>
            );
            return (
              <Space wrap>
                <DatePicker
                  picker="month"
                  value={genMonth}
                  allowClear={false}
                  format="YYYY-MM"
                  placeholder="选择结算月"
                  onChange={(d) => d && setGenMonth(d)}
                  style={{ width: isMobile ? 130 : undefined }}
                />
                <Checkbox checked={overwrite} onChange={(e) => setOverwrite(e.target.checked)}>
                  覆盖重算
                </Checkbox>
                {overwrite ? (
                  <Popconfirm
                    title="重新生成会覆盖该月的待确认结算单"
                    description="已确认/已打款的单会被自动保护、不受影响。确定重算？"
                    okText="确定重算"
                    cancelText="取消"
                    onConfirm={doGenerate}
                  >
                    {btn}
                  </Popconfirm>
                ) : btn}
              </Space>
            );
          })()}
        </Space>
      </div>

      <Card bordered={false} style={{ borderRadius: 12 }} styles={{ body: { padding: isMobile ? 12 : 0 } }}>
        <div style={{ padding: isMobile ? "0 0 12px" : "16px 20px 0" }}>
          <DatePicker
            picker="month"
            placeholder="筛选结算月"
            allowClear
            style={{ width: isMobile ? "100%" : undefined }}
            onChange={(d) => setSelectedMonth(d ? d.format("YYYY-MM") : undefined)}
          />
        </div>
        {isMobile ? (
          <List
            dataSource={settlements || []}
            loading={isLoading}
            pagination={{ pageSize: 20, showSizeChanger: false, size: "small" }}
            renderItem={(item: OwnerSettlementOut) => {
              const cfg = STATUS_MAP[item.status] || { color: "default", label: item.status };
              return (
                <div
                  key={item.settlement_id}
                  style={{
                    padding: "12px 0",
                    borderBottom: "1px solid #f0f0f0",
                  }}
                >
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                    <div>
                      <Text strong style={{ fontSize: 14 }}>{item.billing_month}</Text>
                      <Text type="secondary" style={{ fontSize: 12, marginLeft: 8 }}>{item.owner_id}</Text>
                    </div>
                    <Tag color={cfg.color} style={{ margin: 0 }}>{cfg.label}</Tag>
                  </div>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginTop: 8 }}>
                    <Text strong style={{ color: "#10B981", fontSize: 15 }}>¥{fmtMille(item.actual_owner_amount_precise, item.actual_owner_amount ?? item.owner_amount ?? 0)}</Text>
                    <Space size={12}>
                      <a onClick={() => { setSelectedSettlement(item); setDetailOpen(true); }}>详情</a>
                      <a
                        style={statementExportingId === item.settlement_id ? { color: "#999", pointerEvents: "none" } : undefined}
                        onClick={() => handleExportStatement(item)}
                      >
                        {statementExportingId === item.settlement_id ? "导出中…" : "导出明细"}
                      </a>
                      {item.status === "pending" && (
                        <>
                          <a onClick={() => confirmMutation.mutate(item.settlement_id)}>确认</a>
                          <a style={{ color: "#ff4d4f" }} onClick={() => {
                            setDisputeId(item.settlement_id);
                            setDisputeNotes("");
                            setDisputeOpen(true);
                          }}>争议</a>
                        </>
                      )}
                    </Space>
                  </div>
                </div>
              );
            }}
          />
        ) : (
          <Table
            columns={columns}
            dataSource={settlements}
            rowKey="settlement_id"
            loading={isLoading}
            scroll={{ x: 700 }}
            pagination={{ pageSize: 20, showSizeChanger: false }}
            size="middle"
          />
        )}
      </Card>

      {/* Detail Drawer — 含按房号明细 */}
      <Drawer
        open={detailOpen}
        onClose={() => setDetailOpen(false)}
        width={isMobile ? "100%" : 720}
        title={
          <Space>
            <Text strong>结算详情</Text>
            <Text type="secondary">
              {selectedSettlement?.billing_month} · 业主 {selectedSettlement?.owner_id}
            </Text>
            {selectedSettlement && (
              <Tag color={STATUS_MAP[selectedSettlement.status]?.color} style={{ margin: 0 }}>
                {STATUS_MAP[selectedSettlement.status]?.label}
              </Tag>
            )}
          </Space>
        }
      >
        {selectedSettlement && (
          <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
            <Row gutter={[12, 12]}>
              <Col xs={12} md={6}>
                <Card size="small">
                  <Statistic
                    title="净收入"
                    value={Number(detail?.total_net_revenue ?? selectedSettlement.total_net_revenue ?? 0)}
                    prefix="¥"
                    precision={2}
                    valueStyle={{ fontSize: 20, whiteSpace: "nowrap" }}
                  />
                </Card>
              </Col>
              <Col xs={12} md={6}>
                <Card size="small">
                  <Statistic
                    title="业主承担支出"
                    value={Number(detail?.deducted_expenses ?? selectedSettlement.deducted_expenses ?? 0)}
                    prefix="¥"
                    precision={2}
                    valueStyle={{ color: "#EF4444", fontSize: 20, whiteSpace: "nowrap" }}
                  />
                </Card>
              </Col>
              <Col xs={12} md={6}>
                <Card size="small">
                  <Statistic
                    title="业主应得"
                    value={Number(
                      detail?.actual_owner_amount_precise ??
                      detail?.actual_owner_amount ??
                      selectedSettlement.actual_owner_amount_precise ??
                      selectedSettlement.actual_owner_amount ??
                      selectedSettlement.owner_amount ?? 0
                    )}
                    prefix="¥"
                    precision={
                      (detail?.actual_owner_amount_precise ?? selectedSettlement.actual_owner_amount_precise) != null ? 3 : 2
                    }
                    valueStyle={{ color: "#10B981", fontSize: 20, whiteSpace: "nowrap" }}
                  />
                </Card>
              </Col>
              <Col xs={12} md={6}>
                <Card size="small">
                  <Statistic
                    title="涉及房间"
                    // 只数有房号的行；业主级支出行（room_id 为空，如采购费/水电费）不是房间
                    value={detail?.items?.filter((it) => it.room_id).length ?? 0}
                    suffix="套"
                    valueStyle={{ fontSize: 20, whiteSpace: "nowrap" }}
                  />
                </Card>
              </Col>
            </Row>

            <Text type="secondary" style={{ fontSize: 12 }}>{MILLE_NOTE}</Text>

            <Card
              size="small"
              title={<Text strong>按房号明细</Text>}
              loading={detailLoading}
            >
              <Table
                dataSource={detail?.items ?? []}
                rowKey="item_id"
                pagination={false}
                size="small"
                scroll={{ x: 720 }}
                locale={{ emptyText: "此结算未生成按房号的明细（旧版结算）" }}
                columns={[
                  { title: "房号", dataIndex: "room_id", width: 80, render: (v) => v ?? "—" },
                  {
                    title: "房名",
                    dataIndex: "room_name",
                    width: 140,
                    // 业主级支出行（room_id 空）无房名，显示 label（如「电费·整层」）
                    render: (v, r: any) => v || r.label || "—",
                  },
                  { title: "订单", dataIndex: "order_count", width: 60, align: "right" },
                  {
                    title: "收入",
                    dataIndex: "revenue",
                    width: 100,
                    align: "right",
                    className: "col-amount",
                    render: (v) => <span className="tabular">¥{fmtYuan(v)}</span>,
                  },
                  {
                    title: "净收入",
                    dataIndex: "net_revenue",
                    width: 100,
                    align: "right",
                    className: "col-amount",
                    render: (v) => <span className="tabular">¥{fmtYuan(v)}</span>,
                  },
                  {
                    title: "扣除支出",
                    dataIndex: "owner_expenses",
                    width: 112,
                    align: "right",
                    className: "col-amount",
                    // whiteSpace:nowrap —— 四位数带千位逗号(如 -¥3,200.00)在窄列里会折行，
                    // 负号被挤到上一行，业主/王总会误以为"漏了负号"。强制不折行，整体走横向滚动。
                    render: (v) =>
                      Number(v) > 0 ? (
                        <Text type="danger" className="tabular" style={{ whiteSpace: "nowrap" }}>-¥{fmtYuan(v)}</Text>
                      ) : (
                        <Text type="secondary" className="tabular" style={{ whiteSpace: "nowrap" }}>¥0.00</Text>
                      ),
                  },
                  {
                    title: "比例",
                    dataIndex: "share_ratio_snapshot",
                    width: 70,
                    align: "right",
                    render: (v) => (
                      <Tag color="gold" bordered={false}>
                        {Math.round(Number(v) * 100)}%
                      </Tag>
                    ),
                  },
                  {
                    title: "业主应得",
                    dataIndex: "owner_net_amount",
                    width: 120,
                    align: "right",
                    className: "col-amount",
                    render: (v, r: any) => (
                      <Text strong style={{ color: "#2B2721", whiteSpace: "nowrap" }} className="tabular">
                        ¥{fmtMille(r.owner_net_amount_precise, v)}
                      </Text>
                    ),
                  },
                ]}
              />
            </Card>

            <Descriptions column={1} size="small" bordered>
              <Descriptions.Item label="业主">{selectedSettlement.owner_id}</Descriptions.Item>
              <Descriptions.Item label="结算月">{selectedSettlement.billing_month}</Descriptions.Item>
              <Descriptions.Item label="结算单号">{selectedSettlement.settlement_id}</Descriptions.Item>
              {selectedSettlement.notes && (
                <Descriptions.Item label="备注">{selectedSettlement.notes}</Descriptions.Item>
              )}
            </Descriptions>

            {selectedSettlement.status === "pending" && (
              <Space>
                <Button
                  type="primary"
                  icon={<CheckCircleOutlined />}
                  onClick={() => confirmMutation.mutate(selectedSettlement.settlement_id)}
                  loading={confirmMutation.isPending}
                >
                  确认结算
                </Button>
                <Button
                  danger
                  icon={<ExclamationCircleOutlined />}
                  onClick={() => {
                    setDisputeId(selectedSettlement.settlement_id);
                    setDisputeNotes("");
                    setDisputeOpen(true);
                  }}
                >
                  标记争议
                </Button>
              </Space>
            )}
          </div>
        )}
      </Drawer>

      {/* Dispute Modal */}
      <Modal
        open={disputeOpen}
        onCancel={() => setDisputeOpen(false)}
        title={<Space><ExclamationCircleOutlined style={{ color: "#ff4d4f" }} /><Text strong>标记争议</Text></Space>}
        onOk={() => disputeMutation.mutate({ id: disputeId, notes: disputeNotes })}
        confirmLoading={disputeMutation.isPending}
        okText="提交" cancelText="取消"
        okButtonProps={{ danger: true }}
      >
        <Input.TextArea
          rows={4}
          value={disputeNotes}
          onChange={(e) => setDisputeNotes(e.target.value)}
          placeholder="请说明争议原因..."
        />
      </Modal>
    </div>
  );
}
