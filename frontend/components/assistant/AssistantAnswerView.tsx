"use client";

import { Alert, Card, Collapse, Descriptions, Empty, Space, Table, Tag, Timeline } from "antd";
import type { AssistantAnswer, AssistantTimelineRow } from "@/lib/api";
import { getChannelLabel } from "@/lib/channels";

/** 操作者类型 → 标签颜色（人=绿、系统=蓝灰、飞书保洁=橙、未知=红）。 */
const ACTOR_COLOR: Record<string, string> = {
  human: "green",
  "system-auto": "geekblue",
  "cleaner-via-feishu": "orange",
  unknown: "red",
};
const ACTOR_TEXT: Record<string, string> = {
  human: "人工",
  "system-auto": "系统自动",
  "cleaner-via-feishu": "保洁(飞书)",
  unknown: "未知",
};

function TimelineDraft({ rows }: { rows: AssistantTimelineRow[] }) {
  if (!rows.length) return <Empty description="这一单没有操作记录" />;
  return (
    <Timeline
      items={rows.map((r) => ({
        color: ACTOR_COLOR[r.actor_kind] ?? "gray",
        children: (
          <div>
            <Space size={6} wrap>
              <span style={{ color: "var(--anyu-text-secondary, #888)", fontSize: 13 }}>{r.time}</span>
              <Tag color={ACTOR_COLOR[r.actor_kind] ?? "default"}>
                {r.actor_label}（{ACTOR_TEXT[r.actor_kind] ?? r.actor_kind}）
              </Tag>
              <strong>{r.verb}</strong>
            </Space>
            {r.changes.length > 0 && (
              <div style={{ marginTop: 2, fontSize: 13 }}>{r.changes.join("，")}</div>
            )}
            {r.partial_snapshot && (
              <div style={{ marginTop: 2, fontSize: 12, color: "#c67c00" }}>{r.hedge}</div>
            )}
          </div>
        ),
      }))}
    />
  );
}

function money(value: unknown) {
  const amount = Number(value ?? 0);
  return Number.isFinite(amount)
    ? `¥${amount.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
    : "—";
}

function MetricsDetails({ data }: { data: Record<string, unknown> }) {
  const monthly = Array.isArray(data.monthly_breakdown)
    ? (data.monthly_breakdown as Array<Record<string, unknown>>)
    : [];
  const channels = data.by_channel && typeof data.by_channel === "object"
    ? Object.entries(data.by_channel as Record<string, Record<string, unknown>>).map(([channel, row]) => ({ channel, ...row }))
    : [];
  const cleaners = Array.isArray(data.per_cleaner) ? data.per_cleaner as Array<Record<string, unknown>> : [];
  const rooms = Array.isArray(data.rooms) ? data.rooms as Array<Record<string, unknown>> : [];
  const owners = Array.isArray(data.owners) ? data.owners as Array<Record<string, unknown>> : [];
  const hasTotals = ["period_label", "order_count", "revenue", "commission", "net_revenue", "room_nights"]
    .some((key) => data[key] !== undefined);

  return (
    <Card size="small" title="经营数据" data-testid="metrics-data">
      {hasTotals && (
        <Descriptions size="small" column={{ xs: 2, sm: 3 }}>
          {data.period_label !== undefined && <Descriptions.Item label="统计周期">{String(data.period_label)}</Descriptions.Item>}
          {data.order_count !== undefined && <Descriptions.Item label="订单数">{String(data.order_count)} 单</Descriptions.Item>}
          {data.revenue !== undefined && <Descriptions.Item label="营业额">{money(data.revenue)}</Descriptions.Item>}
          {data.commission !== undefined && <Descriptions.Item label="平台佣金">{money(data.commission)}</Descriptions.Item>}
          {data.net_revenue !== undefined && <Descriptions.Item label="净收入">{money(data.net_revenue)}</Descriptions.Item>}
          {data.room_nights !== undefined && <Descriptions.Item label="间夜数">{String(data.room_nights)} 间夜</Descriptions.Item>}
        </Descriptions>
      )}

      {owners.length > 0 && <div style={{ marginTop: 10 }}>
        <strong>房东收入明细</strong>
        <Table
          size="small"
          pagination={false}
          rowKey={(row) => String(row.owner_id)}
          dataSource={owners}
          columns={[
            { title: "房东", dataIndex: "owner_name" },
            { title: "分成金额", dataIndex: "owner_amount", render: money },
            { title: "扣除支出", dataIndex: "deducted_expenses", render: money },
            { title: "实际到手", dataIndex: "actual_owner_amount", render: money },
            { title: "结算状态", dataIndex: "status_counts", render: (value: Record<string, number> = {}) => {
              const labels: Record<string, string> = { paid: "已付", confirmed: "已确认", pending: "待确认", disputed: "有争议" };
              return Object.entries(value).map(([key, count]) => `${labels[key] ?? key} ${count}`).join("，") || "—";
            } },
          ]}
        />
      </div>}

      {(monthly.length > 0 || channels.length > 0 || cleaners.length > 0 || rooms.length > 0) && (
        <Collapse
          ghost
          style={{ marginTop: 10 }}
          items={[{
            key: "details",
            label: "查看分类明细",
            children: (
              <Space direction="vertical" size={16} style={{ width: "100%" }}>
                {monthly.length > 0 && <div>
                  <strong>按月明细</strong>
                  <Table size="small" pagination={false} rowKey={(r) => String(r.month ?? r.period_label)} dataSource={monthly}
                    columns={[
                      { title: "月份", dataIndex: "period_label" },
                      { title: "订单", dataIndex: "order_count", render: (v) => `${v} 单` },
                      { title: "营业额", dataIndex: "revenue", render: money },
                      { title: "净收入", dataIndex: "net_revenue", render: money },
                    ]} />
                </div>}
                {channels.length > 0 && <div>
                  <strong>渠道明细</strong>
                  <Table size="small" pagination={false} rowKey="channel" dataSource={channels}
                    columns={[
                      { title: "渠道", dataIndex: "channel", render: (v) => getChannelLabel(String(v)) },
                      { title: "订单", dataIndex: "order_count", render: (v) => `${v} 单` },
                      { title: "营业额", dataIndex: "revenue", render: money },
                    ]} />
                </div>}
                {cleaners.length > 0 && <Table size="small" pagination={false} rowKey="cleaner" dataSource={cleaners}
                  columns={[{ title: "保洁人员", dataIndex: "cleaner" }, { title: "完成房间", dataIndex: "rooms_cleaned", render: (v) => `${v} 间` }]} />}
                {rooms.length > 0 && <Table size="small" pagination={{ pageSize: 10 }} rowKey={(r) => String(r.room_id)} dataSource={rooms}
                  columns={[{ title: "房间", dataIndex: "room_name" }, { title: "订单", dataIndex: "order_count" }, { title: "营业额", dataIndex: "revenue", render: money }]} />}
              </Space>
            ),
          }]}
        />
      )}
    </Card>
  );
}

export default function AssistantAnswerView({ answer }: { answer: AssistantAnswer }) {
  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      {/* 人话总结 */}
      <Card size="small">
        <div style={{ whiteSpace: "pre-wrap", fontSize: 15, lineHeight: 1.6 }}>{answer.summary}</div>
        {answer.narration_degraded && (
          <div style={{ marginTop: 6, fontSize: 12, color: "#c67c00" }}>
            （AI 措辞暂不可用，以上为系统直出结果）
          </div>
        )}
      </Card>

      {/* OTA 盲区等强制声明 */}
      {answer.disclaimers.map((d, i) => (
        <Alert key={i} type="warning" showIcon message={d} />
      ))}

      {/* 追溯：并排展示原始操作时间线（亮底稿，一眼可核） */}
      {answer.kind === "forensics" && answer.orders && (
        <Space direction="vertical" size={12} style={{ width: "100%" }}>
          {answer.orders.map((o: Record<string, unknown>, idx) => (
            <Card
              key={idx}
              size="small"
              title={`订单 ${String(o.order_id ?? "")}　${String(o.guest_name ?? "")}　${String(
                o.channel ?? "",
              )}`}
              data-testid="forensics-order"
            >
              <TimelineDraft rows={(o.timeline_rows as AssistantTimelineRow[]) ?? []} />
            </Card>
          ))}
        </Space>
      )}

      {/* 追溯候选（多单需老板确认是哪一单） */}
      {answer.candidates && answer.candidates.length > 0 && (
        <Card size="small" title="符合的订单（请确认是哪一单）">
          <Table
            size="small"
            rowKey={(r: Record<string, unknown>) => String(r.order_id)}
            pagination={false}
            dataSource={answer.candidates}
            columns={[
              { title: "订单号", dataIndex: "order_id" },
              { title: "客人", dataIndex: "guest_name" },
              { title: "房间", dataIndex: "room_id" },
              { title: "入住", dataIndex: "check_in_date" },
              { title: "离店", dataIndex: "check_out_date" },
            ]}
          />
        </Card>
      )}

      {/* 查数：结构化数据 */}
      {answer.kind === "metrics" && answer.data && (
        <MetricsDetails data={answer.data} />
      )}
    </Space>
  );
}
