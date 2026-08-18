"use client";

import { useEffect } from "react";
import { extractErrorMessage } from "@/lib/api-errors";
import { useRouter } from "next/navigation";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Empty, Skeleton, Tag, Button, message, Modal } from "antd";
import dayjs from "dayjs";
import { staffDataApi } from "@/lib/staff-api";
import { useStaffStore } from "@/lib/staff-store";

const TASK_TYPE_LABEL: Record<string, string> = {
  cleaning: "保洁",
  collect_deposit: "收押金",
  checkout_inspection: "退房检查",
  return_deposit: "退押金",
  custom: "其他",
};

const STATUS_COLOR: Record<string, string> = {
  pending: "orange",
  in_progress: "blue",
  pending_review: "gold",
  done: "green",
  cancelled: "default",
};

// 保洁端衍生展示态:提交完工后 status 仍是 in_progress,
// 但应该明确显示"已交付待审核",让保洁不会误以为还要继续操作。
function cleanerDisplayStatus(t: any): { key: string; label: string } {
  if (t.status === "done") return { key: "done", label: "已完成" };
  if (t.review_status === "pending_review") return { key: "pending_review", label: "已交付,待管家查房" };
  if (t.review_status === "rejected") return { key: "in_progress", label: "已打回,请重做" };
  if (t.status === "in_progress") return { key: "in_progress", label: "正在打扫" };
  if (t.status === "pending") return { key: "pending", label: "待打扫" };
  if (t.status === "cancelled") return { key: "cancelled", label: "已取消" };
  return { key: t.status, label: t.status };
}

const PRIORITY_COLOR: Record<string, string> = {
  low: "default",
  medium: "blue",
  high: "orange",
  urgent: "red",
};

export default function CleanerHomePage() {
  const router = useRouter();
  const qc = useQueryClient();
  const isLoggedIn = useStaffStore((s) => s.isLoggedIn());
  const user = useStaffStore((s) => s.user);

  useEffect(() => {
    if (!isLoggedIn) {
      router.replace(`/staff/login?next=${encodeURIComponent("/staff/cleaner")}`);
    }
  }, [isLoggedIn, router]);

  const { data: tasks, isLoading } = useQuery({
    queryKey: ["staff", "tasks"],
    queryFn: async () => (await staffDataApi.tasks()).data,
    enabled: isLoggedIn,
  });

  const startMutation = useMutation({
    mutationFn: (taskId: string) => staffDataApi.updateTaskStatus(taskId, "in_progress"),
    onSuccess: (_res, taskId) => {
      message.success("已开始打扫");
      // 先本地翻状态给即时反馈，invalidate 拉真相兜底（弱网下不用等 refetch 才见变化）
      qc.setQueryData(["staff", "tasks"], (old: any) =>
        Array.isArray(old)
          ? old.map((t: any) => (t.task_id === taskId ? { ...t, status: "in_progress" } : t))
          : old,
      );
      qc.invalidateQueries({ queryKey: ["staff", "tasks"] });
      qc.invalidateQueries({ queryKey: ["staff", "calendar"] });
      qc.invalidateQueries({ queryKey: ["rooms"] });
    },
  });

  const submitMutation = useMutation({
    mutationFn: (taskId: string) => staffDataApi.submitTask(taskId),
    onSuccess: (_res, taskId) => {
      message.success("已提交,管家会去查房");
      qc.setQueryData(["staff", "tasks"], (old: any) =>
        Array.isArray(old)
          ? old.map((t: any) =>
              t.task_id === taskId
                ? { ...t, submitted_at: new Date().toISOString(), review_status: "pending_review" }
                : t,
            )
          : old,
      );
      qc.invalidateQueries({ queryKey: ["staff", "tasks"] });
      qc.invalidateQueries({ queryKey: ["staff", "calendar"] });
      qc.invalidateQueries({ queryKey: ["rooms"] });
    },
    onError: (e: any) => message.error(extractErrorMessage(e, "提交失败")),
  });

  if (!isLoggedIn) return null;

  // 顶部统计沿用 pending/doing/done 三态,但 doing 区间里把"已交付待审核"也算
  // 在"进行中"——保洁视角这些任务仍在他名下未真正结束。
  const pending = (tasks || []).filter((t) => t.status === "pending");
  const doing = (tasks || []).filter((t) => t.status === "in_progress");
  const done = (tasks || []).filter((t) => t.status === "done");

  // 列表排序：被打回的最上(要立刻整改) → 待打扫按截止时间近的在前 → 进行中 → 已完成沉底。
  // 此前按后端返回顺序渲染，最急的活可能埋在中间。
  const urgency = (t: (typeof pending)[number]) => {
    if (t.review_status === "rejected") return 0;
    if (t.status === "pending") return 1;
    if (t.status === "in_progress") return 2;
    return 3;
  };
  const sortedTasks = [...(tasks || [])].sort((a, b) => {
    if (urgency(a) !== urgency(b)) return urgency(a) - urgency(b);
    const ad = a.deadline ? new Date(a.deadline).getTime() : Number.MAX_SAFE_INTEGER;
    const bd = b.deadline ? new Date(b.deadline).getTime() : Number.MAX_SAFE_INTEGER;
    return ad - bd;
  });

  return (
    <div>
      <div
        style={{
          padding: "32px 20px 24px",
          background: "#2B2721",
          color: "#FBF8F1",
        }}
      >
        <div style={{ fontSize: 12, color: "#A89680", marginBottom: 6 }}>
          欢迎,{user?.display_name || "保洁"}
        </div>
        <div className="serif" style={{ fontSize: 22, fontWeight: 400, letterSpacing: 0 }}>
          今日任务
        </div>
        <div style={{ display: "flex", gap: 28, marginTop: 16 }}>
          <Stat label="待打扫" value={pending.length} />
          <Stat label="进行中" value={doing.length} />
          <Stat label="已完成" value={done.length} />
        </div>
      </div>

      <div style={{ padding: 12 }}>
        {isLoading ? (
          <Skeleton active />
        ) : !tasks || tasks.length === 0 ? (
          <Empty description="暂无任务" style={{ marginTop: 60 }} />
        ) : (
          sortedTasks.map((t) => (
            <div
              key={t.task_id}
              style={{
                background: "#F5F1EA",
                border: "0.5px solid #E5DDCB",
                borderRadius: 12,
                padding: 14,
                marginBottom: 10,
              }}
            >
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
                <div style={{ flex: 1 }}>
                  <div style={{ display: "flex", gap: 6, marginBottom: 6 }}>
                    <Tag color="blue" style={{ margin: 0 }}>
                      {TASK_TYPE_LABEL[t.task_type] || t.task_type}
                    </Tag>
                    <Tag color={PRIORITY_COLOR[t.priority]} style={{ margin: 0 }}>
                      {t.priority === "urgent" ? "紧急" : t.priority === "high" ? "高" : t.priority === "medium" ? "中" : "低"}
                    </Tag>
                    {(() => {
                      const ds = cleanerDisplayStatus(t);
                      return (
                        <Tag color={STATUS_COLOR[ds.key]} style={{ margin: 0 }}>
                          {ds.label}
                        </Tag>
                      );
                    })()}
                  </div>
                  <div style={{ fontSize: 15, fontWeight: 600 }}>{t.title}</div>
                  <div style={{ fontSize: 12, color: "#888", marginTop: 4 }}>
                    {t.room_id ? `房间 ${t.room_id}` : ""}
                    {t.deadline && (
                      <>
                        {t.room_id ? " · " : ""}
                        截止 {dayjs(t.deadline).format("M月D日 HH:mm")}
                      </>
                    )}
                  </div>
                  {t.description && (
                    <div style={{ fontSize: 13, color: "#555", marginTop: 6 }}>
                      {t.description}
                    </div>
                  )}
                  {t.review_status === "rejected" && t.rejection_reason && (
                    // 管家打回时填的原因；后台任务页已显示，保洁端此前只见「已打回,请重做」
                    // 却看不到为什么——整改没有方向，同样的问题会再犯。
                    <div
                      style={{
                        fontSize: 13,
                        color: "#a8071a",
                        background: "#fff1f0",
                        border: "1px solid #ffa39e",
                        borderRadius: 6,
                        padding: "6px 10px",
                        marginTop: 8,
                      }}
                    >
                      打回原因：{t.rejection_reason}
                    </div>
                  )}
                </div>
              </div>

              <div style={{ display: "flex", gap: 8, marginTop: 10, justifyContent: "flex-end", alignItems: "center" }}>
                {/* 保洁全程手机操作——按 CLAUDE.md 军规触控目标 ≥44px，small(32px) 会误触 */}
                {t.status === "pending" && (
                  <Button
                    type="primary"
                    style={{ minHeight: 44, paddingInline: 20 }}
                    loading={startMutation.isPending}
                    onClick={() => startMutation.mutate(t.task_id)}
                  >
                    开始打扫
                  </Button>
                )}
                {t.status === "in_progress" && !t.submitted_at && (
                  <Button
                    type="primary"
                    style={{ minHeight: 44, paddingInline: 20 }}
                    loading={submitMutation.isPending}
                    onClick={() =>
                      Modal.confirm({
                        title: "提交完工?",
                        content: "提交后管家会到房间查房,通过后任务结束。",
                        okText: "确定提交",
                        cancelText: "再检查一下",
                        onOk: () => submitMutation.mutate(t.task_id),
                      })
                    }
                  >
                    提交完工
                  </Button>
                )}
                {t.review_status === "rejected" && (
                  <Button
                    type="primary"
                    style={{ minHeight: 44, paddingInline: 20 }}
                    loading={submitMutation.isPending}
                    onClick={() =>
                      Modal.confirm({
                        title: "重新提交?",
                        content: "确认已按管家要求整改完毕,再次提交查房。",
                        okText: "再次提交",
                        cancelText: "取消",
                        onOk: () => submitMutation.mutate(t.task_id),
                      })
                    }
                  >
                    整改完成,再次提交
                  </Button>
                )}
              </div>
            </div>
          ))
        )}
      </div>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div>
      <div style={{ fontSize: 11, color: "#A89680" }}>{label}</div>
      <div className="serif" style={{ fontSize: 26, fontWeight: 400, marginTop: 4, letterSpacing: 0 }}>
        {value}
      </div>
    </div>
  );
}
