"use client";

import {
  BarChartOutlined,
  DollarOutlined,
  FallOutlined,
  RiseOutlined,
} from "@ant-design/icons";
import { Col, Row } from "antd";

import { StatCard } from "@/components/ui/StatCard";
import type { DashboardMonthly } from "@/lib/types";

export function FinanceSummaryCards({ monthly }: { monthly: DashboardMonthly }) {
  const money = (value: number | null | undefined) =>
    Number(value ?? 0).toLocaleString("zh-CN");

  return (
    <Row gutter={[16, 16]}>
      <Col xs={12} sm={8} md={8}>
        <StatCard
          title="总房费收入"
          value={money(monthly.total_actual_price)}
          prefix="¥"
          icon={<DollarOutlined />}
          tone="brand"
        />
      </Col>
      <Col xs={12} sm={8} md={8}>
        <StatCard
          title="平台佣金"
          value={money(monthly.total_commission)}
          prefix="¥"
          icon={<FallOutlined />}
          tone="warn"
        />
      </Col>
      <Col xs={12} sm={8} md={8}>
        <StatCard
          title="净收入"
          value={money(monthly.total_net_revenue)}
          prefix="¥"
          icon={<RiseOutlined />}
          tone="success"
        />
      </Col>
      <Col xs={12} sm={8} md={8}>
        <StatCard
          title="运营支出"
          value={money(monthly.total_expenses)}
          prefix="¥"
          icon={<FallOutlined />}
          tone="warn"
        />
      </Col>
      <Col xs={12} sm={8} md={8}>
        <StatCard
          title="扣运营支出后余额"
          value={money(monthly.gross_profit)}
          prefix="¥"
          icon={<DollarOutlined />}
          tone="success"
          footer="净收入减运营支出，未扣业主应付"
        />
      </Col>
      <Col xs={12} sm={8} md={8}>
        <StatCard
          title="订单数"
          value={monthly.order_count ?? 0}
          suffix="单"
          icon={<BarChartOutlined />}
          tone="info"
        />
      </Col>
    </Row>
  );
}
