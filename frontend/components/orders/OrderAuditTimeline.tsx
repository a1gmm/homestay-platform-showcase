"use client";

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { Empty, Spin, Timeline, Button } from "antd";
import { ArrowRightOutlined } from "@ant-design/icons";
import dayjs from "dayjs";
import Link from "next/link";
import { auditApi, usersApi } from "@/lib/api";
import { tokens } from "@/lib/design-tokens";
import { CHANNEL_LABELS } from "@/lib/channels";

const ORDER_ACTION_LABELS: Record<string, string> = {
  "order.create": "新增预定",
  "order.update": "修改订单",
  "order.update_after_completed": "修改已完成订单",
  "order.update_daily_price": "修改单日房价",
  "order.transition": "状态流转",
  "order.cancel": "取消订单",
  "order.delete": "删除订单",
  "order.assign_room": "排房 / 换房",
  "deposit.collect": "收押金",
  "deposit.return": "退押金",
  "deposit.withhold": "扣押金",
  "payment.create": "记录收款",
  "refund.create": "退款",
  "staff.handle_checkout": "办理退房",
  "staff.revert_checkout": "撤销退房",
};

const BOOKING_TYPE_LABEL: Record<string, string> = {
  normal: "普通订单",
  trial: "试住单",
  owner_self: "自住单",
};

const DEPOSIT_STATUS_LABEL: Record<string, string> = {
  not_collected: "未收",
  collected: "已收",
  returned: "已退",
  withheld: "已扣",
};

// _diff 字段名 → 中文（让退押金/排房等局部 diff 更可读）
const DIFF_KEY_LABEL: Record<string, string> = {
  refund_amount: "实退金额",
  withhold_amount: "扣款金额",
  withhold_reason: "扣款原因",
  cleaner_id: "保洁员",
  task_id: "任务",
  reason: "原因",
  date: "日期",
  price: "价格",
};

interface Props {
  orderId: string;
  /** 默认 20，最多展示多少条 */
  limit?: number;
}

export default function OrderAuditTimeline({ orderId, limit = 20 }: Props) {
  const { data: logs, isLoading } = useQuery({
    queryKey: ["audit-logs", "order", orderId],
    queryFn: () =>
      auditApi
        .list({ resource_type: "order", resource_id: orderId, page_size: limit })
        .then((r) => r.data),
    enabled: !!orderId,
    staleTime: 30 * 1000,
  });

  const { data: usersData } = useQuery({
    queryKey: ["users-for-audit"],
    queryFn: () =>
      usersApi
        .list()
        .then((r) => r.data)
        .catch(() => []),
    staleTime: 5 * 60 * 1000,
  });

  const userMap = useMemo(() => {
    const map: Record<string, string> = {};
    if (Array.isArray(usersData)) {
      usersData.forEach((u: any) => {
        map[u.user_id] = u.display_name || u.username || u.user_id;
      });
    }
    return map;
  }, [usersData]);

  if (isLoading) {
    return (
      <div style={{ padding: "8px 0" }}>
        <Spin size="small" />
      </div>
    );
  }

  const list: any[] = Array.isArray(logs) ? logs : [];

  if (list.length === 0) {
    return (
      <Empty
        image={Empty.PRESENTED_IMAGE_SIMPLE}
        description="暂无操作记录"
        style={{ padding: 8, fontSize: 12 }}
      />
    );
  }

  return (
    <div>
      <Timeline
        style={{ marginBottom: -12, marginTop: 4, paddingLeft: 4 }}
        items={list.map((log: any) => {
          const operator = userMap[log.operator_id] || log.operator_id || "系统";
          const label = ORDER_ACTION_LABELS[log.action] || log.action;
          return {
            color: tokens.color.brand.primary,
            children: (
              <AuditEntryCard
                timestamp={log.created_at}
                actionLabel={label}
                operator={operator}
                notes={log.notes}
                before={log.before_data}
                after={log.after_data}
              />
            ),
          };
        })}
      />
      <div style={{ textAlign: "right", marginTop: 8 }}>
        <Link
          href={`/system/audit?resource_type=order&resource_id=${encodeURIComponent(
            orderId
          )}`}
        >
          <Button type="link" size="small" icon={<ArrowRightOutlined />}>
            查看完整审计日志
          </Button>
        </Link>
      </div>
    </div>
  );
}

// ────────────────────────────────────────────────────────────────────────────
// 单条审计卡片：参考王总截图样式
// after_data 含完整订单快照（guest_name + channel + room_id 等）→ 渲染快照卡
// 否则降级为简版 diff（兼容老数据）
// ────────────────────────────────────────────────────────────────────────────

function isSnapshot(d: any): boolean {
  return !!d && typeof d === "object" && typeof d.guest_name === "string" && !!d.order_id;
}

function AuditEntryCard({
  timestamp,
  actionLabel,
  operator,
  notes,
  before,
  after,
}: {
  timestamp: string;
  actionLabel: string;
  operator: string;
  notes?: string | null;
  before?: any;
  after?: any;
}) {
  const snap = isSnapshot(after) ? after : isSnapshot(before) ? before : null;
  const extraDiff = (after && after._diff) || (before && before._diff) || null;

  return (
    <div style={{ fontSize: 13 }}>
      {/* 时间 + 动作 + 操作人 */}
      <div style={{ marginBottom: 6 }}>
        <span style={{ color: tokens.color.text.tertiary, fontSize: 12, marginRight: 8 }}>
          {dayjs(timestamp).format("YYYY-MM-DD HH:mm:ss")}
        </span>
        <span style={{ fontWeight: 600, color: tokens.color.text.primary, marginRight: 8 }}>
          {actionLabel}
        </span>
        <span style={{ color: tokens.color.text.secondary }}>{operator}</span>
      </div>

      {/* 快照卡：完整字段铺出来；老数据没快照就降级为 diff 简版 */}
      {snap ? (
        <SnapshotCard snap={snap} extraDiff={extraDiff} />
      ) : (
        <LegacyDiffCard before={before} after={after} />
      )}

      {notes && (
        <div
          style={{
            marginTop: 6,
            fontSize: 12,
            color: tokens.color.text.tertiary,
            paddingLeft: 4,
          }}
        >
          {notes}
        </div>
      )}
    </div>
  );
}

function SnapshotCard({ snap, extraDiff }: { snap: any; extraDiff: any }) {
  const channelLabel = snap.channel ? CHANNEL_LABELS[snap.channel] || snap.channel : "—";
  const bookingTypeLabel = BOOKING_TYPE_LABEL[snap.booking_type || "normal"] || "普通订单";
  const ci = snap.check_in_date;
  const co = snap.check_out_date;
  const nights = snap.nights ?? 0;
  const dateRange = ci && co ? `${formatCNDate(ci)}~${formatCNDate(co)}` : "—";
  const rooms = Array.isArray(snap.rooms) && snap.rooms.length > 0
    ? snap.rooms.map((r: any) => r.room_id || "待排房").join("、")
    : (snap.room_id || "待排房");

  // 截图样式 1:1 复刻：
  // - 单条虚线边框卡，灰底
  // - 顶部 4 字段同行（渠道 / 订单类型 / 预订人 / 手机号）
  // - 中段「房间 / 入住时间 / 房费」一行一字段
  // - 下方「备注」独立一行
  // - 三段之间用纯空白（margin-top）撑开，不画分隔线
  return (
    <div
      style={{
        border: `1px dashed ${tokens.color.bg.border}`,
        borderRadius: 8,
        padding: "12px 14px",
        background: tokens.color.bg.subtle,
        fontSize: 12,
        lineHeight: 1.65,
      }}
    >
      {/* 第 1 段：4 字段一行 */}
      <div style={{ display: "flex", flexWrap: "wrap", gap: "0 24px" }}>
        <Field label="渠道">{channelLabel}</Field>
        <Field label="订单类型">{bookingTypeLabel}</Field>
        <Field label="预订人">{snap.guest_name || "—"}</Field>
        <Field label="手机号">{snap.guest_phone || "—"}</Field>
      </div>

      {/* 第 2 段：房间 / 入住时间 / 房费 */}
      <div style={{ marginTop: 10 }}>
        <div>
          <FieldLabel>房间：</FieldLabel>
          <span style={{ color: tokens.color.text.primary, fontWeight: 500 }}>{rooms}</span>
        </div>
        <div>
          <FieldLabel>入住时间：</FieldLabel>
          <span style={{ color: tokens.color.text.primary }}>
            {dateRange}
            {nights > 0 && (
              <span style={{ color: tokens.color.text.primary }}>, 共{nights}晚</span>
            )}
          </span>
        </div>
        <div>
          <FieldLabel>房费：</FieldLabel>
          <span className="tabular" style={{ color: tokens.color.text.primary, fontWeight: 500 }}>
            {snap.actual_price != null ? `¥${Number(snap.actual_price).toLocaleString()}` : "—"}
          </span>
          {snap.deposit != null && Number(snap.deposit) > 0 && (
            <span style={{ marginLeft: 12, color: tokens.color.text.tertiary, fontSize: 11 }}>
              押金 ¥{Number(snap.deposit).toLocaleString()}
              {snap.deposit_status && ` · ${DEPOSIT_STATUS_LABEL[snap.deposit_status] || snap.deposit_status}`}
            </span>
          )}
        </div>
      </div>

      {/* 第 3 段：备注 */}
      {snap.notes && (
        <div style={{ marginTop: 10 }}>
          <FieldLabel>备注：</FieldLabel>
          <span style={{ color: tokens.color.text.primary, whiteSpace: "pre-wrap" }}>{snap.notes}</span>
        </div>
      )}

      {/* 第 4 段：局部 diff（单日改价 / 排房 / 撤销退房 等带 _diff 字段时显示） */}
      {extraDiff && Object.keys(extraDiff).length > 0 && (
        <div
          style={{
            marginTop: 10,
            fontSize: 11,
            color: tokens.color.text.secondary,
            fontFamily: tokens.font?.mono || "monospace",
          }}
        >
          {Object.entries(extraDiff)
            .filter(([, v]) => v !== null && v !== undefined)
            .map(([k, v]) => (
              <div key={k}>· {DIFF_KEY_LABEL[k] || k}: {formatDiffVal(v)}</div>
            ))}
        </div>
      )}
    </div>
  );
}

function LegacyDiffCard({ before, after }: { before: any; after: any }) {
  // 兼容老数据：没有 snapshot 时退化为 field diff 列表
  const changes = useMemo(() => {
    if (!before && !after) return [];
    if (typeof before !== "object" && typeof after !== "object") return [];
    const result: string[] = [];
    const keys = Array.from(
      new Set([...Object.keys(before || {}), ...Object.keys(after || {})])
    ).filter((k) => k !== "_diff");
    for (const k of keys) {
      const b = JSON.stringify(before?.[k]);
      const a = JSON.stringify(after?.[k]);
      if (b !== a) {
        const bs = String(before?.[k] ?? "—").slice(0, 40);
        const as = String(after?.[k] ?? "—").slice(0, 40);
        result.push(`${k}: ${bs} → ${as}`);
      }
    }
    return result;
  }, [before, after]);

  if (changes.length === 0) return null;

  return (
    <div
      style={{
        marginTop: 4,
        fontSize: 11,
        color: tokens.color.text.secondary,
        fontFamily: tokens.font?.mono || "monospace",
        border: `1px dashed ${tokens.color.bg.border}`,
        borderRadius: 8,
        padding: "8px 12px",
        background: tokens.color.bg.subtle,
      }}
    >
      {changes.slice(0, 6).map((c, i) => (
        <div key={i}>· {c}</div>
      ))}
      {changes.length > 6 && (
        <div style={{ color: tokens.color.text.tertiary }}>
          · 还有 {changes.length - 6} 处变更
        </div>
      )}
    </div>
  );
}

// ─── Atoms ───────────────────────────────────────────────────────────────────

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <span>
      <FieldLabel>{label}：</FieldLabel>
      <span style={{ color: tokens.color.text.primary }}>{children}</span>
    </span>
  );
}

function FieldLabel({ children }: { children: React.ReactNode }) {
  return <span style={{ color: tokens.color.text.tertiary }}>{children}</span>;
}

function formatCNDate(d: string): string {
  // "2026-05-27" → "2026年05月27日"
  const m = d.match(/^(\d{4})-(\d{2})-(\d{2})/);
  return m ? `${m[1]}年${m[2]}月${m[3]}日` : d;
}

function formatDiffVal(v: any): string {
  if (v == null) return "—";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}
