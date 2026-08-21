"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Descriptions, Empty, Radio, Select, Space, Table, Tag, Typography, Upload, message } from "antd";
import { InboxOutlined, DownloadOutlined } from "@ant-design/icons";
import { PageHeader } from "@/components/ui/PageHeader";
import { useAuthStore } from "@/lib/auth";
import { extractErrorMessage, utilityReconApi } from "@/lib/api";
import { ANOMALY_LABELS, CATEGORY_LABELS, type UtilityFloorSummary, type UtilityRow, type UtilitySuggestion } from "@/lib/utility-recon";

const { Text, Title } = Typography;

function money(value: string | undefined) {
  const amount = Number(value ?? 0);
  return `${amount >= 0 ? "" : "-"}¥${Math.abs(amount).toFixed(2)}`;
}

export default function UtilityReconPage() {
  const router = useRouter();
  const { user } = useAuthStore();
  const qc = useQueryClient();
  const allowed = !!user && ["admin", "finance", "operator"].includes(user.role);
  const canDecide = !!user && ["admin", "finance"].includes(user.role);
  const [files, setFiles] = useState<File[]>([]);
  const [activeBatchId, setActiveBatchId] = useState<string | null>(null);
  const [view, setView] = useState<"raw" | "corrected">("raw");
  const [floor, setFloor] = useState<string | undefined>();

  useEffect(() => { if (user && !allowed) router.replace("/dashboard"); }, [user, allowed, router]);

  const preflight = useMutation({
    mutationFn: () => utilityReconApi.preflight(files),
    onError: (error) => message.error(extractErrorMessage(error, "预检失败")),
  });
  const run = useMutation({
    mutationFn: () => utilityReconApi.run(files),
    onSuccess: ({ data }) => {
      message.success(`已生成 ${data.batches.length} 个月度对账结果`);
      setActiveBatchId(data.batches[0]?.batch_id ?? null);
      qc.invalidateQueries({ queryKey: ["utility-recon"] });
    },
    onError: (error) => message.error(extractErrorMessage(error, "对账失败")),
  });
  const batches = useQuery({
    queryKey: ["utility-recon", "batches"], enabled: allowed,
    queryFn: async () => (await utilityReconApi.batches()).data,
  });
  const batchId = activeBatchId ?? batches.data?.[0]?.batch_id ?? null;
  const detail = useQuery({
    queryKey: ["utility-recon", "detail", batchId], enabled: allowed && !!batchId,
    queryFn: async () => (await utilityReconApi.detail(batchId!)).data,
  });
  const decide = useMutation({
    mutationFn: ({ id, action }: { id: string; action: "adopt" | "revert" }) => action === "adopt" ? utilityReconApi.adopt(id) : utilityReconApi.revert(id),
    onSuccess: () => { message.success("处理结果已保存"); qc.invalidateQueries({ queryKey: ["utility-recon"] }); },
    onError: (error) => message.error(extractErrorMessage(error, "操作失败")),
  });

  const summary = view === "raw" ? detail.data?.batch.raw_summary : detail.data?.batch.corrected_summary;
  const floors = useMemo(() => Array.from(new Set(summary?.by_floor_category.map((item) => item.floor) ?? [])), [summary]);
  const summaryRows = summary?.by_floor_category.filter((item) => !floor || item.floor === floor) ?? [];
  const differenceRows = detail.data?.rows.filter((row) => row.disposition === "valid" && (!floor || row.floor === floor)) ?? [];
  const excludedRows = detail.data?.rows.filter((row) => row.disposition !== "valid") ?? [];

  async function download() {
    if (!batchId) return;
    try {
      const response = await utilityReconApi.export(batchId);
      const url = URL.createObjectURL(response.data);
      const anchor = document.createElement("a"); anchor.href = url;
      anchor.download = `水电费对账结果_${detail.data?.batch.month ?? ""}.xlsx`; anchor.click();
      URL.revokeObjectURL(url);
    } catch (error) { message.error(extractErrorMessage(error, "导出失败")); }
  }

  if (!allowed) return null;
  return <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
    <PageHeader title="水电费对账" subtitle="上传已收明细和费用明细，自动按月份、楼层和水电项目找出差异" />

    <Card title="1. 上传两份 Excel">
      <Upload.Dragger
        multiple maxCount={2} accept=".xls,.xlsx" fileList={files.map((file) => ({ uid: `${file.name}-${file.size}`, name: file.name, status: "done", originFileObj: file } as never))}
        beforeUpload={(file) => { setFiles((current) => [...current, file].slice(-2)); preflight.reset(); return false; }}
        onRemove={(removed) => { setFiles((current) => current.filter((file) => file.name !== removed.name)); preflight.reset(); }}
      >
        <p className="ant-upload-drag-icon"><InboxOutlined /></p>
        <p>同时选择“住户已收明细”和“公司费用明细”</p>
        <Text type="secondary">系统会自动判断文件角色并按共同月份拆开，不会修改原文件</Text>
      </Upload.Dragger>
      <Space style={{ marginTop: 16 }}>
        <Button disabled={files.length !== 2} loading={preflight.isPending} onClick={() => preflight.mutate()}>检查文件</Button>
        <Button type="primary" disabled={!preflight.data?.data.common_months.length} loading={run.isPending} onClick={() => run.mutate()}>开始对账</Button>
      </Space>
      {preflight.data && <div style={{ marginTop: 16 }}>
        <Descriptions bordered size="small" column={2} items={preflight.data.data.files.map((item) => ({ key: item.role, label: item.role === "receipt" ? "已收流水" : "费用流水", children: `${item.filename}（${item.months.join("、") || "未识别月份"}）` }))} />
        <Alert style={{ marginTop: 12 }} type={preflight.data.data.common_months.length ? "success" : "warning"} showIcon message={`可对账月份：${preflight.data.data.common_months.join("、") || "无"}`} description={[...preflight.data.data.receipt_only_months.map((m) => `${m} 缺少费用表`), ...preflight.data.data.expense_only_months.map((m) => `${m} 缺少已收表`)].join("；") || "月份范围一致"} />
      </div>}
    </Card>

    <Card title="2. 对账结果" extra={<Space><Select placeholder="历史批次" value={batchId ?? undefined} style={{ width: 180 }} options={batches.data?.map((item) => ({ value: item.batch_id, label: `${item.month} · ${money(item.raw_difference)}` }))} onChange={setActiveBatchId} />{canDecide && batchId && <Button icon={<DownloadOutlined />} onClick={download}>导出结果</Button>}</Space>}>
      {!detail.data ? <Empty description="上传并运行后，这里会显示每个月的结果" /> : <Space direction="vertical" size={16} style={{ width: "100%" }}>
        <Alert type={Number(detail.data.batch.raw_difference) === 0 ? "success" : "warning"} showIcon message={`${detail.data.batch.month}：已收 ${money(detail.data.batch.raw_summary.receipt_total)}，费用 ${money(detail.data.batch.raw_summary.expense_total)}，原始差额 ${money(detail.data.batch.raw_difference)}`} description={`采纳确认过的建议后差额：${money(detail.data.batch.corrected_difference)}。正数表示已收更多，负数表示费用更多。`} />
        <Space><Radio.Group value={view} onChange={(event) => setView(event.target.value)} options={[{ label: "按原表查看", value: "raw" }, { label: "按已采纳修正查看", value: "corrected" }]} /><Select allowClear placeholder="全部楼层" value={floor} onChange={setFloor} options={floors.map((item) => ({ value: item, label: item }))} /></Space>
        <Table<UtilityFloorSummary> rowKey={(row) => `${row.floor}-${row.category}`} pagination={false} dataSource={summaryRows} columns={[
          { title: "楼层", dataIndex: "floor" }, { title: "项目", dataIndex: "category", render: (value) => CATEGORY_LABELS[value as keyof typeof CATEGORY_LABELS] },
          { title: "已收", dataIndex: "receipt", render: money }, { title: "费用", dataIndex: "expense", render: money },
          { title: "差额（已收－费用）", dataIndex: "difference", render: (value) => <Text type={Number(value) === 0 ? undefined : "danger"}>{money(value)}</Text> },
        ]} />
        <Title level={5}>需要确认的异常</Title>
        {(detail.data.suggestions.length === 0) ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有发现可自动解释的异常" /> : detail.data.suggestions.map((item: UtilitySuggestion) => <Card size="small" key={item.suggestion_id} title={ANOMALY_LABELS[item.kind] ?? item.kind} extra={<Tag color={item.confidence === "high" ? "green" : "orange"}>{item.confidence === "high" ? "高可信" : "需核对"}</Tag>}><Space><Text>证据：{Object.entries(item.evidence).map(([key, value]) => `${key}=${value}`).join("，")}</Text>{canDecide && item.patch && Object.keys(item.patch).length > 0 && <><Button type="primary" disabled={item.status === "adopted"} onClick={() => decide.mutate({ id: item.suggestion_id, action: "adopt" })}>采纳建议</Button><Button disabled={item.status === "reverted"} onClick={() => decide.mutate({ id: item.suggestion_id, action: "revert" })}>撤销</Button></>}</Space></Card>)}
        <Title level={5}>逐笔来源</Title>
        <Table<UtilityRow> rowKey="row_id" size="small" dataSource={differenceRows} columns={[{ title: "日期", dataIndex: "business_date" }, { title: "方向", dataIndex: "side", render: (value) => value === "receipt" ? "已收" : "费用" }, { title: "楼层", dataIndex: "floor" }, { title: "房间", dataIndex: "room", render: (value) => value || "—" }, { title: "项目", dataIndex: "category", render: (value) => value ? CATEGORY_LABELS[value as keyof typeof CATEGORY_LABELS] : "未识别" }, { title: "金额", dataIndex: "amount", render: money }, { title: "来源", render: (_, row) => `${row.source_filename} / ${row.source_sheet} / 第${row.source_row_number}行` }]} />
        {excludedRows.length > 0 && <Alert type="info" showIcon message={`另有 ${excludedRows.length} 条未参与对账`} description={excludedRows.map((row) => `${row.source_filename} 第${row.source_row_number}行：${row.exclusion_reason}`).join("；")} />}
      </Space>}
    </Card>
  </div>;
}
