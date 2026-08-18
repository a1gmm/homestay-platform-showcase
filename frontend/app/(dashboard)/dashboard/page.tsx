"use client";

import React, { useMemo, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { ordersApi, tasksApi } from "@/lib/api";
import { todayCNString, todayCNYearMonth } from "@/lib/utils";
import { Card, Col, Row, Skeleton, Space, Tooltip, Segmented } from "antd";
import {
  UserAddOutlined,
  LogoutOutlined,
  ClearOutlined,
  WarningOutlined,
  DollarOutlined,
  RiseOutlined,
  BarChartOutlined,
  ArrowRightOutlined,
  CalendarOutlined,
  CheckSquareOutlined,
  QuestionCircleOutlined,
} from "@ant-design/icons";
import {
  AreaChart,
  Area,
  PieChart,
  Pie,
  Cell,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip as RechartsTooltip,
  ResponsiveContainer,
  RadialBarChart,
  RadialBar,
  PolarAngleAxis,
  BarChart,
  Bar,
  LabelList,
} from "recharts";
import { useDashboard } from "@/hooks/useDashboard";
import { useIsMobile } from "@/lib/responsive";
import { StatCard } from "@/components/ui/StatCard";
import { PageHeader } from "@/components/ui/PageHeader";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { EmptyState } from "@/components/ui/EmptyState";
import { tokens } from "@/lib/design-tokens";
import { CHANNEL_LABELS } from "@/lib/channels";
import { useAuthStore } from "@/lib/auth";
import { KpiPeekDrawer } from "@/components/dashboard/KpiPeekDrawer";
import { buildPeekPresets, buildTodayCheckinListParams } from "@/lib/dashboard-peek";
import { buildChannelShare, type ChannelMetric } from "@/lib/channel-share";

const PIE_PALETTE = ["#2B2721", "#6B665B", "#7B8578", "#8A6E5A", "#A89680", "#5C5547"];

function formatCNY(n: number | undefined | null) {
  if (n === undefined || n === null) return "0";
  return Number(n).toLocaleString("zh-CN", { maximumFractionDigits: 0 });
}

function formatCompact(n: number | undefined | null) {
  if (n === undefined || n === null) return "0";
  const v = Math.abs(n);
  if (v >= 1_0000_0000) return (n / 1_0000_0000).toFixed(2) + "亿";
  if (v >= 1_0000) return (n / 1_0000).toFixed(1) + "万";
  return n.toLocaleString("zh-CN");
}

// 各指标量纲不同（收入/ADR 是金额、订单数是次数、入住率是百分比），
// 小图各自格式化，不共用一个数值口径。
function formatMetric(name: string, v: number | undefined | null) {
  const n = v ?? 0;
  if (name === "收入") return "¥" + formatCompact(n);
  if (name === "ADR") return "¥" + formatCNY(n);
  if (name === "入住率") return n.toFixed(1) + "%";
  return formatCNY(n); // 订单数
}

// 今日卡片底部直显房号——只列前 3 间避免撑高卡片(全量点卡片进抽屉看)；
// 无房号（全待排房/无单）时回落到原提示语。
const ROOMS_FOOTER_MAX = 3;
function roomsFooter(rooms: string[] | undefined, fallback: string) {
  if (!rooms || !rooms.length) return fallback;
  if (rooms.length <= ROOMS_FOOTER_MAX) return `房号 ${rooms.join("、")}`;
  return `房号 ${rooms.slice(0, ROOMS_FOOTER_MAX).join("、")} …等 ${rooms.length} 间`;
}

function SectionTitle({
  title,
  extra,
}: {
  title: React.ReactNode;
  extra?: React.ReactNode;
}) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        marginBottom: 12,
      }}
    >
      <div
        style={{
          fontSize: tokens.font.size.lg,
          fontWeight: tokens.font.weight.semibold,
          color: tokens.color.text.primary,
          letterSpacing: "-.01em",
        }}
      >
        {title}
      </div>
      {extra}
    </div>
  );
}

function ChartCard({
  title,
  subtitle,
  extra,
  children,
  height,
  empty,
}: {
  title: React.ReactNode;
  subtitle?: React.ReactNode;
  extra?: React.ReactNode;
  children: React.ReactNode;
  height?: number;
  empty?: boolean;
}) {
  return (
    <Card
      bordered={false}
      style={{
        borderRadius: tokens.radius.lg,
        border: `1px solid ${tokens.color.bg.border}`,
        boxShadow: tokens.shadow.sm,
        height: "100%",
      }}
      styles={{ body: { padding: 20 } }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "flex-start",
          justifyContent: "space-between",
          marginBottom: 14,
        }}
      >
        <div>
          <div
            style={{
              fontSize: tokens.font.size.base,
              fontWeight: tokens.font.weight.semibold,
              color: tokens.color.text.primary,
            }}
          >
            {title}
          </div>
          {subtitle && (
            <div
              style={{
                marginTop: 2,
                fontSize: tokens.font.size.xs,
                color: tokens.color.text.tertiary,
              }}
            >
              {subtitle}
            </div>
          )}
        </div>
        {extra}
      </div>
      <div style={{ height: height ?? 240 }}>
        {empty ? (
          <EmptyState title="暂无数据" size="sm" />
        ) : (
          children
        )}
      </div>
    </Card>
  );
}

export default function DashboardPage() {
  const router = useRouter();
  const isMobile = useIsMobile();
  const { year, month } = todayCNYearMonth();
  const todayDateStr = todayCNString();

  // 点 KPI 卡片 → 同屏抽屉速览当前这批（不整页跳到订单大表）。
  const peekPresets = useMemo(() => buildPeekPresets(todayDateStr), [todayDateStr]);
  const [peekKey, setPeekKey] = useState<string | null>(null);

  // 财务金额可见性：仅 admin / finance。operator / keeper 等只看运营/房价指标。
  // 真正的金额裁剪在后端（这些角色拿到的金额字段已是 null），此处仅控制 UI 呈现，
  // 避免出现 ¥0 / 空卡。
  const userRole = useAuthStore((s) => s.user?.role);
  const canViewRevenue = userRole === "admin" || userRole === "finance";

  const { todayStats, monthlyStats, trend, channel, comparison, isLoading } = useDashboard();
  const todayData = todayStats;
  const monthlyData = monthlyStats;
  const trendData = trend ?? [];
  const todayLoading = isLoading;
  const monthlyLoading = isLoading;
  const channelData = channel;
  const comparisonData = comparison;

  // 口径必须与上方 KPI 卡一致：只列「今天该办入住、还没办」的单。不带 status 会把
  // 已入住/已取消的单一起捞进来（生产 2026-07-25 前 5 条：2 已入住 + 2 已取消）。
  const checkinListParams = useMemo(
    () => buildTodayCheckinListParams(todayDateStr),
    [todayDateStr]
  );
  const { data: todayCheckinOrders } = useQuery({
    queryKey: ["dashboard", "today-checkins", checkinListParams],
    queryFn: () => ordersApi.list(checkinListParams).then((r) => r.data.items),
  });

  const { data: overdueTasks } = useQuery({
    queryKey: ["dashboard", "overdue-tasks"],
    queryFn: () => tasksApi.list({ overdue_only: true }).then((r) => r.data.slice(0, 5)),
  });

  // #6 渠道分布支持「单量 / 净收入」两个口径切换(净收入仅财务可见)。
  // 携程单量可能第一,但扣完佣金到手未必第一——净收入口径才是老板要看的。
  const [channelMetric, setChannelMetric] = useState<ChannelMetric>("orders");
  const { pieData, pieTotal } = useMemo(() => {
    const rows = channelData || [];
    const { items, total } = buildChannelShare(rows as any, channelMetric);
    const byChannel = new Map(rows.map((r) => [r.channel, r]));
    return {
      pieData: items.map((it, i) => {
        const src = byChannel.get(it.channel);
        return {
          name: CHANNEL_LABELS[it.channel] || it.channel,
          value: it.value,
          pct: it.pct,
          orders: src?.order_count ?? 0,
          revenue: src?.net_revenue ?? 0,
          color: PIE_PALETTE[i % PIE_PALETTE.length],
        };
      }),
      pieTotal: total,
    };
  }, [channelData, channelMetric]);
  const channelIsRevenue = channelMetric === "net_revenue";

  const { trendMax, trendMin } = useMemo(() => ({
    trendMax: Math.max(...trendData.map((d) => d.revenue), 1),
    trendMin: Math.min(...trendData.map((d) => d.revenue), 0),
  }), [trendData]);

  const occRate = todayData?.occupancy_rate ?? 0;
  const occRadialData = useMemo(
    () => [{ name: "入住率", value: occRate, fill: tokens.color.brand.primary }],
    [occRate]
  );

  const comparisonBarData = useMemo(
    () =>
      comparisonData
        ? [
            // 「收入」对比仅财务可见——金额，前台隐藏。
            ...(canViewRevenue
              ? [{ name: "收入", current: comparisonData.current.revenue, prev: comparisonData.last_month.revenue }]
              : []),
            { name: "订单数", current: comparisonData.current.order_count, prev: comparisonData.last_month.order_count },
            { name: "入住率", current: comparisonData.current.occupancy, prev: comparisonData.last_month.occupancy },
            // ADR 属价格敏感，仅财务可见
            ...(canViewRevenue
              ? [{ name: "ADR", current: comparisonData.current.adr, prev: comparisonData.last_month.adr }]
              : []),
          ]
        : [],
    [comparisonData, canViewRevenue]
  );

  const revenueMoM = comparisonData?.mom_change?.revenue;
  const occupancyMoM = comparisonData?.mom_change?.occupancy;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20, paddingBottom: 24 }}>
      <PageHeader
        title="今日概览"
        subtitle={new Date().toLocaleDateString("zh-CN", {
          timeZone: "Asia/Shanghai",
          year: "numeric",
          month: "long",
          day: "numeric",
          weekday: "long",
        })}
        extra={
          <span
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 6,
              padding: "4px 10px",
              borderRadius: 999,
              background: tokens.color.status.activeSoft,
              color: "#065F46",
              fontSize: tokens.font.size.xs,
              fontWeight: 500,
            }}
          >
            <span
              style={{
                width: 6,
                height: 6,
                borderRadius: "50%",
                background: tokens.color.status.active,
                boxShadow: "0 0 0 3px rgba(16,185,129,.2)",
              }}
            />
            实时数据
          </span>
        }
      />

      {/* Today KPIs — 卡片可点击,跳转到对应列表(带筛选预填) */}
      <Row gutter={[16, 16]}>
        <Col xs={12} md={6}>
          <StatCard
            title="今日待入住"
            value={todayLoading ? "–" : todayData?.checkin_today ?? 0}
            icon={<UserAddOutlined />}
            tone="brand"
            footer={roomsFooter(todayData?.checkin_rooms, "需提前备房")}
            loading={todayLoading}
            onClick={() => setPeekKey("checkin")}
          />
        </Col>
        <Col xs={12} md={6}>
          <StatCard
            title="今日待退房"
            value={todayLoading ? "–" : todayData?.checkout_today ?? 0}
            icon={<LogoutOutlined />}
            tone="warn"
            footer={roomsFooter(todayData?.checkout_rooms, "12:00 前处理")}
            loading={todayLoading}
            onClick={() => setPeekKey("checkout")}
          />
        </Col>
        <Col xs={12} md={6}>
          <StatCard
            title="待保洁房间"
            value={todayLoading ? "–" : todayData?.cleaning_needed ?? 0}
            icon={<ClearOutlined />}
            tone="info"
            footer={roomsFooter(todayData?.cleaning_rooms, "保洁组待领取")}
            loading={todayLoading}
            onClick={() => setPeekKey("cleaning")}
          />
        </Col>
        <Col xs={12} md={6}>
          {/* #5 视觉分层:无逾期时(常态)弱化——灰底+中性色,不与净收入/OCC 抢视觉;
              有逾期才亮成 warn 抓眼。 */}
          <StatCard
            title="逾期任务"
            value={todayLoading ? "–" : todayData?.overdue_tasks ?? 0}
            icon={<WarningOutlined />}
            tone={(todayData?.overdue_tasks ?? 0) > 0 ? "warn" : "neutral"}
            footer={(todayData?.overdue_tasks ?? 0) > 0 ? "需尽快处理" : "一切正常"}
            loading={todayLoading}
            onClick={() => setPeekKey("overdue")}
            style={
              (todayData?.overdue_tasks ?? 0) > 0
                ? undefined
                : { background: tokens.color.bg.subtle, boxShadow: "none" }
            }
          />
        </Col>
      </Row>

      {/* Occupancy + monthly KPIs */}
      {/* 前台(无营收权限)：右侧财务卡被抽走，把「实时在住 / 本月OCC」改成 12/12 两半对齐，
          避免右侧 2/3 只剩一张 OCC 卡撑宽、版面塌空。财务角色维持 8/16。 */}
      <Row gutter={[16, 16]}>
        <Col xs={24} lg={canViewRevenue ? 8 : 12}>
          <Card
            bordered={false}
            style={{
              borderRadius: tokens.radius.lg,
              border: `1px solid ${tokens.color.bg.border}`,
              boxShadow: tokens.shadow.sm,
              height: "100%",
            }}
            styles={{ body: { padding: 20 } }}
          >
            <SectionTitle title="实时在住" />
            <div style={{ display: "flex", alignItems: "center", gap: 20 }}>
              <div style={{ width: 160, height: 160, position: "relative", flex: "0 0 160px" }}>
                <ResponsiveContainer>
                  <RadialBarChart
                    cx="50%"
                    cy="50%"
                    innerRadius="72%"
                    outerRadius="100%"
                    barSize={14}
                    data={occRadialData}
                    startAngle={90}
                    endAngle={-270}
                  >
                    <defs>
                      <linearGradient id="occGrad" x1="0" y1="0" x2="1" y2="1">
                        <stop offset="0%" stopColor="#3E3831" />
                        <stop offset="100%" stopColor="#2B2721" />
                      </linearGradient>
                    </defs>
                    <PolarAngleAxis
                      type="number"
                      domain={[0, 100]}
                      angleAxisId={0}
                      tick={false}
                    />
                    <RadialBar
                      background={{ fill: tokens.color.bg.subtle } as any}
                      dataKey="value"
                      cornerRadius={999}
                      fill="url(#occGrad)"
                      isAnimationActive
                    />
                  </RadialBarChart>
                </ResponsiveContainer>
                <div
                  style={{
                    position: "absolute",
                    inset: 0,
                    display: "flex",
                    flexDirection: "column",
                    alignItems: "center",
                    justifyContent: "center",
                  }}
                >
                  <span
                    className="serif tabular"
                    style={{
                      fontSize: 28,
                      fontWeight: 400,
                      letterSpacing: 0,
                      color: tokens.color.text.primary,
                    }}
                  >
                    {occRate}%
                  </span>
                  <span style={{ fontSize: 12, color: tokens.color.text.tertiary }}>在住率</span>
                </div>
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 12, flex: 1 }}>
                <div>
                  <div style={{ fontSize: 12, color: tokens.color.text.secondary }}>在住 / 总数</div>
                  <div
                    className="tabular"
                    style={{ fontSize: 20, fontWeight: 600, marginTop: 2 }}
                  >
                    {todayData?.checked_in ?? 0}
                    <span style={{ color: tokens.color.text.tertiary, fontWeight: 400 }}>
                      {" "}/ {todayData?.total_rooms ?? "—"}
                    </span>
                  </div>
                </div>
                {typeof occupancyMoM === "number" && (
                  <div>
                    <div style={{ fontSize: 12, color: tokens.color.text.secondary }}>较上月同期</div>
                    <div
                      className="tabular"
                      style={{
                        fontSize: 18,
                        fontWeight: 600,
                        marginTop: 2,
                        color:
                          occupancyMoM >= 0 ? tokens.color.status.active : tokens.color.status.warn,
                      }}
                    >
                      {occupancyMoM >= 0 ? "▲" : "▼"} {Math.abs(occupancyMoM).toFixed(1)}%
                    </div>
                  </div>
                )}
              </div>
            </div>
          </Card>
        </Col>

        <Col xs={24} lg={canViewRevenue ? 16 : 12}>
          <Row gutter={[16, 16]}>
            {canViewRevenue && (
              <Col xs={24} sm={12} md={12}>
                <StatCard
                  title={`${year}年${month}月净收入`}
                  value={formatCNY(monthlyData?.total_net_revenue)}
                  prefix="¥"
                  icon={<DollarOutlined />}
                  tone="success"
                  delta={
                    typeof revenueMoM === "number"
                      ? { value: revenueMoM, label: "较上月同期" }
                      : undefined
                  }
                  loading={monthlyLoading}
                />
              </Col>
            )}
            {/* OCC 所有有权限的角色都看；前台无 ADR/RevPAR 时让 OCC 占满 */}
            <Col xs={24} sm={canViewRevenue ? 12 : 24} md={canViewRevenue ? 12 : 24}>
              <StatCard
                title={
                  <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                    本月入住率 OCC
                    <Tooltip title="本月已售间夜 ÷ 可售间夜（房间数 × 当月天数）。与左侧「实时在住」口径不同——那是此刻在住房数占比。">
                      <QuestionCircleOutlined
                        style={{ fontSize: 12, color: tokens.color.text.tertiary, cursor: "help" }}
                      />
                    </Tooltip>
                  </span>
                }
                value={monthlyData?.occ ?? 0}
                suffix="%"
                icon={<RiseOutlined />}
                tone="brand"
                footer={`订单 ${monthlyData?.order_count ?? 0} 笔`}
                loading={monthlyLoading}
              />
            </Col>
            {/* ADR / RevPAR 属价格敏感，仅财务可见 */}
            {canViewRevenue && (
              <Col xs={24} sm={12} md={12}>
                <StatCard
                  title="平均房价 ADR"
                  value={formatCNY(monthlyData?.adr)}
                  prefix="¥"
                  icon={<DollarOutlined />}
                  tone="info"
                  footer={`客房 ${monthlyData?.total_nights ?? 0} 间夜`}
                  loading={monthlyLoading}
                />
              </Col>
            )}
            {canViewRevenue && (
              <Col xs={24} sm={12} md={12}>
                <StatCard
                  title="每可用房收益 RevPAR"
                  value={formatCNY(monthlyData?.revpar)}
                  prefix="¥"
                  icon={<BarChartOutlined />}
                  tone="warn"
                  footer={`佣金 ¥${formatCompact(monthlyData?.total_commission)}`}
                  loading={monthlyLoading}
                />
              </Col>
            )}
          </Row>
        </Col>
      </Row>

      {/* Revenue trend (finance only) + Channel doughnut */}
      <Row gutter={[16, 16]}>
        {canViewRevenue && (
        <Col xs={24} lg={14}>
          <ChartCard
            title="近 6 个月收入趋势"
            subtitle="净收入 (¥)"
            height={isMobile ? 220 : 260}
            empty={trendData.length === 0}
          >
            <ResponsiveContainer>
              <AreaChart
                data={trendData}
                margin={{ top: 8, right: 12, left: 0, bottom: 0 }}
              >
                <defs>
                  <linearGradient id="revGrad" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#2B2721" stopOpacity={0.32} />
                    <stop offset="100%" stopColor="#2B2721" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke={tokens.color.bg.border} vertical={false} />
                <XAxis
                  dataKey="month"
                  tick={{ fontSize: 11, fill: tokens.color.text.tertiary }}
                  axisLine={false}
                  tickLine={false}
                />
                <YAxis
                  tick={{ fontSize: 11, fill: tokens.color.text.tertiary }}
                  axisLine={false}
                  tickLine={false}
                  tickFormatter={(v) => `¥${formatCompact(v)}`}
                  width={56}
                  domain={[Math.max(0, trendMin * 0.9), trendMax * 1.1]}
                />
                <RechartsTooltip
                  cursor={{ stroke: tokens.color.brand.primary, strokeDasharray: "3 3", strokeOpacity: 0.5 }}
                  formatter={(v: any) => [`¥${Number(v).toLocaleString("zh-CN")}`, "净收入"]}
                  contentStyle={{
                    borderRadius: tokens.radius.md,
                    border: `1px solid ${tokens.color.bg.border}`,
                    boxShadow: tokens.shadow.md,
                    fontSize: 12,
                  }}
                />
                <Area
                  type="monotone"
                  dataKey="revenue"
                  stroke="#2B2721"
                  strokeWidth={2.5}
                  fill="url(#revGrad)"
                  activeDot={{ r: 5, strokeWidth: 2, stroke: "#fff" }}
                />
              </AreaChart>
            </ResponsiveContainer>
          </ChartCard>
        </Col>
        )}

        <Col xs={24} lg={canViewRevenue ? 10 : 24}>
          <ChartCard
            title="渠道分布"
            subtitle={
              channelIsRevenue
                ? `按净收入 · 共 ¥${formatCompact(pieTotal)}`
                : `按单量 · 共 ${pieTotal} 笔`
            }
            height={isMobile ? 220 : 260}
            extra={
              canViewRevenue ? (
                <Segmented
                  size="small"
                  value={channelMetric}
                  onChange={(v) => setChannelMetric(v as ChannelMetric)}
                  options={[
                    { label: "单量", value: "orders" },
                    { label: "净收入", value: "net_revenue" },
                  ]}
                />
              ) : undefined
            }
            empty={pieData.length === 0}
          >
            <div style={{ display: "flex", height: "100%", alignItems: "center", gap: 8 }}>
              <div style={{ flex: "0 0 180px", height: "100%", position: "relative" }}>
                <ResponsiveContainer>
                  <PieChart>
                    <Pie
                      data={pieData}
                      dataKey="value"
                      nameKey="name"
                      cx="50%"
                      cy="50%"
                      outerRadius="90%"
                      innerRadius="68%"
                      paddingAngle={2}
                      stroke="none"
                    >
                      {pieData.map((d, i) => (
                        <Cell key={i} fill={d.color} />
                      ))}
                    </Pie>
                    <RechartsTooltip
                      formatter={(_v: any, _n: any, p: any) => [
                        canViewRevenue
                          ? `${p?.payload?.orders} 单 · 净收入 ¥${formatCompact(p?.payload?.revenue)}`
                          : `${p?.payload?.orders} 单`,
                        p?.payload?.name,
                      ]}
                      contentStyle={{
                        borderRadius: tokens.radius.md,
                        border: `1px solid ${tokens.color.bg.border}`,
                        boxShadow: tokens.shadow.md,
                        fontSize: 12,
                      }}
                    />
                  </PieChart>
                </ResponsiveContainer>
                <div
                  style={{
                    position: "absolute",
                    inset: 0,
                    display: "flex",
                    flexDirection: "column",
                    alignItems: "center",
                    justifyContent: "center",
                    pointerEvents: "none",
                  }}
                >
                  <span
                    className="tabular"
                    style={{ fontSize: channelIsRevenue ? 20 : 24, fontWeight: 700, letterSpacing: "-.02em" }}
                  >
                    {channelIsRevenue ? `¥${formatCompact(pieTotal)}` : pieTotal}
                  </span>
                  <span style={{ fontSize: 11, color: tokens.color.text.tertiary }}>
                    {channelIsRevenue ? "净收入" : "总订单"}
                  </span>
                </div>
              </div>
              <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 8 }}>
                {pieData.map((d) => (
                  <div
                    key={d.name}
                    style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12 }}
                  >
                    <span
                      style={{
                        width: 10,
                        height: 10,
                        borderRadius: 3,
                        background: d.color,
                        flex: "0 0 10px",
                      }}
                    />
                    <span style={{ flex: 1, color: tokens.color.text.primary }}>{d.name}</span>
                    <span className="tabular" style={{ color: tokens.color.text.secondary }}>
                      {channelIsRevenue ? `¥${formatCompact(d.revenue)}` : d.orders}
                    </span>
                    <span
                      className="tabular"
                      style={{ color: tokens.color.text.tertiary, width: 42, textAlign: "right" }}
                    >
                      {d.pct.toFixed(0)}%
                    </span>
                  </div>
                ))}
              </div>
            </div>
          </ChartCard>
        </Col>
      </Row>

      {/* Comparison + quick lists */}
      <Row gutter={[16, 16]}>
        <Col xs={24} lg={14}>
          <ChartCard
            title="本月至今 vs 上月同期"
            subtitle="按同一天数对齐,避免月初虚高"
            height={isMobile ? 240 : 260}
            empty={comparisonBarData.length === 0}
          >
            <div
              style={{
                height: "100%",
                display: "flex",
                flexDirection: "column",
                gap: 8,
              }}
            >
              {/* 各指标量纲不同，拆成小倍数（small multiples）——每个指标独立自适应 Y 轴，
                  避免收入把订单数/入住率/ADR 压成看不见的零柱。 */}
              <div
                style={{
                  flex: 1,
                  minHeight: 0,
                  display: "grid",
                  gridTemplateColumns: "repeat(2, 1fr)",
                  gridAutoRows: "1fr",
                  columnGap: 16,
                  rowGap: 8,
                }}
              >
                {comparisonBarData.map((item) => (
                  <div
                    key={item.name}
                    style={{ display: "flex", flexDirection: "column", minHeight: 0 }}
                  >
                    <div
                      style={{
                        fontSize: 12,
                        fontWeight: 500,
                        color: tokens.color.text.secondary,
                      }}
                    >
                      {item.name}
                    </div>
                    <div style={{ flex: 1, minHeight: 0 }}>
                      <ResponsiveContainer width="100%" height="100%">
                        <BarChart
                          data={[item]}
                          margin={{ top: 16, right: 8, left: 8, bottom: 2 }}
                          barCategoryGap="18%"
                          barGap={6}
                        >
                          <YAxis hide domain={[0, (max: number) => max * 1.25 || 1]} />
                          <XAxis dataKey="name" hide />
                          <RechartsTooltip
                            cursor={{ fill: "rgba(46,92,255,.05)" }}
                            formatter={(value: number, dataName) => [
                              formatMetric(item.name, value),
                              dataName,
                            ]}
                            contentStyle={{
                              borderRadius: tokens.radius.md,
                              border: `1px solid ${tokens.color.bg.border}`,
                              boxShadow: tokens.shadow.md,
                              fontSize: 12,
                            }}
                          />
                          <Bar dataKey="current" name="本月" fill="#2B2721" radius={[3, 3, 0, 0]} maxBarSize={36} isAnimationActive={false}>
                            <LabelList
                              dataKey="current"
                              position="top"
                              fontSize={10}
                              fill={tokens.color.text.primary}
                              formatter={(v: number) => formatMetric(item.name, v)}
                            />
                          </Bar>
                          <Bar dataKey="prev" name="上月" fill={tokens.color.bg.border} radius={[3, 3, 0, 0]} maxBarSize={36} isAnimationActive={false}>
                            <LabelList
                              dataKey="prev"
                              position="top"
                              fontSize={10}
                              fill={tokens.color.text.tertiary}
                              formatter={(v: number) => formatMetric(item.name, v)}
                            />
                          </Bar>
                        </BarChart>
                      </ResponsiveContainer>
                    </div>
                  </div>
                ))}
              </div>
              {/* 共享图例：本月 / 上月 */}
              <div
                style={{
                  display: "flex",
                  justifyContent: "center",
                  gap: 20,
                  fontSize: 12,
                  color: tokens.color.text.tertiary,
                }}
              >
                <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                  <span
                    style={{ width: 10, height: 10, borderRadius: 2, background: "#2B2721" }}
                  />
                  本月
                </span>
                <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                  <span
                    style={{
                      width: 10,
                      height: 10,
                      borderRadius: 2,
                      background: tokens.color.bg.border,
                    }}
                  />
                  上月
                </span>
              </div>
            </div>
          </ChartCard>
        </Col>

        <Col xs={24} lg={10}>
          <Row gutter={[16, 16]}>
            <Col xs={24}>
              <Card
                bordered={false}
                style={{
                  borderRadius: tokens.radius.lg,
                  border: `1px solid ${tokens.color.bg.border}`,
                  boxShadow: tokens.shadow.sm,
                }}
                styles={{ body: { padding: 16 } }}
              >
                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "space-between",
                    marginBottom: 8,
                  }}
                >
                  <Space size={8}>
                    <CalendarOutlined style={{ color: tokens.color.brand.primary }} />
                    <span style={{ fontWeight: 600 }}>今日待入住</span>
                  </Space>
                  <Link
                    href="/orders"
                    style={{
                      fontSize: 12,
                      color: tokens.color.brand.primary,
                      display: "inline-flex",
                      alignItems: "center",
                      gap: 2,
                    }}
                  >
                    全部 <ArrowRightOutlined style={{ fontSize: 10 }} />
                  </Link>
                </div>
                {todayCheckinOrders && todayCheckinOrders.length > 0 ? (
                  <div style={{ display: "flex", flexDirection: "column" }}>
                    {todayCheckinOrders.slice(0, 5).map((o: any) => (
                      <Link
                        key={o.order_id}
                        href={`/orders?order_id=${o.order_id}`}
                        style={{
                          display: "flex",
                          alignItems: "center",
                          gap: 10,
                          padding: "8px 0",
                          borderBottom: `1px solid ${tokens.color.bg.borderSubtle}`,
                          color: tokens.color.text.primary,
                        }}
                      >
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
                          {o.guest_name?.charAt(0) ?? "?"}
                        </span>
                        <div style={{ flex: 1, minWidth: 0 }}>
                          <div style={{ fontSize: 13, fontWeight: 500, lineHeight: 1.3 }}>
                            {o.guest_name}
                            <span style={{ color: tokens.color.text.tertiary, marginLeft: 6, fontWeight: 400 }}>
                              {o.room_id ?? "待排房"}
                            </span>
                          </div>
                          <div style={{ fontSize: 11, color: tokens.color.text.tertiary, marginTop: 1 }}>
                            {o.order_id}
                          </div>
                        </div>
                        <StatusBadge status={o.order_status} size="sm" />
                      </Link>
                    ))}
                  </div>
                ) : (
                  <div style={{ padding: "12px 0", fontSize: 13, color: tokens.color.text.tertiary }}>
                    今天没有待入住订单
                  </div>
                )}
              </Card>
            </Col>
            <Col xs={24}>
              <Card
                bordered={false}
                style={{
                  borderRadius: tokens.radius.lg,
                  border: `1px solid ${tokens.color.bg.border}`,
                  boxShadow: tokens.shadow.sm,
                }}
                styles={{ body: { padding: 16 } }}
              >
                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "space-between",
                    marginBottom: 8,
                  }}
                >
                  <Space size={8}>
                    <CheckSquareOutlined style={{ color: tokens.color.status.warn }} />
                    <span style={{ fontWeight: 600 }}>逾期任务</span>
                  </Space>
                  <Link
                    href="/tasks"
                    style={{
                      fontSize: 12,
                      color: tokens.color.brand.primary,
                      display: "inline-flex",
                      alignItems: "center",
                      gap: 2,
                    }}
                  >
                    全部 <ArrowRightOutlined style={{ fontSize: 10 }} />
                  </Link>
                </div>
                {overdueTasks && overdueTasks.length > 0 ? (
                  <div style={{ display: "flex", flexDirection: "column" }}>
                    {overdueTasks.map((t: any) => (
                      <Link
                        key={t.task_id}
                        href="/tasks"
                        style={{
                          display: "flex",
                          alignItems: "center",
                          gap: 10,
                          padding: "8px 0",
                          borderBottom: `1px solid ${tokens.color.bg.borderSubtle}`,
                          color: tokens.color.text.primary,
                        }}
                      >
                        <span
                          style={{
                            width: 6,
                            height: 6,
                            borderRadius: "50%",
                            background: tokens.color.status.warn,
                            flex: "0 0 6px",
                          }}
                        />
                        <div style={{ flex: 1, minWidth: 0 }}>
                          <div
                            style={{
                              fontSize: 13,
                              fontWeight: 500,
                              lineHeight: 1.3,
                              whiteSpace: "nowrap",
                              overflow: "hidden",
                              textOverflow: "ellipsis",
                            }}
                          >
                            {t.title ?? t.task_type}
                          </div>
                          <div style={{ fontSize: 11, color: tokens.color.text.tertiary, marginTop: 1 }}>
                            {t.room_id ?? t.order_id ?? "全局任务"}
                          </div>
                        </div>
                        <StatusBadge status={t.task_status ?? "pending"} size="sm" />
                      </Link>
                    ))}
                  </div>
                ) : (
                  <div style={{ padding: "12px 0", fontSize: 13, color: tokens.color.text.tertiary }}>
                    没有逾期任务 🎉
                  </div>
                )}
              </Card>
            </Col>
          </Row>
        </Col>
      </Row>

      <KpiPeekDrawer
        preset={peekKey ? peekPresets[peekKey] : null}
        open={!!peekKey}
        onClose={() => setPeekKey(null)}
        rooms={todayData?.cleaning_list}
      />
    </div>
  );
}
