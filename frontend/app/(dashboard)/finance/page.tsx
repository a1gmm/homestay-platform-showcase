"use client";

import React, { useState, useEffect, useMemo } from "react";
import { extractErrorMessage } from "@/lib/api-errors";
import { useQuery, useMutation, useQueryClient, keepPreviousData } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { financeApi, dashboardApi, roomsApi, ownersApi } from "@/lib/api";
import dayjs, { Dayjs } from "dayjs";
import {
  Card, Row, Col, Statistic, Typography, Space, Tag, Table, Tabs,
  Select, Button, Modal, Form, Input, DatePicker, message, Skeleton,
  Upload, Alert, List, Radio, Popconfirm,
} from "antd";
import { MobileInputNumber } from "@/components/ui/MobileInputNumber";
import type { UploadProps } from "antd";
import {
  DollarOutlined, RiseOutlined, BarChartOutlined,
  PlusOutlined, FallOutlined, DownloadOutlined, UploadOutlined,
  FileExcelOutlined, DeleteOutlined,
} from "@ant-design/icons";
import { exportApi } from "@/lib/api";
import { downloadBlob } from "@/lib/utils";
import type { ColumnsType } from "antd/es/table";
import { useIsMobile } from "@/lib/responsive";
import { useAuthStore } from "@/lib/auth";
import { navForRole } from "@/lib/nav-config";
import { getChannelLabel } from "@/lib/channels";
import { PageHeader } from "@/components/ui/PageHeader";
import { StatCard } from "@/components/ui/StatCard";
import { tokens } from "@/lib/design-tokens";
import ServiceFeeConfigCard from "@/components/finance/ServiceFeeConfigCard";
import { buildExpenseRows } from "@/lib/expense-grouping";

const { Title, Text } = Typography;

// 类别中文 label —— 必须与后端 EXPENSE_CATEGORY_LABELS 全量对齐（backend/app/models/expense.py）。
// 缺项会导致 daily_supplies / laundry 等新类别在页面显示英文原文（曾经的“页面乱”症状之一）。
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

// 已弃用的老类别：仍保留在 EXPENSE_CATEGORIES 里用于展示历史账，
// 但不再出现在「添加支出」下拉，避免前台新录时误选。
// v2#7：冷水/热水合并进"水费"停用；采购费(supplies)已启用移出本集。
const DEPRECATED_CATEGORIES = new Set(["utilities", "cold_water", "hot_water"]);

const CATEGORY_COLORS: Record<string, string> = {
  cleaning: "cyan",
  maintenance: "orange",
  utilities: "blue",
  supplies: "green",
  platform_fee: "purple",
  tax: "red",
  other: "default",
  public_utilities: "geekblue",
  water: "blue",
  cold_water: "blue",
  broadband: "magenta",
  daily_supplies: "green",
  laundry: "cyan",
  hot_water: "volcano",
  gas: "gold",
  property_fee: "purple",
  electricity: "blue",
  kitchen_cleaning: "lime",
  property_guidance_fee: "default",
  new_linen_prewash: "geekblue",
};

function MetricCard({
  title, value, prefix, suffix, icon, color, highlight,
}: {
  title: string;
  value: number | string;
  prefix?: string;
  suffix?: string;
  icon: React.ReactNode;
  color: string;
  highlight?: boolean;
}) {
  return (
    <Card
      bordered={false}
      style={{
        borderRadius: 12,
        boxShadow: highlight
          ? `0 2px 12px ${color}33`
          : "0 2px 8px rgba(0,0,0,0.06)",
        border: highlight ? `1px solid ${color}33` : "none",
        height: "100%",
      }}
    >
      <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between" }}>
        <div>
          <Text type="secondary" style={{ fontSize: 12 }}>{title}</Text>
          <Statistic
            value={value}
            prefix={prefix}
            suffix={suffix}
            valueStyle={{ fontSize: 24, fontWeight: 700, color: highlight ? color : "#1a1a2e" }}
            style={{ marginTop: 4 }}
          />
        </div>
        <div style={{
          width: 44, height: 44, borderRadius: 10,
          background: `${color}1a`, display: "flex",
          alignItems: "center", justifyContent: "center",
          fontSize: 20, color,
        }}>
          {icon}
        </div>
      </div>
    </Card>
  );
}

export default function FinancePage() {
  const isMobile = useIsMobile();
  const router = useRouter();
  const { user } = useAuthStore();
  // 纵深防御：财务页仅「菜单可达 finance」的角色能开（与侧边栏同源 navForRole）。
  // 前台(operator)已无「财务管理」菜单，此处再挡 URL 直达/书签，防止绕过菜单看收支/分成。
  const canAccessFinance = !!user && navForRole(user.role).includes("finance");
  useEffect(() => {
    if (user && !canAccessFinance) router.replace("/dashboard");
  }, [user, canAccessFinance, router]);
  const qc = useQueryClient();
  // 全局日期区间：驱动月度汇总卡片 / 支出明细 / 导出 / 按房号分账。默认本月整月。
  const [range, setRange] = useState<[Dayjs, Dayjs]>([
    dayjs().startOf("month"),
    dayjs().endOf("month"),
  ]);
  const startDate = range[0].format("YYYY-MM-DD");
  const endDate = range[1].format("YYYY-MM-DD");
  const [activeTab, setActiveTab] = useState<string>("summary");
  const [addExpenseOpen, setAddExpenseOpen] = useState(false);

  // 日期范围选择器的快捷预设（本月/上月/近3月/近6月/今年）
  const rangePresets = [
    { label: "本月", value: [dayjs().startOf("month"), dayjs().endOf("month")] as [Dayjs, Dayjs] },
    { label: "上月", value: [dayjs().subtract(1, "month").startOf("month"), dayjs().subtract(1, "month").endOf("month")] as [Dayjs, Dayjs] },
    { label: "近 3 个月", value: [dayjs().subtract(2, "month").startOf("month"), dayjs().endOf("month")] as [Dayjs, Dayjs] },
    { label: "近 6 个月", value: [dayjs().subtract(5, "month").startOf("month"), dayjs().endOf("month")] as [Dayjs, Dayjs] },
    { label: "今年", value: [dayjs().startOf("year"), dayjs().endOf("year")] as [Dayjs, Dayjs] },
  ];

  // 初始化 activeTab 从 URL(支持 ?tab=by-room 深链)
  useEffect(() => {
    if (typeof window === "undefined") return;
    const t = new URLSearchParams(window.location.search).get("tab");
    if (t) setActiveTab(t);
  }, []);

  const handleTabChange = (key: string) => {
    setActiveTab(key);
    if (typeof window !== "undefined") {
      const params = new URLSearchParams(window.location.search);
      params.set("tab", key);
      window.history.replaceState(null, "", `${window.location.pathname}?${params.toString()}`);
    }
  };
  const [exportLoading, setExportLoading] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [importResult, setImportResult] = useState<{
    succeeded: any[];
    failed: { row: number; errors: string; data: any }[];
    total_rows: number;
    imported_count: number;
  } | null>(null);
  const [importing, setImporting] = useState(false);
  const [form] = Form.useForm();

  const handleDownloadTemplate = async () => {
    try {
      const res = await financeApi.expenses.importTemplate();
      downloadBlob(res.data, "expense-template.xlsx");
    } catch {
      message.error("模板下载失败");
    }
  };

  const handleImportFile: UploadProps["customRequest"] = async (options) => {
    const { file, onSuccess, onError } = options;
    setImporting(true);
    try {
      const res = await financeApi.expenses.importUpload(file as File);
      setImportResult(res.data);
      qc.invalidateQueries({ queryKey: ["expenses"] });
      qc.invalidateQueries({ queryKey: ["finance", "by-room"] });
      qc.invalidateQueries({ queryKey: ["dashboard", "monthly"] });
      onSuccess?.(res.data);
      if (res.data.imported_count > 0) {
        message.success(`成功导入 ${res.data.imported_count} 条`);
      }
      if (res.data.failed?.length > 0) {
        message.warning(`${res.data.failed.length} 条失败，请检查`);
      }
    } catch (e: any) {
      onError?.(e);
      message.error(extractErrorMessage(e, "导入失败"));
    } finally {
      setImporting(false);
    }
  };

  const handleExportFinance = async () => {
    setExportLoading(true);
    try {
      const res = await exportApi.finance({ start_date: startDate, end_date: endDate });
      downloadBlob(
        res.data,
        `finance_${range[0].format("YYYYMMDD")}-${range[1].format("YYYYMMDD")}.xlsx`,
      );
      message.success("导出成功");
    } catch {
      message.error("导出失败");
    } finally {
      setExportLoading(false);
    }
  };

  const { data: monthly, isLoading: monthlyLoading } = useQuery({
    queryKey: ["dashboard", "period", startDate, endDate],
    queryFn: () => dashboardApi.periodSummary(startDate, endDate).then((r) => r.data),
    placeholderData: keepPreviousData,
  });

  const { data: expenses, isLoading: expLoading } = useQuery({
    queryKey: ["expenses", startDate, endDate],
    queryFn: () =>
      financeApi.expenses.list({ start_date: startDate, end_date: endDate }).then((r) => r.data),
    placeholderData: keepPreviousData,
  });

  // —— 支出明细：筛选 + 按订单分组 —— //
  const [expCategory, setExpCategory] = useState<string | undefined>(undefined);
  const [expRoom, setExpRoom] = useState<string | undefined>(undefined);
  const [expSearch, setExpSearch] = useState("");

  // 类别 / 房号筛选下拉的可选项：只列出当前区间实际出现过的值（避免下拉塞满空类别）。
  const { categoryFilterOptions, roomFilterOptions } = useMemo(() => {
    const raw = Array.isArray(expenses) ? expenses : [];
    const cats = new Set<string>();
    const rms = new Set<string>();
    for (const e of raw) {
      if (e.category) cats.add(e.category);
      if (e.room_id) rms.add(String(e.room_id));
    }
    return {
      categoryFilterOptions: Array.from(cats)
        .sort()
        .map((c) => ({ value: c, label: EXPENSE_CATEGORIES[c] || c })),
      roomFilterOptions: Array.from(rms)
        .sort()
        .map((r) => ({ value: r, label: r })),
    };
  }, [expenses]);

  // 先按筛选过滤，再把同一 order_id 的多笔（≥2）折叠成可展开父行（纯逻辑见 lib/expense-grouping）。
  const expenseRows = useMemo(
    () => buildExpenseRows(expenses as any, { category: expCategory, room: expRoom, search: expSearch }),
    [expenses, expCategory, expRoom, expSearch],
  );

  const expFilterActive = !!(expCategory || expRoom || expSearch.trim());

  const { data: rooms } = useQuery({
    queryKey: ["rooms"],
    queryFn: () => roomsApi.list().then((r) => r.data),
    staleTime: 5 * 60 * 1000,
  });

  const roomOptions = (rooms || []).map((r) => ({
    value: r.room_id,
    label: `${r.room_id} · ${r.room_name ?? ""}`.trim(),
  }));

  const { data: owners } = useQuery({
    queryKey: ["owners"],
    queryFn: () => ownersApi.list().then((r) => r.data),
    staleTime: 5 * 60 * 1000,
  });
  const ownerOptions = (owners || []).map((o) => ({ value: o.owner_id, label: o.name }));

  // 楼层选项：从房间的 floor 去重（只列有托管房、有楼层的层）。整层录入时每层
  // 显示该层托管房数量，方便前台确认选对了层。
  const floorOptions = React.useMemo(() => {
    const counts = new Map<number, number>();
    (rooms || []).forEach((r) => {
      if (r.floor != null && r.owner_id) {
        counts.set(r.floor, (counts.get(r.floor) ?? 0) + 1);
      }
    });
    return Array.from(counts.entries())
      .sort((a, b) => a[0] - b[0])
      .map(([floor, n]) => ({ value: floor, label: `${floor} 层（${n} 间托管房）` }));
  }, [rooms]);

  // 关联对象模式：单个房间 / 整层 / 某业主
  const [scopeMode, setScopeMode] = useState<"room" | "floor" | "owner">("room");

  const byRoomParams = { start_date: startDate, end_date: endDate };
  const { data: byRoom, isLoading: byRoomLoading } = useQuery({
    queryKey: ["finance", "by-room", byRoomParams],
    queryFn: () => financeApi.summaryByRoom(byRoomParams).then((r) => r.data),
    placeholderData: keepPreviousData,
  });

  const addExpenseMutation = useMutation({
    mutationFn: (data: any) => financeApi.expenses.create(data),
    onSuccess: () => {
      message.success("支出已记录");
      qc.invalidateQueries({ queryKey: ["expenses"] });
      qc.invalidateQueries({ queryKey: ["dashboard", "monthly"] });
      setAddExpenseOpen(false);
      form.resetFields();
      setScopeMode("room");
    },
    onError: (err) => message.error(extractErrorMessage(err, "添加失败")),
  });

  const deleteExpenseMutation = useMutation({
    mutationFn: (id: string) => financeApi.expenses.delete(id),
    onSuccess: () => {
      message.success("支出已删除");
      qc.invalidateQueries({ queryKey: ["expenses"] });
      qc.invalidateQueries({ queryKey: ["dashboard", "monthly"] });
    },
    onError: (err) => message.error(extractErrorMessage(err, "删除失败")),
  });

  const expenseColumns: ColumnsType<any> = [
    {
      title: "日期",
      dataIndex: "expense_date",
      key: "date",
      width: 116,
      render: (v, record: any) => {
        if (record.__group) {
          const label =
            record.__dateMin === record.__dateMax
              ? record.__dateMin
              : `${record.__dateMin} ~ ${record.__dateMax}`;
          return <Text strong style={{ fontSize: 13 }}>{label}</Text>;
        }
        return <Text style={{ fontSize: 13 }}>{v}</Text>;
      },
    },
    {
      title: "类别",
      dataIndex: "category",
      key: "category",
      width: 120,
      render: (v, record: any) => {
        if (record.__group) {
          const cats: string[] = record.__cats;
          const shown = cats.slice(0, 2);
          return (
            <Space size={2} wrap>
              {shown.map((c) => (
                <Tag key={c} color={CATEGORY_COLORS[c] || "default"} bordered={false} style={{ fontSize: 11 }}>
                  {EXPENSE_CATEGORIES[c] || c}
                </Tag>
              ))}
              {cats.length > shown.length && (
                <Tag bordered={false} style={{ fontSize: 11 }}>+{cats.length - shown.length}</Tag>
              )}
            </Space>
          );
        }
        return (
          <Tag color={CATEGORY_COLORS[v] || "default"} bordered={false} style={{ fontSize: 11 }}>
            {EXPENSE_CATEGORIES[v] || v}
          </Tag>
        );
      },
    },
    {
      title: "描述",
      dataIndex: "description",
      key: "description",
      render: (v, record: any) => {
        if (record.__group) {
          return (
            <Text strong style={{ fontSize: 13 }}>
              同订单合计 {record.__count} 笔
              <Text type="secondary" style={{ fontWeight: 400, marginLeft: 6, fontSize: 12 }}>
                单 {record.order_id}
              </Text>
            </Text>
          );
        }
        return <Text style={{ fontSize: 13 }}>{v}</Text>;
      },
    },
    {
      title: "房间",
      dataIndex: "room_id",
      key: "room",
      width: 90,
      render: (v, record: any) => {
        if (record.__group) {
          const rooms: string[] = record.__rooms;
          if (rooms.length === 0) return <Text type="secondary">—</Text>;
          return (
            <span className="tabular">
              {rooms.length <= 2 ? rooms.join(", ") : `${rooms.slice(0, 2).join(", ")} +${rooms.length - 2}`}
            </span>
          );
        }
        return v || <Text type="secondary">—</Text>;
      },
    },
    {
      title: "支付方",
      dataIndex: "payer",
      key: "payer",
      width: 80,
      render: (v, record: any) => {
        const payer = record.__group
          ? record.__payers.length === 1
            ? record.__payers[0]
            : "mixed"
          : v;
        if (payer === "mixed") return <Tag bordered={false} style={{ fontSize: 11 }}>混合</Tag>;
        return payer === "owner" ? (
          <Tag color="gold" bordered={false} style={{ fontSize: 11 }}>业主</Tag>
        ) : (
          <Tag color="blue" bordered={false} style={{ fontSize: 11 }}>公司</Tag>
        );
      },
    },
    {
      title: "金额",
      dataIndex: "amount",
      key: "amount",
      width: 100,
      align: "right" as const,
      render: (v) => (
        <Text strong style={{ color: "#ff4d4f" }}>
          -¥{Number(v).toLocaleString()}
        </Text>
      ),
    },
    ...(user?.role === "admin"
      ? [
          {
            title: "操作",
            key: "action",
            width: 72,
            align: "center" as const,
            render: (_: unknown, record: any) =>
              record.__group ? null : (
                <Popconfirm
                  title="删除这条支出？"
                  description="删除后从汇总与业主结算中剔除（可从后台恢复）"
                  okText="删除"
                  cancelText="取消"
                  okButtonProps={{ danger: true, loading: deleteExpenseMutation.isPending }}
                  onConfirm={() => deleteExpenseMutation.mutate(record.expense_id || record.id)}
                >
                  <Button type="text" size="small" danger icon={<DeleteOutlined />} aria-label="删除支出" />
                </Popconfirm>
              ),
          },
        ]
      : []),
  ];

  const tabItems = [
    {
      key: "summary",
      label: "月度汇总",
      children: (
        <Skeleton loading={monthlyLoading} active>
          {monthly && (
            <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
              {/* Main metrics */}
              <Row gutter={[16, 16]}>
                <Col xs={12} sm={8} md={8}>
                  <StatCard title="总房费收入" value={Number(monthly.total_actual_price ?? 0).toLocaleString("zh-CN")}
                    prefix="¥" icon={<DollarOutlined />} tone="brand" />
                </Col>
                <Col xs={12} sm={8} md={8}>
                  <StatCard title="平台佣金" value={Number(monthly.total_commission ?? 0).toLocaleString("zh-CN")}
                    prefix="¥" icon={<FallOutlined />} tone="warn" />
                </Col>
                <Col xs={12} sm={8} md={8}>
                  <StatCard title="净收入" value={Number(monthly.total_net_revenue ?? 0).toLocaleString("zh-CN")}
                    prefix="¥" icon={<RiseOutlined />} tone="success" />
                </Col>
                <Col xs={12} sm={8} md={8}>
                  <StatCard title="总支出" value={Number(monthly.total_expenses ?? 0).toLocaleString("zh-CN")}
                    prefix="¥" icon={<FallOutlined />} tone="warn" />
                </Col>
                <Col xs={12} sm={8} md={8}>
                  <StatCard title="毛利润" value={Number(monthly.gross_profit ?? 0).toLocaleString("zh-CN")}
                    prefix="¥" icon={<DollarOutlined />} tone="success" />
                </Col>
                <Col xs={12} sm={8} md={8}>
                  <StatCard title="订单数" value={monthly.order_count ?? 0}
                    suffix="单" icon={<BarChartOutlined />} tone="info" />
                </Col>
              </Row>

              {/* KPI metrics */}
              <Row gutter={[16, 16]}>
                <Col xs={12} sm={8}>
                  <div
                    style={{
                      background: tokens.color.bg.container,
                      border: `1px solid ${tokens.color.bg.border}`,
                      borderRadius: tokens.radius.lg,
                      padding: 18,
                      textAlign: "center",
                      boxShadow: tokens.shadow.sm,
                    }}
                  >
                    <div style={{ fontSize: 12, color: tokens.color.text.secondary }}>入住率 OCC</div>
                    <div
                      className="tabular"
                      style={{ fontSize: 28, fontWeight: 700, marginTop: 4, letterSpacing: "-.01em" }}
                    >
                      {monthly.occ}%
                    </div>
                  </div>
                </Col>
                <Col xs={12} sm={8}>
                  <div
                    style={{
                      background: tokens.color.bg.container,
                      border: `1px solid ${tokens.color.bg.border}`,
                      borderRadius: tokens.radius.lg,
                      padding: 18,
                      textAlign: "center",
                      boxShadow: tokens.shadow.sm,
                    }}
                  >
                    <div style={{ fontSize: 12, color: tokens.color.text.secondary }}>平均房价 ADR</div>
                    <div
                      className="tabular"
                      style={{ fontSize: 28, fontWeight: 700, marginTop: 4, letterSpacing: "-.01em" }}
                    >
                      ¥{monthly.adr}
                    </div>
                  </div>
                </Col>
                <Col xs={12} sm={8}>
                  <div
                    style={{
                      background: tokens.color.bg.container,
                      border: `1px solid ${tokens.color.bg.border}`,
                      borderRadius: tokens.radius.lg,
                      padding: 18,
                      textAlign: "center",
                      boxShadow: tokens.shadow.sm,
                    }}
                  >
                    <div style={{ fontSize: 12, color: tokens.color.text.secondary }}>RevPAR</div>
                    <div
                      className="tabular"
                      style={{ fontSize: 28, fontWeight: 700, marginTop: 4, letterSpacing: "-.01em" }}
                    >
                      ¥{monthly.revpar}
                    </div>
                  </div>
                </Col>
              </Row>

              {/* Channel breakdown */}
              {monthly.channel_breakdown && Object.keys(monthly.channel_breakdown).length > 0 && (
                <Card bordered={false}
                  style={{ borderRadius: 12, boxShadow: "0 2px 8px rgba(0,0,0,0.06)" }}
                  title={<Text strong>渠道分布</Text>}>
                  <Space wrap>
                    {Object.entries(monthly.channel_breakdown).map(([ch, count]) => (
                      <Space key={ch} direction="vertical" size={0} style={{ textAlign: "center" }}>
                        <Tag color="processing" style={{ fontSize: 12, padding: "2px 10px" }}>{getChannelLabel(ch)}</Tag>
                        <Text type="secondary" style={{ fontSize: 12 }}>{count as number} 单</Text>
                      </Space>
                    ))}
                  </Space>
                </Card>
              )}
            </div>
          )}
        </Skeleton>
      ),
    },
    {
      key: "by-room",
      label: "按房号分账",
      children: (
        <div
          style={{
            background: tokens.color.bg.container,
            border: `1px solid ${tokens.color.bg.border}`,
            borderRadius: tokens.radius.lg,
            overflow: "hidden",
          }}
        >
          <div
            style={{
              padding: "14px 20px",
              borderBottom: `1px solid ${tokens.color.bg.borderSubtle}`,
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: 12,
              flexWrap: "wrap",
            }}
          >
            <div>
              <div style={{ fontWeight: 600 }}>
                {`${startDate} 至 ${endDate} · 每套房账单`}
              </div>
              <div style={{ fontSize: 12, color: tokens.color.text.tertiary, marginTop: 2 }}>
                跟随顶部日期区间。点击任意房号查看该房区间明细
              </div>
            </div>
          </div>
          <Alert
            type="info"
            showIcon
            banner
            message="仅供速览，分账以「业主结算单」为准"
            description="本页「业主应得」用简化口径估算，不认每类费用的业主分摊规则、不计携程补贴，可能与正式结算单有出入。正式分账请以结算单为准。"
            style={{ fontSize: 12 }}
          />
          <Table
            dataSource={byRoom ?? []}
            rowKey="room_id"
            loading={byRoomLoading}
            scroll={{ x: 960 }}
            size="middle"
            pagination={{ pageSize: 20, showTotal: (t) => `共 ${t} 套房`, style: { padding: "12px 20px" } }}
            onRow={(record) => ({
              onClick: () => router.push(`/finance/rooms/${record.room_id}?start_date=${startDate}&end_date=${endDate}`),
              style: { cursor: "pointer" },
            })}
            summary={(pageData) => {
              const sum = (key: keyof typeof pageData[number]) =>
                pageData.reduce((acc, r) => acc + Number(r[key] ?? 0), 0);
              return (
                <Table.Summary.Row style={{ background: tokens.color.bg.subtle, fontWeight: 600 }}>
                  <Table.Summary.Cell index={0}>合计</Table.Summary.Cell>
                  <Table.Summary.Cell index={1}></Table.Summary.Cell>
                  <Table.Summary.Cell index={2} align="right">{sum("order_count")}</Table.Summary.Cell>
                  <Table.Summary.Cell index={3} align="right" className="col-amount">¥{sum("revenue").toLocaleString()}</Table.Summary.Cell>
                  <Table.Summary.Cell index={4} align="right" className="col-amount">¥{sum("commission").toLocaleString()}</Table.Summary.Cell>
                  <Table.Summary.Cell index={5} align="right" className="col-amount">¥{sum("net_revenue").toLocaleString()}</Table.Summary.Cell>
                  <Table.Summary.Cell index={6} align="right" className="col-amount">¥{sum("expense_company").toLocaleString()}</Table.Summary.Cell>
                  <Table.Summary.Cell index={7} align="right" className="col-amount">¥{sum("expense_owner").toLocaleString()}</Table.Summary.Cell>
                  <Table.Summary.Cell index={8} align="right" className="col-amount">¥{sum("owner_net_share").toLocaleString()}</Table.Summary.Cell>
                  <Table.Summary.Cell index={9} align="right" className="col-amount">¥{sum("company_net").toLocaleString()}</Table.Summary.Cell>
                </Table.Summary.Row>
              );
            }}
            columns={[
              {
                title: "房号",
                dataIndex: "room_id",
                key: "room_id",
                width: 80,
                fixed: "left",
                render: (v) => <Text strong>{v}</Text>,
              },
              {
                title: "房名",
                dataIndex: "room_name",
                key: "room_name",
                width: 140,
                render: (v) => <Text>{v}</Text>,
              },
              {
                title: "订单",
                dataIndex: "order_count",
                key: "order_count",
                width: 70,
                align: "right",
                render: (v) => <span className="tabular">{v}</span>,
              },
              {
                title: "收入",
                dataIndex: "revenue",
                key: "revenue",
                width: 110,
                align: "right",
                className: "col-amount",
                render: (v) => (
                  <span className="tabular">¥{Number(v).toLocaleString()}</span>
                ),
              },
              {
                title: "佣金",
                dataIndex: "commission",
                key: "commission",
                width: 100,
                align: "right",
                className: "col-amount",
                render: (v) => (
                  <span className="tabular" style={{ color: tokens.color.text.tertiary }}>
                    -¥{Number(v).toLocaleString()}
                  </span>
                ),
              },
              {
                title: "净收入",
                dataIndex: "net_revenue",
                key: "net_revenue",
                width: 110,
                align: "right",
                className: "col-amount",
                render: (v) => (
                  <Text strong className="tabular">¥{Number(v).toLocaleString()}</Text>
                ),
              },
              {
                title: "公司支出",
                dataIndex: "expense_company",
                key: "expense_company",
                width: 100,
                align: "right",
                className: "col-amount",
                render: (v) => (
                  <span className="tabular" style={{ color: Number(v) > 0 ? "#ff4d4f" : tokens.color.text.tertiary }}>
                    ¥{Number(v).toLocaleString()}
                  </span>
                ),
              },
              {
                title: "业主支出",
                dataIndex: "expense_owner",
                key: "expense_owner",
                width: 100,
                align: "right",
                className: "col-amount",
                render: (v) => (
                  <span className="tabular" style={{ color: Number(v) > 0 ? "#ff4d4f" : tokens.color.text.tertiary }}>
                    ¥{Number(v).toLocaleString()}
                  </span>
                ),
              },
              {
                title: "业主应得",
                dataIndex: "owner_net_share",
                key: "owner_net_share",
                width: 110,
                align: "right",
                className: "col-amount",
                render: (v, r) => (
                  <div style={{ textAlign: "right" }}>
                    <Text strong className="tabular" style={{ color: "#2B2721" }}>
                      ¥{Number(v).toLocaleString()}
                    </Text>
                    {r.owner_share_ratio ? (
                      <div style={{ fontSize: 11, color: tokens.color.text.tertiary }}>
                        {Math.round(Number(r.owner_share_ratio) * 100)}% 分成
                      </div>
                    ) : null}
                  </div>
                ),
              },
              {
                title: "公司净利",
                dataIndex: "company_net",
                key: "company_net",
                width: 110,
                align: "right",
                className: "col-amount",
                render: (v) => (
                  <Text
                    strong
                    className="tabular"
                    style={{ color: Number(v) >= 0 ? "#10B981" : "#EF4444" }}
                  >
                    ¥{Number(v).toLocaleString()}
                  </Text>
                ),
              },
            ]}
            locale={{ emptyText: "本月暂无数据" }}
          />
        </div>
      ),
    },
    {
      key: "expenses",
      label: "支出明细",
      children: (
        <div
          style={{
            background: tokens.color.bg.container,
            border: `1px solid ${tokens.color.bg.border}`,
            borderRadius: tokens.radius.lg,
            overflow: "hidden",
          }}
        >
          <div
            style={{
              padding: "14px 20px",
              borderBottom: `1px solid ${tokens.color.bg.borderSubtle}`,
              display: "flex",
              flexDirection: "column",
              gap: 12,
            }}
          >
            <div
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                flexWrap: "wrap",
                gap: 8,
              }}
            >
              <div style={{ fontWeight: 600 }}>支出记录</div>
              <Space wrap>
                <Button size="small" icon={<FileExcelOutlined />} onClick={handleDownloadTemplate}>
                  下载模板
                </Button>
                <Button size="small" icon={<UploadOutlined />} onClick={() => setImportOpen(true)}>
                  Excel 导入
                </Button>
                <Button type="primary" size="small" icon={<PlusOutlined />} onClick={() => { form.resetFields(); setScopeMode("room"); setAddExpenseOpen(true); }}>
                  添加支出
                </Button>
              </Space>
            </div>
            {/* 筛选：类别 / 房号 / 描述搜索。父行按 order_id 折叠，筛选后组内只留命中项。 */}
            <Space wrap size={8}>
              <Select
                allowClear
                size="small"
                placeholder="类别"
                style={{ width: 130 }}
                value={expCategory}
                onChange={(v) => setExpCategory(v)}
                options={categoryFilterOptions}
              />
              <Select
                allowClear
                showSearch
                size="small"
                placeholder="房号"
                style={{ width: 110 }}
                value={expRoom}
                onChange={(v) => setExpRoom(v)}
                options={roomFilterOptions}
                filterOption={(input, opt) => String(opt?.label ?? "").includes(input)}
              />
              <Input.Search
                allowClear
                size="small"
                placeholder="搜索描述，如「1609」「历史补录」"
                style={{ width: 240 }}
                value={expSearch}
                onChange={(e) => setExpSearch(e.target.value)}
              />
              {expFilterActive && (
                <Button
                  size="small"
                  type="link"
                  onClick={() => { setExpCategory(undefined); setExpRoom(undefined); setExpSearch(""); }}
                >
                  清除筛选
                </Button>
              )}
            </Space>
          </div>
          <Table
            columns={expenseColumns}
            dataSource={expenseRows}
            rowKey={(r) => r.__key || r.expense_id || r.id}
            loading={expLoading}
            scroll={{ x: 560 }}
            expandable={{ indentSize: 14 }}
            pagination={{
              pageSize: 15,
              showTotal: (t) => (expFilterActive ? `筛选后 ${t} 项（同订单已折叠）` : `共 ${t} 项（同订单已折叠）`),
              style: { padding: "12px 20px" },
            }}
            size="middle"
            locale={{ emptyText: expFilterActive ? "没有匹配的支出记录" : "本区间暂无支出记录" }}
          />
        </div>
      ),
    },
    // 费用标准设置：仅 admin 可见（服务费费率影响业主分成）。
    ...(user?.role === "admin"
      ? [{
          key: "settings",
          label: "费用标准",
          children: <ServiceFeeConfigCard />,
        }]
      : []),
  ];

  // 无财务权限（如前台 operator 直达 URL）：不渲染任何财务内容，等 useEffect 重定向。
  if (user && !canAccessFinance) return null;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      <PageHeader
        title="财务管理"
        subtitle={`${startDate} 至 ${endDate} · 收支汇总与明细`}
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
            <Button icon={<DownloadOutlined />} loading={exportLoading} onClick={handleExportFinance}>
              导出 Excel
            </Button>
            <Button icon={<FileExcelOutlined />} onClick={() => router.push("/finance/reconciliation")}>
              对账
            </Button>
            {user?.role === "admin" && (
              <Button onClick={() => router.push("/finance/billing-recon")}>
                账单对账
              </Button>
            )}
          </Space>
        }
      />

      <Tabs items={tabItems} activeKey={activeTab} onChange={handleTabChange} />

      {/* Add Expense Modal */}
      <Modal open={addExpenseOpen} onCancel={() => setAddExpenseOpen(false)}
        title="添加支出记录" footer={null}>
        <Form form={form} layout="vertical" style={{ marginTop: 16 }}
          onFinish={(vals) => {
            // 按关联对象模式只带上对应的范围字段，其余清空，避免混传
            const scope =
              scopeMode === "floor" ? { floor: vals.floor, room_id: null, owner_id: null }
              : scopeMode === "owner" ? { owner_id: vals.owner_id, room_id: null, floor: null }
              : { room_id: vals.room_id ?? null, floor: null, owner_id: null };
            addExpenseMutation.mutate({
              ...vals,
              ...scope,
              expense_date: vals.expense_date?.format("YYYY-MM-DD"),
            });
          }}>
          <Form.Item name="category" label="类别" rules={[{ required: true }]}>
            <Select options={Object.entries(EXPENSE_CATEGORIES).filter(([k]) => !DEPRECATED_CATEGORIES.has(k)).map(([k, v]) => ({ value: k, label: v }))} />
          </Form.Item>
          <Form.Item name="description" label="描述" rules={[{ required: true }]}>
            <Input placeholder="请输入支出描述" />
          </Form.Item>
          <Row gutter={12}>
            <Col xs={24} sm={12}>
              <Form.Item name="amount" label="金额 (¥)" rules={[{ required: true }]}>
                <MobileInputNumber
                  style={{ width: "100%" }}
                  min={0}
                  precision={2}
                  placeholder="0.00"
                  inputMode="decimal"
                />
              </Form.Item>
            </Col>
            <Col xs={24} sm={12}>
              <Form.Item name="expense_date" label="日期" rules={[{ required: true }]}>
                <DatePicker style={{ width: "100%" }} />
              </Form.Item>
            </Col>
          </Row>
          <Form.Item label="关联对象（可选）">
            <Radio.Group
              value={scopeMode}
              onChange={(e) => setScopeMode(e.target.value)}
              optionType="button"
              buttonStyle="solid"
              style={{ marginBottom: 12 }}
            >
              <Radio.Button value="room">单个房间</Radio.Button>
              <Radio.Button value="floor">整层</Radio.Button>
              <Radio.Button value="owner">某业主</Radio.Button>
            </Radio.Group>

            {scopeMode === "room" && (
              <Form.Item name="room_id" noStyle>
                <Select
                  allowClear
                  showSearch
                  placeholder="选择房间"
                  options={roomOptions}
                  filterOption={(input, opt) =>
                    (opt?.label ?? "").toLowerCase().includes(input.toLowerCase())
                  }
                />
              </Form.Item>
            )}
            {scopeMode === "floor" && (
              <>
                <Form.Item name="floor" noStyle rules={[{ required: true, message: "请选择楼层" }]}>
                  <Select placeholder="选择楼层" options={floorOptions} />
                </Form.Item>
                <Text type="secondary" style={{ display: "block", marginTop: 6, fontSize: 12 }}>
                  金额整笔计入该层业主，不拆分到各房。若该层有多个业主，请改用「某业主」分别录入。
                </Text>
              </>
            )}
            {scopeMode === "owner" && (
              <>
                <Form.Item name="owner_id" noStyle rules={[{ required: true, message: "请选择业主" }]}>
                  <Select
                    showSearch
                    placeholder="选择业主"
                    options={ownerOptions}
                    filterOption={(input, opt) =>
                      (opt?.label ?? "").toLowerCase().includes(input.toLowerCase())
                    }
                  />
                </Form.Item>
                <Text type="secondary" style={{ display: "block", marginTop: 6, fontSize: 12 }}>
                  金额整笔计入该业主，不拆分到各房。
                </Text>
              </>
            )}
          </Form.Item>
          <Form.Item
            name="payer"
            label="支付方"
            initialValue="owner"
            tooltip="标记公司垫付（公司支出）或业主承担（从业主分成中扣除）"
          >
            <Select
              options={[
                { value: "company", label: "公司" },
                { value: "owner", label: "业主（从分成中扣除）" },
              ]}
            />
          </Form.Item>
          <Form.Item name="notes" label="备注（可选）">
            <Input.TextArea rows={2} placeholder="供应商、发票号等" maxLength={200} />
          </Form.Item>
          <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
            <Button onClick={() => setAddExpenseOpen(false)}>取消</Button>
            <Button type="primary" htmlType="submit" loading={addExpenseMutation.isPending}>
              保存
            </Button>
          </div>
        </Form>
      </Modal>

      {/* Excel Import Modal */}
      <Modal
        open={importOpen}
        onCancel={() => {
          setImportOpen(false);
          setImportResult(null);
        }}
        title="批量导入支出"
        footer={null}
        width={isMobile ? undefined : 720}
      >
        <div style={{ marginTop: 12, display: "flex", flexDirection: "column", gap: 16 }}>
          <Alert
            type="info"
            showIcon
            message={
              <span>
                1. 先下载模板填写；2. 日期 <code>YYYY-MM-DD</code>；
                3. 类别可填中文或英文；4. 房号支持「101」或「R101」；
                5. 支付方填「公司」或「业主」（业主项将从业主分成中扣除）。
              </span>
            }
          />

          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <Button icon={<FileExcelOutlined />} onClick={handleDownloadTemplate}>
              下载导入模板
            </Button>
            <Upload
              customRequest={handleImportFile}
              accept=".xlsx,.xls"
              showUploadList={false}
              maxCount={1}
            >
              <Button type="primary" icon={<UploadOutlined />} loading={importing}>
                选择 Excel 文件上传
              </Button>
            </Upload>
          </div>

          {importResult && (
            <>
              <div
                style={{
                  padding: 12,
                  background: tokens.color.bg.subtle,
                  borderRadius: 8,
                  display: "flex",
                  gap: 24,
                  flexWrap: "wrap",
                }}
              >
                <div>
                  <div style={{ fontSize: 12, color: tokens.color.text.tertiary }}>总行数</div>
                  <div style={{ fontSize: 20, fontWeight: 600 }}>{importResult.total_rows}</div>
                </div>
                <div>
                  <div style={{ fontSize: 12, color: tokens.color.text.tertiary }}>成功</div>
                  <div style={{ fontSize: 20, fontWeight: 600, color: "#10B981" }}>
                    {importResult.imported_count}
                  </div>
                </div>
                <div>
                  <div style={{ fontSize: 12, color: tokens.color.text.tertiary }}>失败</div>
                  <div style={{ fontSize: 20, fontWeight: 600, color: "#EF4444" }}>
                    {importResult.failed.length}
                  </div>
                </div>
              </div>

              {importResult.failed.length > 0 && (
                <div>
                  <Text strong style={{ display: "block", marginBottom: 8 }}>
                    失败行明细（已跳过，不影响成功行）
                  </Text>
                  <List
                    size="small"
                    bordered
                    dataSource={importResult.failed}
                    style={{ maxHeight: 300, overflow: "auto" }}
                    renderItem={(f) => (
                      <List.Item>
                        <div>
                          <Tag color="red">第 {f.row} 行</Tag>
                          <Text type="danger" style={{ fontSize: 13 }}>
                            {f.errors}
                          </Text>
                          <div style={{ fontSize: 11, color: tokens.color.text.tertiary, marginTop: 4 }}>
                            {JSON.stringify(f.data)}
                          </div>
                        </div>
                      </List.Item>
                    )}
                  />
                </div>
              )}

              <div style={{ textAlign: "right" }}>
                <Button
                  type="primary"
                  onClick={() => {
                    setImportOpen(false);
                    setImportResult(null);
                  }}
                >
                  完成
                </Button>
              </div>
            </>
          )}
        </div>
      </Modal>
    </div>
  );
}
