"use client";

import React, { useState } from "react";
import { ordersApi, exportApi, batchApi } from "@/lib/api";
import { filtersFromSearchParams } from "@/lib/orders-url-filters";
import { downloadBlob, todayCNString } from "@/lib/utils";
import {
  Table,
  Button,
  Space,
  Card,
  message,
  Checkbox,
  Dropdown,
  Tag,
  Modal,
} from "antd";
import { extractErrorMessage } from "@/lib/api-errors";
import {
  PlusOutlined,
  DownloadOutlined,
  CheckCircleOutlined,
  MoreOutlined,
  EyeOutlined,
  DollarOutlined,
  FilterOutlined,
  CloseOutlined,
} from "@ant-design/icons";
import type { ColumnsType } from "antd/es/table";
import type { StayGroup, StaySegment } from "@/lib/types";
import { useOrders, type OrderFilters } from "@/hooks/useOrders";
import { useRouter, useSearchParams } from "next/navigation";
import {
  OrderDetailModal,
  PaymentModal,
  OrderFilters as OrderFiltersBar,
} from "@/components/orders";
import { useIsMobile } from "@/lib/responsive";
import { PageHeader } from "@/components/ui/PageHeader";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { EmptyState } from "@/components/ui/EmptyState";
import { tokens } from "@/lib/design-tokens";
import { formatExpectedRevenue } from "@/lib/order-display";
import { segmentCountLabel } from "@/lib/stay-group-display";
import { resolveBatchIds } from "@/lib/batch-selection";
import { CHANNEL_LABELS } from "@/lib/channels";
import { FreeRoomBadge } from "@/components/ui/FreeRoomBadge";
import {
  hasStaySettlementLabels,
  StayGroupSettlementLabels,
} from "@/components/orders/StayGroupSettlementLabels";

// 段行的展示助手。**不含任何口径**：合计/晚数/状态/房间序列全由后端 group_view 算好，
// 这里只是从段里挑出「代表那张单」用来显示客人名、以及数一数有几段。
// 行 key 用 anchor_order_id —— stay_group_id 对无组单是 null，当 key 会撞。
const segCount = (row: StayGroup) => row.segments?.length ?? 0;
const anchorSeg = (row: StayGroup): StaySegment | undefined =>
  row.segments?.find((s) => s.order_id === row.anchor_order_id) ?? row.segments?.[0];
// 任一活段的价格还没从 OTA 回填（占位价 0）→ 整段房费必然少算，显示「同步中…」而不是那个假数。
// 这不是重算口径，只是一个布尔或：金额仍然只用后端的 total_amount。
const segPricePending = (row: StayGroup) =>
  (row.segments ?? []).some((s) => s.order_status !== "cancelled" && s.price_pending);

export default function OrdersPage() {
  const isMobile = useIsMobile();
  const router = useRouter();
  const searchParams = useSearchParams();
  const [selectedOrder, setSelectedOrder] = useState<any>(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const [paymentOpen, setPaymentOpen] = useState(false);
  // 支持 URL 参数预填筛选(从 dashboard KPI 卡片、新订单提示 toast 或外部链接跳转过来时)。
  const [filters, setFilters] = useState<OrderFilters>(() =>
    filtersFromSearchParams(new URLSearchParams(searchParams.toString())),
  );
  // 从概览卡片跳来时携带的可读筛选标签(如「今日待入住」),用于顶部提示条。
  // 存 state 而非每次读 URL——清除后要能立即消失。
  const [filterLabel, setFilterLabel] = useState<string | null>(
    () => searchParams.get("filter_label") || null
  );
  const hasActiveFilter = Boolean(
    filters.status ||
      filters.channel ||
      filters.keyword ||
      filters.check_in_from ||
      filters.check_in_to ||
      filters.check_out_from ||
      filters.check_out_to
  );

  const clearFilters = () => {
    setFilters({});
    setFilterLabel(null);
    setPage(1);
    // 筛选一变，勾选残留就是跨页地雷（key 不在新结果里 → 批量静默漏单），一并清空
    setSelectedRowKeys([]);
    // 清掉 URL 上的预填参数,刷新/分享不再带筛选
    router.replace("/orders");
  };
  const [page, setPage] = useState(1);

  // URL 变了就同步筛选态。上面的惰性初始化只在挂载时跑一次——人已经在本页时，
  // 同路由 router.push('/orders?keyword=X') 不会重挂组件，没有这个 effect 就
  // 「点了等于没点」（新订单 toast 的第二次点击、KPI 卡片在本页内的跳转都靠它）。
  const spString = searchParams.toString();
  React.useEffect(() => {
    if (!spString) return; // 无参数(如点「清空筛选」后)不覆盖用户当前的手动筛选
    const sp = new URLSearchParams(spString);
    setFilters(filtersFromSearchParams(sp));
    // 必须重置页码：换了筛选还停在第 2 页 → 空表格 → 又是「点了等于没点」。
    setPage(1);
    // 必须同步横幅标签：只换 filters 不换 label，横幅会继续宣称「今日待入住」，
    // 底下却只列着一张无关的单。
    setFilterLabel(sp.get("filter_label") || null);
    // 筛选变了，勾选残留 key 不在新结果里 → 批量会静默漏单，清空
    setSelectedRowKeys([]);
  }, [spString]);

  const [selectedRowKeys, setSelectedRowKeys] = useState<React.Key[]>([]);
  const [exporting, setExporting] = useState(false);
  const [batchLoading, setBatchLoading] = useState(false);

  const {
    ordersQuery,
    transitionMutation,
    cancelMutation,
    usePaymentsQuery,
    createPaymentMutation,
  } = useOrders(filters, page);

  const { data, isLoading, refetch } = ordersQuery;
  // 收款弹窗从行内直达时详情抽屉没开，payments 查询也要启用——否则「已收/待收」是空的
  const { data: payments } = usePaymentsQuery(selectedOrder?.order_id, detailOpen || paymentOpen);
  // 收款弹窗的来路：从详情抽屉进的，关掉回详情；从列表行直达的，关掉就回列表
  const [paymentFrom, setPaymentFrom] = useState<"detail" | "row">("detail");

  const handleExport = async () => {
    setExporting(true);
    try {
      const res = await exportApi.orders(filters);
      downloadBlob(res.data, `orders_${todayCNString()}.xlsx`);
      message.success("导出成功");
    } catch {
      message.error("导出失败");
    } finally {
      setExporting(false);
    }
  };

  const handleBatchTransition = async (targetStatus: string) => {
    // 立即"冻结"本次提交的 IDs，并清空选中态：
    // - 防止 loading 中用户继续勾选导致下次提交混合本批未完成 + 新选的订单
    // - 防止 selectedRowKeys 引用被覆盖后 catch 分支拿不到原批次做提示
    // 列表按段之后，勾中的 key 是段（anchor_order_id）。一段 = 前台眼里的一个订单，
    // 所以批量操作要落到该段的**全部活单**上，不能只动锚单——只动锚单会让续住组的其余段
    // 静默留在原状态。已取消段不碰。后端逐单返回失败原因，下面的 Modal 会逐条列出。
    // key 不在当前页（勾选后翻页/换筛选的残留）→ 跳过并明示，不再 fallback 只发锚单。
    const rows: StayGroup[] = (data?.items ?? []) as StayGroup[];
    const { ids: batchIds, staleKeys } = resolveBatchIds(selectedRowKeys as string[], rows);
    if (staleKeys.length > 0) {
      message.warning(`有 ${staleKeys.length} 个选中项因翻页失效未处理`);
    }
    if (batchIds.length === 0) {
      setSelectedRowKeys([]);
      return;
    }
    setSelectedRowKeys([]);
    setBatchLoading(true);
    try {
      const res = await batchApi.transition(batchIds, targetStatus);
      const { succeeded, failed } = res.data;
      if (succeeded.length > 0) message.success(`${succeeded.length} 笔订单操作成功`);
      if (failed.length > 0) {
        // 后端逐单返回失败原因，列出来——只报数字运营要挨个猜是哪单、为什么
        Modal.warning({
          title: `${failed.length} 笔订单未能操作`,
          width: 520,
          content: (
            <ul style={{ paddingLeft: 18, margin: "8px 0", maxHeight: 280, overflowY: "auto" }}>
              {(failed as { order_id: string; reason: string }[]).map((f) => (
                <li key={f.order_id} style={{ marginBottom: 4, fontSize: 13 }}>
                  <b>{f.order_id}</b>：{f.reason}
                </li>
              ))}
            </ul>
          ),
          okText: "知道了",
        });
      }
      refetch();
    } catch (e) {
      message.error(extractErrorMessage(e, "批量操作失败"));
    } finally {
      setBatchLoading(false);
    }
  };

  // 点段行开详情：喂锚单那一段（完整订单对象），详情页自己会去问整段并按整段渲染。
  const openDetail = (row: StayGroup) => {
    setSelectedOrder(anchorSeg(row) ?? { order_id: row.anchor_order_id });
    setDetailOpen(true);
  };

  const columns: ColumnsType<any> = [
    {
      title: "订单号",
      key: "order_id",
      width: 160,
      render: (_v, r: StayGroup) => (
        <span
          style={{
            fontFamily: tokens.font.mono,
            fontSize: 12,
            color: tokens.color.text.secondary,
          }}
        >
          {r.anchor_order_id}
          {segCount(r) > 1 && (
            <Tag
              bordered={false}
              style={{
                marginLeft: 6,
                background: tokens.color.brand.primarySoft,
                color: tokens.color.brand.primary,
                fontSize: 11,
              }}
            >
              {/* 段数按活段计（与 nights/金额同口径）：取消段注明，别标 3 段金额只有 2 段的 */}
              续住 {segmentCountLabel(r.segments)}
            </Tag>
          )}
        </span>
      ),
    },
    {
      title: "客人",
      key: "guest",
      render: (_, row: StayGroup) => {
        const r = anchorSeg(row);
        return (
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span
            style={{
              width: 28,
              height: 28,
              borderRadius: "50%",
              background: tokens.color.brand.primarySoft,
              color: tokens.color.brand.primary,
              display: "inline-flex",
              alignItems: "center",
              justifyContent: "center",
              fontSize: 12,
              fontWeight: 600,
              flex: "0 0 28px",
            }}
          >
            {r?.guest_name?.charAt(0) ?? "?"}
          </span>
          <div style={{ minWidth: 0 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap", fontSize: 13, fontWeight: 500, lineHeight: 1.3 }}>
              <span>{r?.guest_name}</span>
              {!hasStaySettlementLabels(row) ? (
                <FreeRoomBadge kind={row.free_room_kind} />
              ) : null}
              <StayGroupSettlementLabels group={row} />
            </div>
            <div style={{ fontSize: 11, color: tokens.color.text.tertiary, marginTop: 1 }}>
              {r?.guest_phone}
            </div>
          </div>
        </div>
        );
      },
    },
    {
      title: "房间",
      key: "room",
      width: 130,
      render: (_v, r: StayGroup) => {
        // 段口径：按夜去重的房间序列，后端从 order_rooms 算好。跨房续住会是 1605 → 1606。
        const ids = r.rooms ?? [];
        if (ids.length === 0) {
          return <span style={{ color: tokens.color.text.tertiary }}>待排房</span>;
        }
        if (ids.length === 1) {
          return <span style={{ fontWeight: 500 }}>{ids[0]}</span>;
        }
        const head = ids.slice(0, 2).join(" → ");
        const extra = ids.length - 2;
        return (
          <span style={{ fontWeight: 500 }}>
            {head}
            {extra > 0 && (
              <span style={{ color: tokens.color.text.tertiary, marginLeft: 4 }}>(+{extra})</span>
            )}
          </span>
        );
      },
    },
    {
      title: "入住",
      key: "check_in_date",
      width: 110,
      render: (_v, r: StayGroup) => (
        <div style={{ fontSize: 12, lineHeight: 1.4 }}>
          <div>{r.check_in_date}</div>
          <div style={{ color: tokens.color.text.tertiary }}>{r.nights} 晚</div>
        </div>
      ),
    },
    {
      title: "退房",
      key: "check_out_date",
      width: 110,
      render: (_v, r: StayGroup) => <div style={{ fontSize: 12 }}>{r.check_out_date}</div>,
    },
    {
      title: "渠道",
      key: "channel",
      width: 80,
      render: (_v, r: StayGroup) => (
        // 多于一个 = 这段跨渠道（携程转去哪儿是真实形态）
        <span style={{ display: "inline-flex", flexWrap: "wrap", gap: 4 }}>
          {(r.channels ?? []).map((c) => (
            <Tag
              key={c}
              bordered={false}
              style={{ margin: 0, background: tokens.color.bg.subtle, color: tokens.color.text.secondary }}
            >
              {CHANNEL_LABELS[c] || c}
            </Tag>
          ))}
        </span>
      ),
    },
    {
      title: "金额",
      key: "amount",
      width: 110,
      align: "right",
      className: "col-amount",
      render: (_v, r: StayGroup) => {
        // 整段房费：后端算好（已排除取消段）。前端不累加。
        return segPricePending(r) ? (
          <span style={{ color: tokens.color.text.tertiary, fontWeight: 600, fontSize: 13 }}>
            同步中…
          </span>
        ) : Number(r.total_amount ?? 0) > 0 ? (
          <span className="tabular" style={{ fontWeight: 600, color: tokens.color.text.primary }}>
            ¥{Number(r.total_amount).toLocaleString()}
          </span>
        ) : (
          <span style={{ color: tokens.color.text.tertiary }}>—</span>
        );
      },
    },
    {
      title: "净房费",
      key: "expected_revenue",
      width: 110,
      align: "right",
      render: (_v, r: StayGroup) => {
        // 净房费是按单口径（OTA 到手价），组级没有这个数——前端累加就是在 TS 里重实现口径，
        // 且必然漏掉「取消段不计入」。续住组显示「—」，要看得进详情页展开分段明细。
        const s = segCount(r) === 1 ? formatExpectedRevenue(anchorSeg(r)?.expected_revenue) : null;
        return s ? (
          <span className="tabular" style={{ fontWeight: 600, color: tokens.color.brand.primary }}>{s}</span>
        ) : (
          <span style={{ color: tokens.color.text.tertiary }}>—</span>
        );
      },
    },
    {
      title: "状态",
      key: "status",
      width: 120,
      render: (_v, r: StayGroup) => <StatusBadge status={r.group_status} size="sm" />,
    },
    {
      title: "",
      key: "action",
      width: 48,
      render: (_, record: StayGroup) => (
        <Dropdown
          menu={{
            items: [
              {
                key: "view",
                icon: <EyeOutlined />,
                label: "查看详情",
                onClick: () => openDetail(record),
              },
              // 高频动作直达：不用先开详情再找「记收款」（定金+尾款场景配合
              // 弹窗内「保存,再记一笔」一口气录完）。已取消单不收款。
              // 续住组不给这个快捷项：每段房费/佣金各自独立，钱记错段直接坑 OTA 对账
              //（王总 2026-07-17 拍板）→ 走详情页「分段明细」点那一段。
              ...(record.group_status !== "cancelled" && segCount(record) === 1
                ? [
                    {
                      key: "payment",
                      icon: <DollarOutlined />,
                      label: "记收款",
                      onClick: () => {
                        setSelectedOrder(anchorSeg(record));
                        setPaymentFrom("row");
                        setPaymentOpen(true);
                      },
                    },
                  ]
                : []),
            ],
          }}
          trigger={["click"]}
          placement="bottomRight"
        >
          <Button
            type="text"
            icon={<MoreOutlined />}
            onClick={(e) => e.stopPropagation()}
            size="small"
          />
        </Dropdown>
      ),
    },
  ];

  const orders = data?.items ?? [];
  const total = data?.total ?? 0;
  const hasData = orders.length > 0;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <PageHeader
        title="订单管理"
        subtitle={`共 ${total} 笔订单`}
        extra={
          <Space>
            <Button icon={<DownloadOutlined />} loading={exporting} onClick={handleExport}>
              导出
            </Button>
            <Button
              type="primary"
              icon={<PlusOutlined />}
              onClick={() => router.push("/orders/new")}
            >
              新建订单
            </Button>
          </Space>
        }
      />

      <OrderFiltersBar
        filters={filters}
        onFiltersChange={(f) => {
          // 用户手动动了筛选,预设标签(今日待入住等)不再准确,撤掉
          setFilters(f);
          setFilterLabel(null);
          // 筛选变更 → 勾选残留失效，清空（防批量静默漏单）
          setSelectedRowKeys([]);
        }}
        onRefresh={() => refetch()}
      />

      {hasActiveFilter && (
        <div
          style={{
            background: tokens.color.brand.primarySoft,
            border: `1px solid ${tokens.color.brand.primary}22`,
            borderRadius: tokens.radius.md,
            padding: "10px 14px",
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 12,
            flexWrap: "wrap",
          }}
        >
          <span
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 8,
              fontSize: 13,
              color: tokens.color.brand.primary,
              fontWeight: 500,
            }}
          >
            <FilterOutlined />
            {filterLabel ? (
              <>
                正在查看：<b>{filterLabel}</b>
                <span style={{ opacity: 0.7, fontWeight: 400 }}>· 共 {total} 笔</span>
              </>
            ) : (
              <>已按条件筛选 · 共 {total} 笔</>
            )}
          </span>
          <Button size="small" type="text" icon={<CloseOutlined />} onClick={clearFilters}>
            清除筛选
          </Button>
        </div>
      )}

      {selectedRowKeys.length > 0 && (
        <div
          style={{
            background: tokens.color.brand.primarySoft,
            border: `1px solid ${tokens.color.brand.primary}22`,
            borderRadius: tokens.radius.md,
            padding: "10px 14px",
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 12,
            flexWrap: "wrap",
          }}
        >
          <span style={{ fontSize: 13, color: tokens.color.brand.primary, fontWeight: 500 }}>
            已选 {selectedRowKeys.length} 条订单
          </span>
          <Space>
            <Button
              size="small"
              icon={<CheckCircleOutlined />}
              loading={batchLoading}
              onClick={() => handleBatchTransition("paid_pending_room")}
            >
              批量确认
            </Button>
            <Button size="small" onClick={() => setSelectedRowKeys([])}>
              取消选择
            </Button>
          </Space>
        </div>
      )}

      {isMobile ? (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {orders.map((order: StayGroup) => {
            const isSelected = selectedRowKeys.includes(order.anchor_order_id);
            const seg = anchorSeg(order);
            return (
              <div
                key={order.anchor_order_id}
                className="card-hoverable"
                style={{
                  background: tokens.color.bg.container,
                  border: `1px solid ${isSelected ? tokens.color.brand.primary : tokens.color.bg.border}`,
                  borderRadius: tokens.radius.lg,
                  padding: 14,
                  cursor: "pointer",
                  display: "flex",
                  alignItems: "flex-start",
                  gap: 12,
                }}
                onClick={() => openDetail(order)}
              >
                <Checkbox
                  checked={isSelected}
                  disabled={batchLoading}
                  onClick={(e) => e.stopPropagation()}
                  onChange={(e) => {
                    if (e.target.checked) {
                      setSelectedRowKeys([...selectedRowKeys, order.anchor_order_id]);
                    } else {
                      setSelectedRowKeys(selectedRowKeys.filter((k) => k !== order.anchor_order_id));
                    }
                  }}
                  style={{ marginTop: 2, flexShrink: 0 }}
                />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div
                    style={{
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "space-between",
                      marginBottom: 6,
                      gap: 8,
                    }}
                  >
                    <div style={{ fontWeight: 600, fontSize: 14 }}>
                      {seg?.guest_name}
                      <span style={{ display: "inline-flex", marginLeft: 6, verticalAlign: "middle" }}>
                        {!hasStaySettlementLabels(order) ? (
                          <FreeRoomBadge kind={order.free_room_kind} />
                        ) : null}
                      </span>
                      <StayGroupSettlementLabels group={order} />
                      {segCount(order) > 1 && (
                        <Tag
                          bordered={false}
                          style={{
                            marginLeft: 6,
                            background: tokens.color.brand.primarySoft,
                            color: tokens.color.brand.primary,
                            fontSize: 11,
                          }}
                        >
                          续住 {segmentCountLabel(order.segments)}
                        </Tag>
                      )}
                    </div>
                    <StatusBadge status={order.group_status} size="sm" />
                  </div>
                  <div
                    style={{
                      display: "flex",
                      alignItems: "center",
                      flexWrap: "wrap",
                      gap: 8,
                      marginBottom: 6,
                      fontSize: 12,
                      color: tokens.color.text.secondary,
                    }}
                  >
                    <span>{(order.channels ?? []).map((c) => CHANNEL_LABELS[c] || c).join(" / ")}</span>
                    <span>·</span>
                    <span>
                      {(() => {
                        // 段口径：按夜去重的房间序列（跨房续住是 1605 → 1606）
                        const ids = order.rooms ?? [];
                        if (ids.length === 0) return "待排房";
                        if (ids.length === 1) return ids[0];
                        return `${ids.slice(0, 2).join(" → ")}${ids.length > 2 ? ` (+${ids.length - 2})` : ""}`;
                      })()}
                    </span>
                    <span>·</span>
                    <span>
                      {order.check_in_date} → {order.check_out_date}
                    </span>
                    <span>·</span>
                    <span>{order.nights}晚</span>
                  </div>
                  {segPricePending(order) ? (
                    <div style={{ color: tokens.color.text.tertiary, fontSize: 13, fontWeight: 600 }}>
                      价格同步中…
                    </div>
                  ) : Number(order.total_amount ?? 0) > 0 ? (
                    <div
                      className="tabular"
                      style={{ color: tokens.color.text.primary, fontSize: 15, fontWeight: 600 }}
                    >
                      ¥{Number(order.total_amount).toLocaleString()}
                    </div>
                  ) : null}
                  {/* 净房费按单口径，组级没有 → 续住组不显示这行（详情页分段明细里看） */}
                  {segCount(order) === 1 && formatExpectedRevenue(seg?.expected_revenue) && (
                    <div
                      className="tabular"
                      style={{ color: tokens.color.brand.primary, fontSize: 13, fontWeight: 600, marginTop: 2 }}
                    >
                      净房费 {formatExpectedRevenue(seg?.expected_revenue)}
                    </div>
                  )}
                </div>
              </div>
            );
          })}
          {!hasData && !isLoading && (
            <Card
              bordered={false}
              style={{
                borderRadius: tokens.radius.lg,
                border: `1px solid ${tokens.color.bg.border}`,
              }}
              styles={{ body: { padding: 0 } }}
            >
              <EmptyState
                title="还没有订单"
                description="从右上角「新建订单」开始创建第一笔订单。"
                actionLabel="新建订单"
                onAction={() => router.push("/orders/new")}
              />
            </Card>
          )}
        </div>
      ) : (
        <div
          style={{
            background: tokens.color.bg.container,
            border: `1px solid ${tokens.color.bg.border}`,
            borderRadius: tokens.radius.lg,
            overflow: "hidden",
          }}
        >
          <Table
            columns={columns}
            dataSource={orders}
            rowKey="anchor_order_id"
            loading={isLoading}
            scroll={{ x: 960 }}
            rowSelection={{
              selectedRowKeys,
              onChange: (keys) => setSelectedRowKeys(keys),
              columnWidth: 44,
              // 批量提交中禁用所有 checkbox，防止用户勾选混入下一批次
              getCheckboxProps: () => ({ disabled: batchLoading }),
            }}
            onRow={(record) => ({
              onClick: () => openDetail(record),
              style: { cursor: "pointer" },
            })}
            locale={{
              emptyText: (
                <EmptyState
                  title="还没有订单"
                  description="从右上角「新建订单」开始创建第一笔订单。"
                  actionLabel="新建订单"
                  onAction={() => router.push("/orders/new")}
                />
              ),
            }}
            pagination={{
              current: page,
              total,
              pageSize: 20,
              // 翻页清空勾选：跨页勾选的 key 不在新页 rows 里，批量会把它们当失效项跳过
              //（简单可靠优先于跨页记忆——真要跨页批量，后端应有专门端点）
              onChange: (p) => {
                setPage(p);
                setSelectedRowKeys([]);
              },
              showSizeChanger: false,
              showTotal: (t) => `共 ${t} 笔订单`,
              style: { padding: "12px 20px", marginTop: 0 },
            }}
            size="middle"
          />
        </div>
      )}

      <OrderDetailModal
        open={detailOpen}
        order={selectedOrder}
        payments={Array.isArray(payments) ? payments : []}
        onClose={() => setDetailOpen(false)}
        onOpenPayment={() => {
          setDetailOpen(false);
          setPaymentFrom("detail");
          setPaymentOpen(true);
        }}
        transitionMutation={transitionMutation}
        cancelMutation={cancelMutation}
      />

      <PaymentModal
        open={paymentOpen}
        order={selectedOrder}
        payments={Array.isArray(payments) ? payments : []}
        onClose={() => {
          setPaymentOpen(false);
          if (paymentFrom === "detail") setDetailOpen(true);
        }}
        onFinish={(values) =>
          // 成功后关闭收款弹窗并按来路返回,避免弹窗滞留导致重复提交同一笔 (#50)
          createPaymentMutation.mutate(values, {
            onSuccess: () => {
              setPaymentOpen(false);
              if (paymentFrom === "detail") setDetailOpen(true);
            },
          })
        }
        onFinishAndNext={(values, resetForNext) =>
          // 保存后弹窗保持打开,清空金额继续录下一笔(定金+尾款场景)。
          // 「已收/待收」随 payments 查询失效自动刷新。
          createPaymentMutation.mutate(values, { onSuccess: resetForNext })
        }
        loading={createPaymentMutation.isPending}
      />
    </div>
  );
}
