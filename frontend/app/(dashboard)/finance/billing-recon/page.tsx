"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert, Button, Card, Descriptions, Modal, Popconfirm, Space, Table, Tag, Typography, Upload, message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { InboxOutlined } from "@ant-design/icons";
import { PageHeader } from "@/components/ui/PageHeader";
import { useAuthStore } from "@/lib/auth";
import { extractErrorMessage } from "@/lib/api-errors";
import { billingReconApi, ordersApi, type ReconBatchDetail } from "@/lib/api";
import { OrderQuickSearch } from "@/components/rooms/OrderQuickSearch";
import {
  ACTIONS_BY_CLASS, CLAIM_CONFIDENCE_SINGLE, DIFF_CLASS_META, DIFF_STATUS_META,
  amountExplanation, claimReason, compensationLabel, shortPaidAmount,
  type ClaimCandidate, type DiffActionDef, type ReconBatchOut, type ReconDiffOut,
} from "@/lib/billing-recon";

const { Text } = Typography;

function formatUploadTime(value: string | null | undefined): string {
  if (!value) return "时间未知";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

// 单条动作确认文案：fix_amount 命中赔款（detail.has_compensation）时把赔款金额带进确认框，
// 采纳前让人知道账单净额里已经扣了这笔钱（终稿 R5）。
function confirmTitle(d: ReconDiffOut, a: DiffActionDef): string {
  const base = `${a.label}？${DIFF_CLASS_META[d.diff_class].hint}`;
  const comp = a.action === "adopt" && d.diff_class === "fix_amount" ? compensationLabel(d.detail) : null;
  return comp ? `${base}（${comp}，账单净额已扣除赔款）` : base;
}

export default function BillingReconPage() {
  const { user } = useAuthStore();
  const router = useRouter();
  const isAdmin = !!user && user.role === "admin";

  useEffect(() => {
    if (user && user.role !== "admin") {
      router.replace("/dashboard");
    }
  }, [user, router]);

  const qc = useQueryClient();
  const [activeBatchId, setActiveBatchId] = useState<string | null>(null);
  const [pickerDiff, setPickerDiff] = useState<ReconDiffOut | null>(null);
  const [selectedOrderId, setSelectedOrderId] = useState<string | null>(null);
  const [viewOrderId, setViewOrderId] = useState<string | null>(null);

  useEffect(() => {
    const requested = new URLSearchParams(window.location.search).get("batch");
    if (requested) setActiveBatchId(requested);
  }, []);

  const batches = useQuery({
    queryKey: ["billing-recon", "batches"],
    enabled: isAdmin,
    queryFn: async () => (await billingReconApi.batches()).data,
  });

  const batchId = activeBatchId ?? batches.data?.[0]?.batch_id ?? null;

  const detail = useQuery({
    queryKey: ["billing-recon", "batch", batchId],
    enabled: isAdmin && !!batchId,
    queryFn: async () => (await billingReconApi.batchDetail(batchId!)).data,
  });

  const selectedOrder = useQuery({
    queryKey: ["billing-recon", "selected-order", selectedOrderId],
    queryFn: () => ordersApi.get(selectedOrderId!).then((r) => r.data),
    enabled: !!selectedOrderId,
  });
  const viewedOrder = useQuery({
    queryKey: ["billing-recon", "view-order", viewOrderId],
    queryFn: () => ordersApi.get(viewOrderId!).then((r) => r.data),
    enabled: !!viewOrderId,
  });

  useEffect(() => {
    if (detail.isError && activeBatchId && batches.data?.[0]
      && activeBatchId !== batches.data[0].batch_id) {
      message.warning("指定的对账批次不可用，已为你打开最近批次");
      setActiveBatchId(batches.data[0].batch_id);
    }
  }, [detail.isError, activeBatchId, batches.data]);

  const upload = useMutation({
    mutationFn: (file: File) => billingReconApi.upload(file),
    onSuccess: (resp) => {
      const d: ReconBatchDetail = resp.data;
      message.success(`对账完成：${d.batch.bill_month} 共 ${d.diffs.length} 条差异`);
      setActiveBatchId(d.batch.batch_id);
      qc.invalidateQueries({ queryKey: ["billing-recon"] });
    },
    onError: (e: unknown) => {
      // 三种失败形态都要给人话：422 拒收（detail 是 {errors,batch_id} 或纯字符串）、
      // 503 AI 不可用（detail 是字符串）、其余走通用 extractErrorMessage（超时/网络/500）。
      const ax = e as { response?: { data?: { detail?: unknown } } };
      const detail = ax?.response?.data?.detail;
      const errors = detail && typeof detail === "object" && "errors" in (detail as Record<string, unknown>)
        ? (detail as { errors?: unknown }).errors
        : null;
      Modal.error({
        title: "账单被拒收",
        content: Array.isArray(errors)
          ? errors.join("；")
          : typeof detail === "string"
            ? detail
            : extractErrorMessage(e, "上传失败"),
      });
    },
  });

  // R5：onSuccess 只做 invalidate + message.success；结算警告弹窗移到调用点（onConfirm /
  // 全部采纳循环），避免连续操作时警告弹窗此起彼伏盖住操作区。
  const act = useMutation({
    mutationFn: ({ diffId, action }: { diffId: string; action: string }) =>
      billingReconApi.diffAction(diffId, action),
    onSuccess: (resp) => {
      message.success(`已处理：${DIFF_STATUS_META[resp.data.status].label}`);
      qc.invalidateQueries({ queryKey: ["billing-recon"] });
    },
    onError: (e: unknown) => message.error(extractErrorMessage(e, "操作失败")),
  });

  // AI 候选认领：只把某行差异链接到系统单（写 order_id），不改任何金额——
  // 后端 claim 端点本身就是「链接」语义，前端这里不重复计算/展示钱。
  const claim = useMutation({
    mutationFn: ({ diffId, orderId }: { diffId: string; orderId: string }) =>
      billingReconApi.claim(diffId, orderId),
    onSuccess: () => {
      message.success("已认领");
      setPickerDiff(null);
      setSelectedOrderId(null);
      qc.invalidateQueries({ queryKey: ["billing-recon"] });
    },
    onError: (e: unknown) => message.error(extractErrorMessage(e, "认领失败")),
  });

  const review = useMutation({
    mutationFn: (id: string) => billingReconApi.review(id),
    onSuccess: () => {
      message.success("本月账单已确认核对完成");
      qc.invalidateQueries({ queryKey: ["billing-recon"] });
    },
    onError: (e: unknown) => message.error(extractErrorMessage(e, "确认失败")),
  });

  const archive = useMutation({
    mutationFn: (id: string) => billingReconApi.archive(id),
    onSuccess: (_resp, id) => {
      qc.setQueryData<ReconBatchOut[]>(["billing-recon", "batches"],
        (current) => current?.filter((batch) => batch.batch_id !== id));
      setActiveBatchId(null);
      message.success("本次对账结果已关闭");
    },
    onError: (e: unknown) => message.error(extractErrorMessage(e, "关闭失败")),
  });

  const diffs = useMemo(() => detail.data?.diffs ?? [], [detail.data]);
  const pendingFixes = useMemo(
    () => diffs.filter((d) => d.diff_class === "fix_amount" && d.status === "pending"),
    [diffs],
  );

  const handleSingleAction = async (d: ReconDiffOut, a: DiffActionDef) => {
    try {
      const resp = await act.mutateAsync({ diffId: d.diff_id, action: a.action });
      const { settlement_warnings } = resp.data;
      if (settlement_warnings.length > 0) {
        Modal.warning({
          title: "结算单需重新生成",
          content: `本月已生成的待确认结算单（${settlement_warnings.join("、")}）含此单旧金额，请到结算页重新生成后再确认。`,
        });
      }
    } catch {
      // act.onError 已经弹过 message，这里不用重复处理
    }
  };

  const adoptAll = () => {
    Modal.confirm({
      title: `全部采纳 ${pendingFixes.length} 条修数？`,
      width: 560,
      content: (
        <div style={{ maxHeight: 320, overflowY: "auto" }}>
          {pendingFixes.map((d) => {
            const comp = compensationLabel(d.detail);
            return (
              <div key={d.diff_id}>
                {d.guest_name}（{d.platform_order_id}）：{d.system_amount} → <b>{d.bill_amount}</b>
                {comp && (
                  <Tag color="purple" style={{ marginLeft: 6 }}>
                    {comp}
                  </Tag>
                )}
              </div>
            );
          })}
        </div>
      ),
      okText: "确认全部采纳",
      onOk: async () => {
        // 逐条串行 adopt（不用 Promise.all）；单条失败不再中断循环——用 try/catch 收集
        // 成功/失败两个桶，失败原因走既有的 extractErrorMessage 归一文案，循环结束后
        // 只弹一次汇总 Modal（成功数 + 失败明细 + 去重后的 settlement_warnings），
        // 避免第一条 400（如多房单守卫）就卡住整个批量操作、丢掉后面已收集的警告。
        const warnings = new Set<string>();
        const succeeded: ReconDiffOut[] = [];
        const failed: { diff: ReconDiffOut; message: string }[] = [];
        for (const d of pendingFixes) {
          try {
            const resp = await act.mutateAsync({ diffId: d.diff_id, action: "adopt" });
            succeeded.push(d);
            resp.data.settlement_warnings.forEach((w) => warnings.add(w));
          } catch (e) {
            failed.push({ diff: d, message: extractErrorMessage(e, "操作失败") });
          }
        }
        const warningList = Array.from(warnings);
        const modalFn = failed.length > 0 ? Modal.warning : Modal.info;
        modalFn({
          title: "全部采纳结果",
          width: 560,
          content: (
            <div style={{ maxHeight: 320, overflowY: "auto" }}>
              <p>
                成功 {succeeded.length} 条，失败 {failed.length} 条
              </p>
              {failed.length > 0 && (
                <div>
                  <Text type="danger">失败明细：</Text>
                  <ul style={{ paddingLeft: 20, margin: 0 }}>
                    {failed.map(({ diff, message }) => (
                      <li key={diff.diff_id}>
                        {diff.guest_name}（{diff.platform_order_id}）：{message}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {warningList.length > 0 && (
                <div style={{ marginTop: failed.length > 0 ? 8 : 0 }}>
                  <Text type="warning">
                    结算单需重新生成：本月已生成的待确认结算单（{warningList.join("、")}）含以上采纳单的旧金额，请到结算页重新生成后再确认。
                  </Text>
                </div>
              )}
            </div>
          ),
        });
      },
    });
  };

  const columns: ColumnsType<ReconDiffOut> = [
    {
      title: "分类",
      dataIndex: "diff_class",
      render: (c: ReconDiffOut["diff_class"]) => (
        <Tag color={DIFF_CLASS_META[c].color}>{DIFF_CLASS_META[c].label}</Tag>
      ),
    },
    { title: "客人", dataIndex: "guest_name", render: (v: string | null) => v ?? "—" },
    { title: "携程单号", dataIndex: "platform_order_id", render: (v: string | null) => v ?? "—" },
    {
      title: "系统单",
      dataIndex: "order_id",
      render: (v: string | null) => v ? (
        <Button type="link" style={{ padding: 0 }} onClick={() => setViewOrderId(v)}>{v}</Button>
      ) : "—",
    },
    {
      title: "系统 → 账单",
      key: "amounts",
      render: (_: unknown, d: ReconDiffOut) => {
        // appeal_settled：真正的金额故事在 detail 里（settled_amount/system_amount），
        // 主字段 bill_amount 在 appeal 分类天生是 null，别用它误导展示。
        if (d.diff_class === "appeal" && d.status === "appeal_settled") {
          const settled = typeof d.detail?.settled_amount === "string" ? d.detail.settled_amount : null;
          const sys = typeof d.detail?.system_amount === "string" ? d.detail.system_amount : d.system_amount;
          const shortfall = shortPaidAmount(d.detail);
          return (
            <span>
              <Text>{sys ?? "—"}</Text> {" → "} <Text strong>{settled ?? "—"}</Text>
              {shortfall && (
                <div>
                  <Text type="danger">少补 ¥{shortfall}</Text>
                </div>
              )}
            </span>
          );
        }
        const comp = d.diff_class === "fix_amount" ? compensationLabel(d.detail) : null;
        // 「钱为啥差」：诊断挂在 batch 层（AI 对整批账单一次性给的逐行解释），
        // 页面按 platform_order_id 去 batch.diagnosis.per_row 里查这一行的那句话；
        // 查不到（没算出来 / 非 fix_amount）就不渲染，不拿空态制造噪音。
        const whyDiff = amountExplanation(d);
        return (
          <span>
            <Text delete={d.diff_class === "fix_amount"}>{d.system_amount ?? "—"}</Text>
            {" → "}
            <Text strong>{d.bill_amount ?? "—"}</Text>
            {comp && (
              <div>
                <Tag color="purple">{comp}</Tag>
              </div>
            )}
            {whyDiff && (
              <div>
                <Text type="secondary" style={{ fontSize: 12 }}>{whyDiff}</Text>
              </div>
            )}
          </span>
        );
      },
    },
    {
      title: "状态",
      dataIndex: "status",
      render: (s: ReconDiffOut["status"]) => (
        <Tag color={DIFF_STATUS_META[s].color}>{DIFF_STATUS_META[s].label}</Tag>
      ),
    },
    {
      title: "操作",
      key: "actions",
      render: (_: unknown, d: ReconDiffOut) => {
        if (d.status !== "pending") return null;
        const dismissActions = (
          <Space>
            {ACTIONS_BY_CLASS[d.diff_class].map((a) => (
              <Popconfirm
                key={a.action}
                title={confirmTitle(d, a)}
                onConfirm={() => handleSingleAction(d, a)}
              >
                <Button
                  size="small"
                  danger={a.danger}
                  disabled={act.isPending}
                  loading={act.isPending && act.variables?.diffId === d.diff_id}
                >
                  {a.label}
                </Button>
              </Popconfirm>
            ))}
          </Space>
        );
        // manual_review 且 AI 给出候选：先展示候选理由 + 认领按钮（认领只链接单号，
        // 不改钱），既有的忽略动作仍保留在下面——认领判断错了人还能忽略这行。
        const cands = (d.detail?.ai_candidates as ClaimCandidate[] | undefined) ?? [];
        if (d.diff_class === "manual_review" && cands.length > 0) {
          const top = cands[0];
          const single = cands.length === 1 && top.confidence >= CLAIM_CONFIDENCE_SINGLE;
          return (
            <Space direction="vertical" size={2}>
              {cands.map((c) => (
                <div key={c.order_id}>
                <Text type="secondary" style={{ fontSize: 12 }}>{claimReason(c)}</Text>{" "}
                  <Button
                    size="small"
                    type="link"
                    disabled={claim.isPending}
                    loading={claim.isPending && claim.variables?.diffId === d.diff_id
                      && claim.variables?.orderId === c.order_id}
                    onClick={() => claim.mutate({ diffId: d.diff_id, orderId: c.order_id })}
                  >
                    {single ? "就是它" : `认领 ${c.order_id}`}
                  </Button>
                </div>
              ))}
              {dismissActions}
              <Button size="small" onClick={() => { setPickerDiff(d); setSelectedOrderId(null); }}>
                查找其他订单
              </Button>
            </Space>
          );
        }
        if (d.diff_class === "manual_review") {
          return (
            <Space direction="vertical" size={4}>
              <Button type="primary" size="small" onClick={() => { setPickerDiff(d); setSelectedOrderId(null); }}>
                查找并关联订单
              </Button>
              {dismissActions}
            </Space>
          );
        }
        return dismissActions;
      },
    },
  ];

  // 无权限（非 admin 直达 URL）：不渲染任何内容，等 useEffect 重定向；后端本身也会 403。
  if (user && !isAdmin) return null;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <PageHeader
        title="账单对账"
        subtitle="上传 OTA 月账单对账 · 宝禹↔系统日常对账在 财务→对账"
        extra={
          pendingFixes.length > 0 && (
            <Button type="primary" danger disabled={act.isPending} onClick={adoptAll}>
              全部采纳（{pendingFixes.length}）
            </Button>
          )
        }
      />
      {detail.data && detail.data.batch.status !== "rejected" && (
        <Card>
          <Space direction="vertical" size={8} style={{ width: "100%" }}>
            <Space wrap style={{ width: "100%", justifyContent: "space-between" }}>
              <Text type="secondary">
                本次上传：{detail.data.batch.filename ?? "历史账单"} · {formatUploadTime(detail.data.batch.created_at)}
              </Text>
              <Popconfirm
                title="关闭本次对账结果？"
                description="关闭后不再显示；原始记录和已完成操作仍会保留。"
                okText="确定关闭"
                cancelText="取消"
                onConfirm={() => archive.mutate(detail.data!.batch.batch_id)}
              >
                <Button size="small" loading={archive.isPending}>关闭本次结果</Button>
              </Popconfirm>
            </Space>
            <Typography.Title level={4} style={{ margin: 0 }}>
              {detail.data.batch.reviewed_at
                ? "本月已核对完成"
                : (detail.data.batch.summary?.pending_count ?? diffs.filter((d) => d.status === "pending").length) > 0
                  ? `还有 ${detail.data.batch.summary?.pending_count ?? diffs.filter((d) => d.status === "pending").length} 条差异需要处理`
                  : "差异已全部处理，可以确认本月已核对"}
            </Typography.Title>
            {!detail.data.batch.reviewed_at && (
              <Text>
                待处理金额影响：<Text strong type="danger">¥{detail.data.batch.summary?.pending_impact_total ?? "0.00"}</Text>
                {" · "}已处理 {detail.data.batch.summary?.resolved_actionable_count ?? diffs.filter((d) => d.status !== "pending").length}/
                {detail.data.batch.summary?.total_actionable_count ?? diffs.length} 条
              </Text>
            )}
            {!detail.data.batch.reviewed_at && (detail.data.batch.summary?.pending_count ?? diffs.filter((d) => d.status === "pending").length) === 0 && (
              <Button type="primary" size="large" loading={review.isPending} onClick={() => review.mutate(detail.data!.batch.batch_id)}>
                确认本月已核对
              </Button>
            )}
            {detail.data.batch.reviewed_at && (
              <Text type="secondary">确认时间：{new Date(detail.data.batch.reviewed_at).toLocaleString("zh-CN")}</Text>
            )}
          </Space>
        </Card>
      )}
      <Card>
        <Upload.Dragger
          accept=".xls,.xlsx"
          maxCount={1}
          showUploadList={false}
          customRequest={({ file }) => upload.mutate(file as File)}
          disabled={upload.isPending}
        >
          <p className="ant-upload-drag-icon"><InboxOutlined /></p>
          <p className="ant-upload-text">
            {upload.isPending ? "AI 解析对账中（最长约 2 分钟）…" : "点击或拖入携程账单（.xls / .xlsx）"}
          </p>
          <p className="ant-upload-hint">
            当前仅支持携程系账单（含去哪儿/同程/智行）；按离店月自动对账；
            同月重传会作废旧的待处理差异（申诉追踪保留）；对不上的行 AI 会帮你找单
          </p>
        </Upload.Dragger>
      </Card>
      {batches.isSuccess && batches.data.length === 0 && (
        <Alert type="success" showIcon message="当前没有打开的对账结果" description="上传新账单后，结果会显示在这里。" />
      )}
      {batches.data && batches.data.length > 1 && (
        <Space wrap>
          {batches.data.map((b) => (
            <Tag.CheckableTag
              key={b.batch_id}
              checked={b.batch_id === batchId}
              onChange={() => setActiveBatchId(b.batch_id)}
            >
              {b.status === "rejected"
                ? `已拒收 · ${b.filename ?? "账单"} · ${formatUploadTime(b.created_at)}`
                : `${b.filename ?? b.bill_month} · ${formatUploadTime(b.created_at)} · ${b.bill_month}（¥${b.summary_total}）`}
            </Tag.CheckableTag>
          ))}
        </Space>
      )}
      {detail.data?.batch.status === "rejected" && (
        <Alert type="error" message="该批次被校验闸拒收" description={detail.data.batch.error} />
      )}
      {detail.data && detail.data.batch.stats.out_of_window > 0 && (
        <Alert
          type="info"
          showIcon
          message={`提示：本批次有 ${detail.data.batch.stats.out_of_window} 行离店日期超出账单月 ±7 天窗口（跨月补结/退款行），已纳入本次对账`}
        />
      )}
      {detail.data?.batch.diagnosis?.summary && (
        <Alert
          type="info"
          showIcon
          message="当月账单诊断（AI）"
          description={detail.data.batch.diagnosis.summary}
        />
      )}
      <Card
        title={
          detail.data
            ? `${detail.data.batch.bill_month} 差异清单（账单合计 ¥${detail.data.batch.summary_total} · ${detail.data.batch.row_count} 行）`
            : "差异清单"
        }
      >
        <Table<ReconDiffOut>
          rowKey="diff_id"
          size="small"
          loading={detail.isLoading}
          dataSource={diffs}
          columns={columns}
          pagination={false}
          scroll={{ x: 900 }}
          locale={{ emptyText: "暂无差异——账单与系统完全一致，或还没上传账单" }}
        />
      </Card>
      <Modal
        title="查找并关联系统订单"
        open={!!pickerDiff}
        onCancel={() => { setPickerDiff(null); setSelectedOrderId(null); }}
        okText="确认关联"
        okButtonProps={{ disabled: !selectedOrder.data }}
        confirmLoading={claim.isPending}
        onOk={() => pickerDiff && selectedOrderId
          && claim.mutate({ diffId: pickerDiff.diff_id, orderId: selectedOrderId })}
      >
        <Space direction="vertical" size={12} style={{ width: "100%" }}>
          <Alert
            type="info"
            showIcon
            message="请选择可能对应的系统订单"
            description="搜索客人姓名、手机号或订单号，选中后先核对订单信息，再确认关联。"
          />
          <OrderQuickSearch onSelectOrder={setSelectedOrderId} style={{ width: "100%" }} />
          {selectedOrder.data && (
            <>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 12 }}>
                <Card size="small" title="账单数据">
                  <Descriptions size="small" column={1} colon={false}>
                    <Descriptions.Item label="客人">{pickerDiff?.guest_name ?? "—"}</Descriptions.Item>
                    <Descriptions.Item label="平台单号">{pickerDiff?.platform_order_id ?? "—"}</Descriptions.Item>
                    <Descriptions.Item label="账单金额">¥{pickerDiff?.bill_amount ?? "—"}</Descriptions.Item>
                  </Descriptions>
                </Card>
                <Card size="small" title="系统订单">
                  <Descriptions size="small" column={1} colon={false}>
                    <Descriptions.Item label="订单号">{selectedOrder.data.order_id}</Descriptions.Item>
                    <Descriptions.Item label="客人">{selectedOrder.data.guest_name}</Descriptions.Item>
                    <Descriptions.Item label="入住日期">
                      {selectedOrder.data.check_in_date} → {selectedOrder.data.check_out_date}
                    </Descriptions.Item>
                    <Descriptions.Item label="订单金额">¥{selectedOrder.data.actual_price ?? "—"}</Descriptions.Item>
                  </Descriptions>
                </Card>
              </div>
              {(pickerDiff?.guest_name !== selectedOrder.data.guest_name
                || (pickerDiff?.bill_amount != null && selectedOrder.data.actual_price != null
                  && Number(pickerDiff.bill_amount) !== Number(selectedOrder.data.actual_price))) && (
                <Alert
                  type="warning"
                  showIcon
                  message="账单与所选订单存在差异"
                  description="请重点核对客人和金额；确认关联后，系统会把平台单号写入该订单。"
                />
              )}
            </>
          )}
        </Space>
      </Modal>
      <Modal
        title="订单详情"
        open={!!viewOrderId}
        footer={null}
        onCancel={() => setViewOrderId(null)}
      >
        {viewedOrder.data && (
          <Descriptions bordered size="small" column={1}>
            <Descriptions.Item label="订单号">{viewedOrder.data.order_id}</Descriptions.Item>
            <Descriptions.Item label="客人">{viewedOrder.data.guest_name}</Descriptions.Item>
            <Descriptions.Item label="平台单号">{viewedOrder.data.platform_order_id ?? "—"}</Descriptions.Item>
            <Descriptions.Item label="入住日期">
              {viewedOrder.data.check_in_date} → {viewedOrder.data.check_out_date}
            </Descriptions.Item>
            <Descriptions.Item label="订单金额">¥{viewedOrder.data.actual_price ?? "—"}</Descriptions.Item>
          </Descriptions>
        )}
      </Modal>
    </div>
  );
}
