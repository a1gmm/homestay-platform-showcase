"use client";

import {
  Alert,
  Button,
  Card,
  Drawer,
  Modal,
  Spin,
  Tag,
  message,
} from "antd";
import dayjs from "dayjs";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { OrderSyncDiagnosis } from "@/components/sync/OrderSyncDiagnosis";
import { PageHeader } from "@/components/ui/PageHeader";
import {
  useBypmsConflicts,
  useBypmsCycles,
  useBypmsOverview,
  useRequestBypmsRetry,
} from "@/hooks/useBypmsSyncAdmin";
import { useAuthStore } from "@/lib/auth";
import { tokens } from "@/lib/design-tokens";
import { useIsMobile } from "@/lib/responsive";
import type {
  BypmsAdminConflictField,
  BypmsAdminConflictStatus,
  BypmsAdminCycleStatus,
  BypmsAdminSyncConflict,
  BypmsAdminSyncCycle,
  BypmsAdminSyncCycleWithSteps,
  BypmsAdminSyncOverview,
  BypmsAdminSyncStep,
  ManualOverrideField,
} from "@/lib/types";

const CYCLE_STATUS: Record<BypmsAdminCycleStatus, { label: string; color: string }> = {
  running: { label: "运行中", color: "processing" },
  succeeded: { label: "成功", color: "success" },
  partial: { label: "部分完成", color: "warning" },
  failed: { label: "失败", color: "error" },
  skipped: { label: "已跳过", color: "default" },
};

const STEP_STATUS: Record<BypmsAdminSyncStep["status"], { label: string; color: string }> = {
  running: { label: "运行中", color: "processing" },
  succeeded: { label: "成功", color: "success" },
  failed: { label: "失败", color: "error" },
  skipped: { label: "已跳过", color: "default" },
};

const STEP_LABELS: Record<BypmsAdminSyncStep["name"], string> = {
  pull: "拉取宝寓订单",
  price_reconcile: "价格同步",
  name_reconcile: "姓名同步",
  create: "创建订单",
  date_reconcile: "日期同步",
  room_reconcile: "房间同步",
  assign: "排房",
  status_reconcile: "订单状态同步",
  subsidized_scan: "补贴住宿扫描",
  cancel: "取消订单同步",
  room_status_reconcile: "房态同步",
  unknown: "未知步骤",
};

const FIELD_LABELS: Record<BypmsAdminConflictField, string> = {
  guest_name: "客人姓名",
  guest_profile: "客人资料",
  check_in_date: "入住日期",
  check_out_date: "离店日期",
  room_assignment: "房间",
  stay_structure: "住宿结构",
  actual_price: "实收金额",
  daily_prices: "每日价格",
  ota_owner_revenue: "房东收入",
  channel: "渠道",
  note: "备注",
  order_status: "订单状态",
  unknown: "未知字段",
};

const FILTER_FIELDS: Array<{ value: ManualOverrideField; label: string }> = [
  { value: "guest_name", label: "客人姓名" },
  { value: "guest_profile", label: "客人资料" },
  { value: "check_in_date", label: "入住日期" },
  { value: "check_out_date", label: "离店日期" },
  { value: "room_assignment", label: "房间" },
  { value: "stay_structure", label: "住宿结构" },
  { value: "actual_price", label: "实收金额" },
  { value: "daily_prices", label: "每日价格" },
  { value: "ota_owner_revenue", label: "房东收入" },
  { value: "channel", label: "渠道" },
  { value: "note", label: "备注" },
  { value: "order_status", label: "订单状态" },
];

const surfaceStyle = {
  border: `1px solid ${tokens.color.bg.border}`,
  borderRadius: tokens.radius.lg,
  background: "#fff",
} as const;

function formatDateTime(value: string | null): string {
  return value ? dayjs(value).format("YYYY-MM-DD HH:mm:ss") : "—";
}

function formatElapsed(seconds: number | null, suffix = ""): string {
  if (seconds === null) return "暂无数据";
  if (seconds < 60) return `${seconds} 秒${suffix}`;
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  if (minutes < 60) {
    return `${minutes} 分${remainder ? ` ${remainder} 秒` : "钟"}${suffix}`;
  }
  const hours = Math.floor(minutes / 60);
  const remainingMinutes = minutes % 60;
  return `${hours} 小时${remainingMinutes ? ` ${remainingMinutes} 分` : ""}${suffix}`;
}

function formatDuration(milliseconds: number): string {
  if (milliseconds < 1_000) return `${milliseconds} 毫秒`;
  if (milliseconds < 60_000) return `${(milliseconds / 1_000).toFixed(1)} 秒`;
  const seconds = Math.floor(milliseconds / 1_000);
  return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
}

function StructuredValue({ value }: { value: unknown }) {
  const rendered = value === undefined ? "—" : JSON.stringify(value, null, 2) ?? "—";
  return (
    <pre
      style={{
        margin: 0,
        maxWidth: 320,
        overflow: "auto",
        whiteSpace: "pre-wrap",
        overflowWrap: "anywhere",
        fontFamily: tokens.font.mono,
        fontSize: tokens.font.size.xs,
        color: tokens.color.text.secondary,
      }}
    >
      {rendered}
    </pre>
  );
}

type HealthState = {
  title: string;
  description: string;
  tone: "success" | "warning" | "error" | "info";
};

function healthState(overview: BypmsAdminSyncOverview): HealthState {
  if (!overview.available) {
    return {
      title: "等待同步服务升级",
      description: overview.message || "同步服务正在升级，完成后这里会自动显示运行数据。",
      tone: "info",
    };
  }
  if (overview.clock_skew_detected) {
    return {
      title: "同步时间异常",
      description: "同步记录出现超出容差的未来时间，当前健康状态不可信，请检查同步服务和服务器时钟。",
      tone: "error",
    };
  }

  const latest = overview.latest_cycle;
  if (!latest) {
    return {
      title: "同步延迟",
      description: "还没有可确认的完成周期，请先检查同步服务是否已启动。",
      tone: "warning",
    };
  }
  const lastCompletedIsStale =
    overview.last_successful_or_partial_age_seconds === null ||
    overview.last_successful_or_partial_age_seconds > overview.stale_after_seconds;
  const stagingIsStale =
    overview.staging_lag_seconds === null ||
    overview.staging_lag_seconds > overview.stale_after_seconds;
  if (latest.status === "running") {
    const cycleIsStuck = latest.duration_ms > overview.stale_after_seconds * 1_000;
    if (cycleIsStuck || lastCompletedIsStale || stagingIsStale) {
      return {
        title: "同步延迟",
        description: `最新一轮运行时间、完成记录或宝寓水位已超过服务阈值（${formatElapsed(overview.stale_after_seconds)}），同步可能卡住，请检查同步服务。`,
        tone: "warning",
      };
    }
    return {
      title: "同步处理中",
      description: "最新一轮尚未完成，完成前不会把它判断为同步正常。",
      tone: "info",
    };
  }
  if (latest.status === "failed") {
    return {
      title: "同步失败",
      description: "最新一轮执行失败，请查看步骤错误码并确认是否需要请求完整重试。",
      tone: "error",
    };
  }
  if (latest.status === "partial") {
    return {
      title: "同步部分完成",
      description: "最新一轮仅部分成功，请检查失败或跳过的步骤以及待处理差异。",
      tone: "warning",
    };
  }

  const isStale =
    latest.status !== "succeeded" ||
    latest.finished_at === null ||
    lastCompletedIsStale ||
    stagingIsStale;
  if (isStale) {
    return {
      title: "同步延迟",
      description: `最新完成记录或宝寓水位已超过服务阈值（${formatElapsed(overview.stale_after_seconds)}），请先以宝寓后台为准并检查同步服务。`,
      tone: "warning",
    };
  }
  return {
    title: "同步正常",
    description: Object.values(overview.open_conflicts_by_field).some((count) => (count ?? 0) > 0)
      ? "最新一轮已完成，但仍有业务差异需要人工处理；请查看下方待处理差异。"
      : "最新一轮已成功完成，宝寓数据水位在正常时效内。",
    tone: "success",
  };
}

function completedLatestCycle(cycle: BypmsAdminSyncCycle | null): boolean {
  return Boolean(
    cycle?.finished_at && ["succeeded", "partial", "failed"].includes(cycle.status),
  );
}

function SummaryCard({ title, value, detail }: { title: string; value: string; detail: string }) {
  return (
    <Card styles={{ body: { padding: 18, height: "100%" } }} style={{ ...surfaceStyle, height: "100%" }}>
      <div style={{ color: tokens.color.text.tertiary, fontSize: tokens.font.size.sm }}>{title}</div>
      <div
        style={{
          marginTop: 8,
          color: tokens.color.text.primary,
          fontSize: tokens.font.size.xl,
          fontWeight: tokens.font.weight.semibold,
        }}
      >
        {value}
      </div>
      <div style={{ marginTop: 6, color: tokens.color.text.secondary, fontSize: tokens.font.size.xs }}>
        {detail}
      </div>
    </Card>
  );
}

function CycleStatusTag({ status }: { status: BypmsAdminCycleStatus }) {
  const meta = CYCLE_STATUS[status];
  return <Tag color={meta.color}>{meta.label}</Tag>;
}

function StepDrawer({
  cycle,
  isMobile,
  onClose,
}: {
  cycle: BypmsAdminSyncCycleWithSteps | null;
  isMobile: boolean;
  onClose: () => void;
}) {
  return (
    <Drawer
      title={cycle ? `周期步骤 · ${cycle.cycle_id}` : "周期步骤"}
      open={Boolean(cycle)}
      onClose={onClose}
      placement={isMobile ? "bottom" : "right"}
      height={isMobile ? "88vh" : undefined}
      width={isMobile ? undefined : 560}
      closable={false}
      extra={
        <Button
          aria-label="关闭步骤详情"
          onClick={onClose}
          style={{ minHeight: isMobile ? tokens.layout.touchTarget : undefined }}
        >
          关闭
        </Button>
      }
    >
      {cycle?.steps.length ? (
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          {cycle.steps.map((step, index) => {
            const meta = STEP_STATUS[step.status];
            return (
              <section
                key={step.step_id}
                style={{ ...surfaceStyle, padding: 16 }}
                aria-label={`第 ${index + 1} 步 ${STEP_LABELS[step.name]}`}
              >
                <div style={{ display: "flex", justifyContent: "space-between", gap: 12 }}>
                  <strong style={{ color: tokens.color.text.primary }}>
                    {index + 1}. {STEP_LABELS[step.name]}
                  </strong>
                  <Tag color={meta.color}>{meta.label}</Tag>
                </div>
                <div style={{ marginTop: 8, color: tokens.color.text.secondary }}>
                  耗时：{formatDuration(step.duration_ms)}
                </div>
                <div style={{ marginTop: 10 }}>
                  <div style={{ color: tokens.color.text.tertiary, marginBottom: 4 }}>允许展示的计数</div>
                  <StructuredValue value={step.summary?.counts ?? {}} />
                </div>
                <div style={{ marginTop: 10, color: tokens.color.text.secondary }}>
                  安全错误码：{step.summary?.error_code || "—"}
                </div>
              </section>
            );
          })}
        </div>
      ) : (
        <div style={{ color: tokens.color.text.secondary }}>该周期没有步骤记录。</div>
      )}
    </Drawer>
  );
}

function CyclesSection({
  data,
  available,
  isLoading,
  isError,
  isMobile,
  onOpen,
}: {
  data: BypmsAdminSyncCycleWithSteps[] | undefined;
  available: boolean | undefined;
  isLoading: boolean;
  isError: boolean;
  isMobile: boolean;
  onOpen: (cycle: BypmsAdminSyncCycleWithSteps) => void;
}) {
  if (isLoading) return <Spin tip="正在读取同步周期…" />;
  if (isError) return <Alert type="error" showIcon message="无法读取同步周期，请稍后重试" />;
  if (available === false) {
    return <div style={{ color: tokens.color.text.secondary }}>同步周期数据等待服务升级</div>;
  }
  if (!data?.length) {
    return <div style={{ color: tokens.color.text.secondary }}>暂无同步周期记录。</div>;
  }

  const action = (cycle: BypmsAdminSyncCycleWithSteps) => (
    <Button
      onClick={() => onOpen(cycle)}
      style={{ minHeight: isMobile ? tokens.layout.touchTarget : undefined }}
    >
      查看步骤
    </Button>
  );

  if (isMobile) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        {data.map((cycle) => (
          <Card
            key={cycle.cycle_id}
            data-testid="mobile-cycle-card"
            style={surfaceStyle}
            styles={{ body: { padding: 16 } }}
          >
            <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
              <strong>{cycle.cycle_id}</strong>
              <CycleStatusTag status={cycle.status} />
            </div>
            <div style={{ marginTop: 8, color: tokens.color.text.secondary }}>
              开始：{formatDateTime(cycle.started_at)}
            </div>
            <div style={{ marginTop: 4, color: tokens.color.text.secondary }}>
              耗时：{formatDuration(cycle.duration_ms)}
            </div>
            <div style={{ marginTop: 12 }}>{action(cycle)}</div>
          </Card>
        ))}
      </div>
    );
  }

  return (
    <div data-testid="desktop-cycle-table" style={{ overflowX: "auto" }}>
      <table style={{ width: "100%", minWidth: 720, borderCollapse: "collapse" }}>
        <thead>
          <tr>
            {['周期', '状态', '开始时间', '耗时', '步骤'].map((title) => (
              <th key={title} scope="col" style={{ padding: 12, textAlign: "left", borderBottom: `1px solid ${tokens.color.bg.border}` }}>{title}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {data.map((cycle) => (
            <tr key={cycle.cycle_id}>
              <td style={{ padding: 12, borderBottom: `1px solid ${tokens.color.bg.border}` }}>{cycle.cycle_id}</td>
              <td style={{ padding: 12, borderBottom: `1px solid ${tokens.color.bg.border}` }}><CycleStatusTag status={cycle.status} /></td>
              <td style={{ padding: 12, borderBottom: `1px solid ${tokens.color.bg.border}` }}>{formatDateTime(cycle.started_at)}</td>
              <td style={{ padding: 12, borderBottom: `1px solid ${tokens.color.bg.border}` }}>{formatDuration(cycle.duration_ms)}</td>
              <td style={{ padding: 12, borderBottom: `1px solid ${tokens.color.bg.border}` }}>{action(cycle)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ConflictContent({ conflict }: { conflict: BypmsAdminSyncConflict }) {
  return (
    <>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
        <Link href={`/orders?keyword=${encodeURIComponent(conflict.source_order_id)}`}>
          {conflict.source_order_id}
        </Link>
        <Tag>{FIELD_LABELS[conflict.field]}</Tag>
      </div>
      <div style={{ marginTop: 8, color: tokens.color.text.secondary }}>
        {conflict.channel} · {conflict.check_in_date} 至 {conflict.check_out_date}
      </div>
      <div style={{ marginTop: 10 }}>
        <div style={{ color: tokens.color.text.tertiary }}>系统当前值</div>
        <StructuredValue value={conflict.local_value} />
      </div>
      <div style={{ marginTop: 10 }}>
        <div style={{ color: tokens.color.text.tertiary }}>宝寓值</div>
        <StructuredValue value={conflict.upstream_value} />
      </div>
    </>
  );
}

function ConflictsSection({
  data,
  isLoading,
  isError,
  isMobile,
}: {
  data: BypmsAdminSyncConflict[] | undefined;
  isLoading: boolean;
  isError: boolean;
  isMobile: boolean;
}) {
  if (isLoading) return <Spin tip="正在读取待处理差异…" />;
  if (isError) return <Alert type="error" showIcon message="无法读取待处理差异，请稍后重试" />;
  if (!data?.length) {
    return <div style={{ color: tokens.color.text.secondary }}>暂无符合条件的待处理差异</div>;
  }
  if (isMobile) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        {data.map((conflict) => (
          <Card
            key={conflict.conflict_id}
            data-testid="mobile-conflict-card"
            style={surfaceStyle}
            styles={{ body: { padding: 16 } }}
          >
            <ConflictContent conflict={conflict} />
          </Card>
        ))}
      </div>
    );
  }

  return (
    <div data-testid="desktop-conflict-table" style={{ overflowX: "auto" }}>
      <table style={{ width: "100%", minWidth: 1080, borderCollapse: "collapse" }}>
        <thead>
          <tr>
            {['宝寓订单', '渠道', '入住区间', '字段', '系统当前值', '宝寓值', '最后发现'].map((title) => (
              <th key={title} scope="col" style={{ padding: 12, textAlign: "left", borderBottom: `1px solid ${tokens.color.bg.border}` }}>{title}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {data.map((conflict) => (
            <tr key={conflict.conflict_id}>
              <td style={{ padding: 12, borderBottom: `1px solid ${tokens.color.bg.border}` }}>
                <Link href={`/orders?keyword=${encodeURIComponent(conflict.source_order_id)}`}>{conflict.source_order_id}</Link>
              </td>
              <td style={{ padding: 12, borderBottom: `1px solid ${tokens.color.bg.border}` }}>{conflict.channel}</td>
              <td style={{ padding: 12, borderBottom: `1px solid ${tokens.color.bg.border}` }}>{conflict.check_in_date} 至 {conflict.check_out_date}</td>
              <td style={{ padding: 12, borderBottom: `1px solid ${tokens.color.bg.border}` }}>{FIELD_LABELS[conflict.field]}</td>
              <td style={{ padding: 12, borderBottom: `1px solid ${tokens.color.bg.border}` }}><StructuredValue value={conflict.local_value} /></td>
              <td style={{ padding: 12, borderBottom: `1px solid ${tokens.color.bg.border}` }}><StructuredValue value={conflict.upstream_value} /></td>
              <td style={{ padding: 12, borderBottom: `1px solid ${tokens.color.bg.border}` }}>{formatDateTime(conflict.last_seen_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function WorkbenchShell({ children }: { children: React.ReactNode }) {
  return (
    <div style={{ maxWidth: 1280, margin: "0 auto" }}>
      <PageHeader
        title="宝寓同步"
        subtitle="查看宝寓同步是否正常、每一步做了什么，并处理需要人工关注的差异。"
      />
      {children}
    </div>
  );
}

function AdminWorkbench() {
  const isMobile = useIsMobile();
  const [field, setField] = useState<ManualOverrideField | undefined>();
  const [status, setStatus] = useState<BypmsAdminConflictStatus>("open");
  const [page, setPage] = useState(1);
  const [selectedCycle, setSelectedCycle] = useState<BypmsAdminSyncCycleWithSteps | null>(null);
  const overviewQuery = useBypmsOverview();
  const cyclesQuery = useBypmsCycles({ limit: 20 });
  const conflictsQuery = useBypmsConflicts({ field, status, page, page_size: 50 });
  const retryMutation = useRequestBypmsRetry();

  const overview = overviewQuery.data;
  const openConflictCount = Object.values(overview?.open_conflicts_by_field ?? {}).reduce(
    (total, count) => total + count,
    0,
  );
  const canRetry = Boolean(
    overview?.available &&
      !overview.clock_skew_detected &&
      overview.retry_available &&
      completedLatestCycle(overview.latest_cycle) &&
      !retryMutation.isPending,
  );
  const touchStyle = { minHeight: isMobile ? tokens.layout.touchTarget : undefined };

  const requestRetry = () => {
    Modal.confirm({
      title: "确认请求完整重试？",
      content: "请求会进入后台队列，完成可能需要一些时间。系统不会记录宝寓账号、密码或登录凭证。",
      okText: "确认排队",
      cancelText: "取消",
      okButtonProps: { style: touchStyle },
      cancelButtonProps: { style: touchStyle },
      onOk: async () => {
        const key = crypto.randomUUID();
        try {
          await retryMutation.mutateAsync(key);
          message.success("完整重试已排队，处理可能需要一些时间");
        } catch (caught) {
          message.error(caught instanceof Error ? caught.message : "宝寓同步重试请求失败");
        }
      },
    });
  };

  if (overviewQuery.isLoading) {
    return (
      <WorkbenchShell>
        <div style={{ ...surfaceStyle, padding: 24, textAlign: "center" }}>
          <Spin />
          <div style={{ marginTop: 12, color: tokens.color.text.secondary }}>正在读取同步状态…</div>
        </div>
      </WorkbenchShell>
    );
  }
  if (overviewQuery.isError || !overview) {
    return (
      <WorkbenchShell>
        <Alert type="error" showIcon message="无法读取宝寓同步状态，请稍后重试" />
      </WorkbenchShell>
    );
  }

  const health = healthState(overview);
  const maxPage = Math.max(1, Math.ceil((conflictsQuery.data?.total ?? 0) / 50));

  return (
    <WorkbenchShell>
      <section
        aria-label="同步健康状态"
        style={{
          ...surfaceStyle,
          padding: isMobile ? 18 : 24,
          borderLeft: `5px solid ${
            health.tone === "success"
              ? tokens.color.status.active
              : health.tone === "error"
                ? "#C2413B"
                : health.tone === "warning"
                  ? tokens.color.status.warn
                  : tokens.color.status.info
          }`,
        }}
      >
        <div style={{ display: "flex", flexWrap: "wrap", justifyContent: "space-between", gap: 16 }}>
          <div>
            <div style={{ color: tokens.color.text.tertiary, fontSize: tokens.font.size.sm }}>当前状态</div>
            <h2 style={{ margin: "6px 0 0", color: tokens.color.text.primary }}>{health.title}</h2>
            <p style={{ margin: "8px 0 0", color: tokens.color.text.secondary }}>{health.description}</p>
          </div>
          <Button
            type="primary"
            disabled={!canRetry}
            loading={retryMutation.isPending}
            onClick={requestRetry}
            style={touchStyle}
          >
            请求完整重试
          </Button>
        </div>
        <div style={{ marginTop: 14, color: tokens.color.text.tertiary, fontSize: tokens.font.size.sm }}>
          重试为异步排队操作，处理可能需要一些时间；系统不会记录宝寓账号、密码或登录凭证。
        </div>
      </section>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: isMobile ? "1fr" : "repeat(4, minmax(0, 1fr))",
          gap: 12,
          marginTop: 16,
        }}
      >
        <SummaryCard
          title="最近完整成功"
          value={formatElapsed(overview.last_fully_successful_age_seconds ?? null, "前")}
          detail={formatDateTime(overview.last_fully_successful_at ?? null)}
        />
        <SummaryCard
          title="水位延迟"
          value={formatElapsed(overview.staging_lag_seconds)}
          detail={`水位：${formatDateTime(overview.staging_watermark)}`}
        />
        <SummaryCard
          title="待处理差异"
          value={`${openConflictCount} 项`}
          detail={
            Object.entries(overview.open_conflicts_by_field)
              .map(([name, count]) => `${FIELD_LABELS[name as BypmsAdminConflictField] || name} ${count}`)
              .join(" · ") || "当前没有待处理字段差异"
          }
        />
        <SummaryCard
          title="排队重试"
          value={`${overview.pending_retry_count} 个`}
          detail="包含等待中和执行中的管理员重试"
        />
      </div>

      <OrderSyncDiagnosis />

      <section style={{ ...surfaceStyle, padding: isMobile ? 16 : 20, marginTop: 16 }}>
        <h2 style={{ margin: "0 0 14px", fontSize: tokens.font.size.xl }}>最近同步周期</h2>
        <CyclesSection
          data={cyclesQuery.data?.items}
          available={cyclesQuery.data?.available}
          isLoading={cyclesQuery.isLoading}
          isError={cyclesQuery.isError}
          isMobile={isMobile}
          onOpen={setSelectedCycle}
        />
      </section>

      <section style={{ ...surfaceStyle, padding: isMobile ? 16 : 20, marginTop: 16 }}>
        <div
          style={{
            display: "flex",
            flexDirection: isMobile ? "column" : "row",
            alignItems: isMobile ? "stretch" : "center",
            justifyContent: "space-between",
            gap: 12,
            marginBottom: 14,
          }}
        >
          <h2 style={{ margin: 0, fontSize: tokens.font.size.xl }}>待处理差异</h2>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
            <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              <span style={{ fontSize: tokens.font.size.xs, color: tokens.color.text.tertiary }}>差异字段</span>
              <select
                aria-label="差异字段"
                value={field ?? ""}
                onChange={(event) => {
                  setField((event.target.value || undefined) as ManualOverrideField | undefined);
                  setPage(1);
                }}
                style={{ ...touchStyle, minWidth: 140, border: `1px solid ${tokens.color.bg.border}`, borderRadius: 8, padding: "0 10px", background: "#fff" }}
              >
                <option value="">全部字段</option>
                {FILTER_FIELDS.map((option) => (
                  <option key={option.value} value={option.value}>{option.label}</option>
                ))}
              </select>
            </label>
            <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              <span style={{ fontSize: tokens.font.size.xs, color: tokens.color.text.tertiary }}>差异状态</span>
              <select
                aria-label="差异状态"
                value={status}
                onChange={(event) => {
                  setStatus(event.target.value as BypmsAdminConflictStatus);
                  setPage(1);
                }}
                style={{ ...touchStyle, minWidth: 120, border: `1px solid ${tokens.color.bg.border}`, borderRadius: 8, padding: "0 10px", background: "#fff" }}
              >
                <option value="open">待处理</option>
                <option value="ignored">已忽略</option>
                <option value="resolved">已解决</option>
              </select>
            </label>
          </div>
        </div>
        <ConflictsSection
          data={conflictsQuery.data?.items}
          isLoading={conflictsQuery.isLoading}
          isError={conflictsQuery.isError}
          isMobile={isMobile}
        />
        <div style={{ marginTop: 14, display: "flex", alignItems: "center", justifyContent: "flex-end", gap: 8 }}>
          <Button
            aria-label="上一页"
            disabled={page <= 1}
            onClick={() => setPage((current) => Math.max(1, current - 1))}
            style={touchStyle}
          >
            上一页
          </Button>
          <span style={{ color: tokens.color.text.secondary }}>第 {page} / {maxPage} 页</span>
          <Button
            aria-label="下一页"
            disabled={page >= maxPage}
            onClick={() => setPage((current) => Math.min(maxPage, current + 1))}
            style={touchStyle}
          >
            下一页
          </Button>
        </div>
      </section>

      <StepDrawer cycle={selectedCycle} isMobile={isMobile} onClose={() => setSelectedCycle(null)} />
    </WorkbenchShell>
  );
}

export default function BypmsSyncPage() {
  const { user } = useAuthStore();
  const router = useRouter();
  const role = user?.role;

  useEffect(() => {
    if (role && role !== "admin") {
      router.replace("/dashboard");
    }
  }, [role, router]);

  if (!user || user.role !== "admin") return null;
  return <AdminWorkbench />;
}
