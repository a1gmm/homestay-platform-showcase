"use client";

import { useEffect, useRef, useState } from "react";
import dayjs from "dayjs";
import { Alert, Button, Drawer, Empty, Skeleton, message } from "antd";
import { useRouter } from "next/navigation";
import { MonthlyCloseWorkspace } from "@/components/monthly-close/MonthlyCloseWorkspace";
import { useMonthlyClose } from "@/hooks/useMonthlyClose";
import { extractErrorMessage } from "@/lib/api-errors";
import { resolveMonthlyCloseAction, type MonthlyCloseActionDestination } from "@/lib/monthly-close-actions";
import { staffMonthlyCloseApi } from "@/lib/staff-api";
import { useStaffStore } from "@/lib/staff-store";

const MONTH_PATTERN = /^\d{4}-(0[1-9]|1[0-2])$/;

function initialMonthSelection() {
  const currentMonth = dayjs().format("YYYY-MM");
  if (typeof window === "undefined") return { month: currentMonth, canFallback: true };
  const requested = new URLSearchParams(window.location.search).get("month");
  return requested && MONTH_PATTERN.test(requested)
    ? { month: requested, canFallback: false }
    : { month: currentMonth, canFallback: true };
}

function isCycleNotFound(error: unknown) {
  const response = (error as { response?: { status?: number; data?: { detail?: { code?: string } } } } | null)?.response;
  return response?.status === 404 && response.data?.detail?.code === "monthly_close_not_found";
}

function errorStatus(error: unknown) {
  return (error as { response?: { status?: number } } | null)?.response?.status;
}

export default function StaffMonthlyClosePage() {
  const router = useRouter();
  const user = useStaffStore((state) => state.user);
  const token = useStaffStore((state) => state.access_token);
  const sessionId = useStaffStore((state) => state.session_id);
  const [authHydrated, setAuthHydrated] = useState(false);
  const [monthSelection, setMonthSelection] = useState(initialMonthSelection);
  const month = monthSelection.month;
  const fallbackAttempted = useRef(false);
  const [uploadSource, setUploadSource] = useState<string | null>(null);
  const [detail, setDetail] = useState<MonthlyCloseActionDestination | null>(null);
  const input = useRef<HTMLInputElement>(null);
  const allowed = user?.role === "cleaner" || user?.role === "keeper";
  const close = useMonthlyClose(month, authHydrated && !!token && allowed, false, false, user && sessionId ? {
    channel: "staff",
    sessionId,
    userId: user.user_id,
    role: user.role,
  } : undefined, staffMonthlyCloseApi);
  const projection = close.visibleProjection?.billing_month === month ? close.visibleProjection : null;

  useEffect(() => {
    const persistApi = useStaffStore.persist;
    if (!persistApi) {
      setAuthHydrated(true);
      return;
    }
    const unsubscribe = persistApi.onFinishHydration(() => setAuthHydrated(true));
    if (persistApi.hasHydrated()) setAuthHydrated(true);
    return unsubscribe;
  }, []);

  useEffect(() => {
    if (!authHydrated) return;
    if (!token || !user) router.replace(`/staff/login?next=${encodeURIComponent(`/staff/monthly-close?month=${month}`)}`);
    else if (!allowed) router.replace("/staff");
  }, [allowed, authHydrated, month, router, token, user]);

  useEffect(() => {
    if (!authHydrated || !monthSelection.canFallback || fallbackAttempted.current || !close.projection.isError || !isCycleNotFound(close.projection.error)) return;
    fallbackAttempted.current = true;
    const previousMonth = dayjs(`${month}-01`).subtract(1, "month").format("YYYY-MM");
    setMonthSelection({ month: previousMonth, canFallback: false });
    window.history.replaceState(null, "", `/staff/monthly-close?month=${previousMonth}`);
  }, [authHydrated, close.projection.error, close.projection.isError, month, monthSelection.canFallback]);

  if (!authHydrated || !token || !user || !allowed) return null;

  const primaryAction = () => {
    if (!projection) return;
    const destination = resolveMonthlyCloseAction(projection, projection.recommended_action);
    if (destination.kind === "upload_source") {
      setUploadSource(destination.sourceType);
      input.current?.click();
    } else if (destination.kind === "invalid") message.warning(destination.message);
    else if (destination.kind !== "no_op") setDetail(destination);
  };

  return (
    <main style={{ maxWidth: 1280, margin: "0 auto", padding: "18px 14px 84px" }}>
      <header style={{ display: "flex", gap: 12, alignItems: "center", justifyContent: "space-between", marginBottom: 16 }}>
        <div><h1 style={{ margin: 0, fontSize: 24 }}>员工月结</h1><p style={{ margin: "4px 0 0", color: "var(--stone)" }}>只显示你提交的资料和分配给你的事项。</p></div>
        <input aria-label="月结月份" type="month" value={month} onChange={(event) => {
          const nextMonth = event.target.value;
          if (!MONTH_PATTERN.test(nextMonth)) return;
          fallbackAttempted.current = true;
          setMonthSelection({ month: nextMonth, canFallback: false });
          window.history.replaceState(null, "", `/staff/monthly-close?month=${nextMonth}`);
        }} style={{ minHeight: 44 }} />
      </header>
      {close.projection.isLoading && !projection ? <Skeleton active /> : close.projection.isError ? (
        isCycleNotFound(close.projection.error)
          ? <Empty description="当前月份没有可查看的月结事项"><Button onClick={() => close.projection.refetch()}>重新加载</Button></Empty>
          : <Alert type="error" showIcon message={errorStatus(close.projection.error) === 403 ? "没有权限查看这个月份" : "月结资料加载失败"} description="请重新登录或稍后再试；系统不会把加载失败当作空月份。" action={<Button onClick={() => close.projection.refetch()}>重新加载</Button>} />
      ) : !projection ? (
        <Empty description="当前月份没有可查看的月结事项"><Button onClick={() => close.projection.refetch()}>重新加载</Button></Empty>
      ) : projection.features.assistant_enabled ? (
        <MonthlyCloseWorkspace
          projection={projection}
          scopeKey={`${sessionId ?? "signed-out"}:${projection.cycle_id}:${projection.actor_role}`}
          events={close.events}
          replies={close.replies}
          receipts={close.durableReceipts}
          onPrimaryAction={primaryAction}
          onSendMessage={async (text, attachmentIds, contextRunId) => (await close.sendMessage.mutateAsync({ text, attachmentIds, contextRunId })).data}
          onReceiveInbox={async (file) => (await close.receiveInbox.mutateAsync(file)).data}
          onRetryAnalysis={() => setDetail({ kind: "invalid", message: "请等待管理员复核；需要协助时请联系管理员。" })}
          onConfirmFields={() => setDetail({ kind: "invalid", message: "识别字段由管理员复核，你可以查看自己的资料状态。" })}
          onRequestEvidence={close.loadEvidence}
        />
      ) : (
        <Alert role="alert" type="info" showIcon message="月结助理暂未开放" description="请把本月资料交给管理员，由管理员继续使用手工月结流程。" />
      )}
      <input ref={input} hidden type="file" accept=".xls,.xlsx" onChange={(event) => {
        const file = event.target.files?.[0];
        event.target.value = "";
        const sourceType = uploadSource;
        setUploadSource(null);
        if (!file || !sourceType) return;
        void close.uploadDocument.mutateAsync({ sourceType, file }).then(() => message.success("文件已保存，后台会继续处理")).catch((error) => message.error(extractErrorMessage(error, "文件保存失败")));
      }} />
      <Drawer title="分配给我的月结事项" open={detail != null} onClose={() => setDetail(null)}>
        {detail?.kind === "invalid" ? <Alert type="info" message={detail.message} />
          : detail ? <Alert type="info" showIcon message="只读详情" description="当前员工入口不会执行管理员操作；请联系管理员继续处理。" /> : null}
      </Drawer>
    </main>
  );
}
