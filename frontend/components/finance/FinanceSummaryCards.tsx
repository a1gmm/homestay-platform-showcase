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
    value == null || !Number.isFinite(value)
      ? "—"
      : value.toLocaleString("zh-CN", { maximumFractionDigits: 2 });
  const hasCostBasis = monthly.recorded_operating_costs !== undefined;

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
          title={hasCostBasis ? "已登记运营成本" : "运营支出"}
          value={money(hasCostBasis ? monthly.recorded_operating_costs : monthly.total_expenses)}
          prefix="¥"
          icon={<FallOutlined />}
          tone="warn"
          footer={hasCostBasis ? `标准服务费 ¥${money(monthly.standard_service_fees)} 另列；成本含公司垫付的业主费用${monthly.cost_payment_unconfirmed_count ? `；${monthly.cost_payment_unconfirmed_count} 笔付款方待核实` : ""}` : undefined}
        />
      </Col>
      <Col xs={12} sm={8} md={8}>
        <StatCard
          title={hasCostBasis ? "扣已登记成本后余额" : "扣运营支出后余额"}
          value={money(hasCostBasis ? monthly.balance_after_recorded_costs : monthly.gross_profit)}
          prefix="¥"
          icon={<DollarOutlined />}
          tone="success"
          footer={hasCostBasis ? "按业务发生日归集，未扣业主应付；此余额不代表公司净利润" : "净收入减运营支出，未扣业主应付"}
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
