"use client";

import React, { useState } from "react";
import { extractErrorMessage } from "@/lib/api-errors";
import { useSearchParams } from "next/navigation";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import dayjs from "dayjs";
import { tasksApi, staffApi } from "@/lib/api";
import {
  Card,
  List,
  Tag,
  Button,
  Select,
  Space,
  Typography,
  Checkbox,
  Badge,
  Empty,
  Skeleton,
  Tooltip,
  Switch,
  Row,
  Col,
  Popconfirm,
  Modal,
  Input,
  message,
} from "antd";
import {
  CheckCircleOutlined, ClockCircleOutlined, ExclamationCircleOutlined,
  WarningOutlined,
} from "@ant-design/icons";
import { useIsMobile } from "@/lib/responsive";
import { PageHeader } from "@/components/ui/PageHeader";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { EmptyState } from "@/components/ui/EmptyState";
import { tokens } from "@/lib/design-tokens";

const { Title, Text } = Typography;

const TASK_TYPES: Record<string, string> = {
  collect_deposit: "收取押金",
  cleaning: "保洁安排",
  checkout_inspection: "退房查房",
  return_deposit: "退还押金",
  custom: "自定义",
};

const PRIORITY_CONFIG: Record<string, { color: string; label: string; icon: React.ReactNode }> = {
  low: { color: "default", label: "低", icon: <ClockCircleOutlined /> },
  medium: { color: "processing", label: "中", icon: <ClockCircleOutlined /> },
  high: { color: "warning", label: "高", icon: <ExclamationCircleOutlined /> },
  urgent: { color: "error", label: "紧急", icon: <WarningOutlined /> },
};

const STATUS_CONFIG: Record<string, { color: string; label: string }> = {
  pending: { color: "default", label: "待处理" },
  in_progress: { color: "processing", label: "进行中" },
  pending_review: { color: "warning", label: "待审核" },
  done: { color: "success", label: "已完成" },
};

// 衍生展示态:保洁提交完工后, task.status 仍是 in_progress 但 review_status=pending_review,
// 前台/管家应该看到"待审核"而不是"进行中",否则不知道该去查房。
function displayStatus(task: any): string {
  if (task.review_status === "pending_review") return "pending_review";
  if (task.review_status === "rejected") return "in_progress";  // 打回了,保洁继续做
  return task.status;
}

export default function TasksPage() {
  const isMobile = useIsMobile();
  const qc = useQueryClient();
  const searchParams = useSearchParams();
  // 支持 URL 参数预填筛选(从 dashboard 跳转过来时)。
  const [statusFilter, setStatusFilter] = useState<string | undefined>(
    () => searchParams.get("status") || undefined
  );
  const [overdueOnly, setOverdueOnly] = useState(
    () => searchParams.get("overdue") === "true"
  );

  const { data: tasks, isLoading } = useQuery({
    queryKey: ["tasks", statusFilter, overdueOnly],
    queryFn: () =>
      tasksApi
        .list({
          status: statusFilter || undefined,
          overdue_only: overdueOnly || undefined,
        })
        .then((r) => r.data),
  });

  // 保洁姓名 lookup: TaskOut 只有 assignee_id, 显示需要 cleaner.display_name。
  // 走 /staff/cleaners 而非 /auth/users: 后者仅 admin 可读, operator 进任务页会 403 x4;
  // 任务 assignee 本就只可能是 cleaner(派单/自领都写 cleaner.user_id), 该端点对
  // admin/operator/keeper/finance 均放行, 返回 {user_id, display_name} 正好够用。
  const { data: cleaners } = useQuery({
    queryKey: ["staff-cleaners"],
    queryFn: () => staffApi.listCleaners().then((r) => r.data),
    staleTime: 5 * 60 * 1000,
  });
  const userNameById = React.useMemo(() => {
    const m: Record<string, string> = {};
    (cleaners || []).forEach((u) => { m[u.user_id] = u.display_name; });
    return m;
  }, [cleaners]);

  // 完成任务 mutation: 清扫任务必须走 review_task(approved=true) 触发后端房态/订单联动;
  // 直接 PATCH status=done 会跳过 review_task 里的"房间→available + 订单→completed"流程,
  // 导致前台看似完成实际房态没恢复(2026-05-29 孙鹏飞反馈"前台不显示保洁状态"的 root cause)。
  const completeMutation = useMutation({
    mutationFn: (task: any) => {
      const isCleaningPendingReview =
        task.task_type === "cleaning" && task.review_status === "pending_review";
      if (isCleaningPendingReview) {
        return tasksApi.review(task.task_id || task.id, true);
      }
      return tasksApi.update(task.task_id || task.id, { status: "done" });
    },
    onSuccess: (_, task: any) => {
      const isCleaningPendingReview =
        task.task_type === "cleaning" && task.review_status === "pending_review";
      message.success(isCleaningPendingReview ? "查房通过,房间已恢复可入住" : "任务已完成");
      qc.invalidateQueries({ queryKey: ["tasks"] });
      qc.invalidateQueries({ queryKey: ["rooms"] });
      qc.invalidateQueries({ queryKey: ["orders"] });
    },
    onError: (e: any) => message.error(extractErrorMessage(e, "操作失败")),
  });

  const rejectMutation = useMutation({
    mutationFn: ({ taskId, reason }: { taskId: string; reason: string }) =>
      tasksApi.review(taskId, false, reason),
    onSuccess: () => {
      message.success("已打回,保洁可重做");
      qc.invalidateQueries({ queryKey: ["tasks"] });
    },
    onError: (e: any) => message.error(extractErrorMessage(e, "打回失败")),
  });

  const deleteMutation = useMutation({
    mutationFn: (taskId: string) => tasksApi.delete(taskId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["tasks"] });
    },
  });

  const taskList = Array.isArray(tasks) ? tasks : [];
  const pending = taskList.filter((t) => t.status !== "done").length;
  const overdue = taskList.filter(
    (t) => t.deadline && new Date(t.deadline) < new Date() && t.status !== "done"
  ).length;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <PageHeader
        title="运营任务"
        subtitle={
          <>
            待处理 <b style={{ color: tokens.color.text.primary }}>{pending}</b> 项
            {overdue > 0 && (
              <>
                {" "}
                · <span style={{ color: tokens.color.status.warn }}>逾期 {overdue} 项</span>
              </>
            )}
          </>
        }
      />

      {/* Summary cards */}
      <Row gutter={[12, 12]}>
        {Object.entries(STATUS_CONFIG).map(([k, v]) => {
          const count = taskList.filter((t) => displayStatus(t) === k).length;
          const active = statusFilter === k;
          return (
            <Col key={k} xs={12} sm={6}>
              <div
                onClick={() => setStatusFilter(statusFilter === k ? undefined : k)}
                className="card-hoverable"
                style={{
                  background: tokens.color.bg.container,
                  border: `1px solid ${active ? tokens.color.brand.primary : tokens.color.bg.border}`,
                  boxShadow: tokens.shadow.sm,
                  borderRadius: tokens.radius.lg,
                  padding: 14,
                  cursor: "pointer",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                }}
              >
                <div>
                  <div style={{ fontSize: 12, color: tokens.color.text.secondary }}>{v.label}</div>
                  <div
                    className="tabular"
                    style={{ fontSize: 24, fontWeight: 700, marginTop: 2, letterSpacing: "-.01em" }}
                  >
                    {count}
                  </div>
                </div>
                <StatusBadge status={k} size="sm" />
              </div>
            </Col>
          );
        })}
      </Row>

      {/* Filters */}
      <div
        style={{
          background: tokens.color.bg.container,
          border: `1px solid ${tokens.color.bg.border}`,
          borderRadius: tokens.radius.lg,
          padding: "10px 16px",
          display: "flex",
          flexWrap: "wrap",
          alignItems: "center",
          gap: 12,
        }}
      >
        <Select
          placeholder="全部状态"
          allowClear
          value={statusFilter}
          onChange={setStatusFilter}
          style={{ width: 140 }}
          options={Object.entries(STATUS_CONFIG).map(([k, v]) => ({ value: k, label: v.label }))}
        />
        <Space>
          <Switch size="small" checked={overdueOnly} onChange={setOverdueOnly} />
          <Text style={{ fontSize: 13 }}>仅显示逾期</Text>
        </Space>
      </div>

      {/* Task list */}
      <Skeleton loading={isLoading} active>
        {taskList.length === 0 ? (
          <div
            style={{
              background: tokens.color.bg.container,
              border: `1px solid ${tokens.color.bg.border}`,
              borderRadius: tokens.radius.lg,
            }}
          >
            <EmptyState title="暂无任务" description="当前没有符合筛选条件的任务。" />
          </div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {taskList.map((task: any) => {
              const isOverdue =
                task.deadline && new Date(task.deadline) < new Date() && task.status !== "done";
              const isDone = task.status === "done";
              const priority = PRIORITY_CONFIG[task.priority] || PRIORITY_CONFIG.medium;
              const dispStatus = displayStatus(task);
              const isCleaning = task.task_type === "cleaning";
              const isPendingReview = task.review_status === "pending_review";
              const isRejected = task.review_status === "rejected";
              // 清扫任务必须保洁提交后才能审核完成;非清扫任务管家随时可勾完成。
              const checkboxDisabled =
                isDone || (isCleaning && !isPendingReview);
              const checkboxTooltip = isDone
                ? "已完成"
                : isCleaning && !isPendingReview
                ? "保洁尚未提交完工,不能直接完成"
                : isCleaning && isPendingReview
                ? "查房通过(房间恢复可入住,订单完成)"
                : "标记完成";
              const assigneeName = task.assignee_id ? userNameById[task.assignee_id] : null;

              return (
                <Card
                  key={task.task_id || task.id}
                  bordered={false}
                  style={{
                    borderRadius: tokens.radius.lg,
                    boxShadow: tokens.shadow.sm,
                    border: `1px solid ${isOverdue ? "rgba(239,68,68,.4)" : tokens.color.bg.border}`,
                    borderLeft: `3px solid ${
                      isOverdue
                        ? tokens.color.status.warn
                        : isDone
                        ? tokens.color.status.active
                        : tokens.color.brand.primary
                    }`,
                    background: tokens.color.bg.container,
                    opacity: isDone ? 0.7 : 1,
                  }}
                  styles={{ body: { padding: "12px 16px" } }}
                >
                  <div style={{ display: "flex", alignItems: "flex-start", gap: isMobile ? 10 : 12 }}>
                    {/* Checkbox — 44x44 touch target on mobile */}
                    <Tooltip title={checkboxTooltip}>
                      <div style={{
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "center",
                        minWidth: isMobile ? 44 : "auto",
                        minHeight: isMobile ? 44 : "auto",
                        flexShrink: 0,
                      }}>
                        <Checkbox
                          checked={isDone}
                          disabled={checkboxDisabled}
                          aria-label={isDone ? `${task.title} 已完成` : `标记 ${task.title} 为完成`}
                          onChange={() => {
                            if (!checkboxDisabled) completeMutation.mutate(task);
                          }}
                        />
                      </div>
                    </Tooltip>

                    {/* Content */}
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <Space wrap size={6} style={{ marginBottom: 4 }}>
                        <Text
                          strong
                          style={{
                            fontSize: 14,
                            textDecoration: isDone ? "line-through" : "none",
                            color: isDone ? "#8c8c8c" : "#262626",
                          }}
                        >
                          {task.title}
                        </Text>
                        <Tag color={priority.color} icon={priority.icon} style={{ fontSize: 11 }}>
                          {priority.label}
                        </Tag>
                        <Tag bordered={false} style={{ fontSize: 11, background: "#f5f5f5", color: "#595959" }}>
                          {TASK_TYPES[task.task_type] || task.task_type}
                        </Tag>
                        {isOverdue && (
                          <Tag color="error" icon={<WarningOutlined />} style={{ fontSize: 11 }}>
                            已逾期
                          </Tag>
                        )}
                      </Space>

                      {isMobile ? (
                        <div style={{ display: "flex", flexDirection: "column", gap: 2, marginTop: 4 }}>
                          {task.order_id && (
                            <Text type="secondary" style={{ fontSize: 11 }}>
                              订单: {task.order_id}
                            </Text>
                          )}
                          {task.room_id && (
                            <Text type="secondary" style={{ fontSize: 11 }}>
                              房间: {task.room_id}
                            </Text>
                          )}
                          {task.deadline && (
                            <Text
                              style={{
                                fontSize: 11,
                                color: isOverdue ? "#ff4d4f" : "#8c8c8c",
                              }}
                            >
                              <ClockCircleOutlined style={{ marginRight: 3 }} />
                              {new Date(task.deadline).toLocaleString("zh-CN", {
                                dateStyle: "short",
                                timeStyle: "short",
                              })}
                            </Text>
                          )}
                          {assigneeName && (
                            <Text type="secondary" style={{ fontSize: 11 }}>
                              负责人: {assigneeName}
                            </Text>
                          )}
                          {task.submitted_at && (
                            <Text style={{ fontSize: 11, color: "#fa8c16" }}>
                              提交于 {dayjs(task.submitted_at).format("M月D日 HH:mm")}
                            </Text>
                          )}
                          {isRejected && task.rejection_reason && (
                            <Text style={{ fontSize: 11, color: "#ff4d4f" }}>
                              已打回: {task.rejection_reason}
                            </Text>
                          )}
                        </div>
                      ) : (
                        <Space size={16} style={{ marginTop: 2 }} wrap>
                          {task.order_id && (
                            <Text type="secondary" style={{ fontSize: 11 }}>
                              订单: {task.order_id}
                            </Text>
                          )}
                          {task.room_id && (
                            <Text type="secondary" style={{ fontSize: 11 }}>
                              房间: {task.room_id}
                            </Text>
                          )}
                          {task.deadline && (
                            <Text
                              style={{
                                fontSize: 11,
                                color: isOverdue ? "#ff4d4f" : "#8c8c8c",
                              }}
                            >
                              <ClockCircleOutlined style={{ marginRight: 3 }} />
                              {new Date(task.deadline).toLocaleString("zh-CN", {
                                dateStyle: "short",
                                timeStyle: "short",
                              })}
                            </Text>
                          )}
                          {assigneeName && (
                            <Text type="secondary" style={{ fontSize: 11 }}>
                              负责人: {assigneeName}
                            </Text>
                          )}
                          {task.submitted_at && (
                            <Text style={{ fontSize: 11, color: "#fa8c16" }}>
                              提交于 {dayjs(task.submitted_at).format("M月D日 HH:mm")}
                            </Text>
                          )}
                          {isRejected && task.rejection_reason && (
                            <Text style={{ fontSize: 11, color: "#ff4d4f" }}>
                              已打回: {task.rejection_reason}
                            </Text>
                          )}
                        </Space>
                      )}
                    </div>

                    {/* Status tag + actions */}
                    <Space direction="vertical" align="end" size={6}>
                      <StatusBadge status={dispStatus} size="sm" />
                      {isCleaning && isPendingReview && (
                        <Button
                          size="small"
                          danger
                          onClick={() => {
                            let reason = "";
                            Modal.confirm({
                              title: "打回保洁重做?",
                              content: (
                                <div style={{ marginTop: 8 }}>
                                  <div style={{ fontSize: 12, color: "#888", marginBottom: 6 }}>
                                    填写打回原因(保洁可在自己的端看到):
                                  </div>
                                  <Input.TextArea
                                    rows={3}
                                    placeholder="例如:卫生间地板未拖干净"
                                    onChange={(e) => { reason = e.target.value; }}
                                  />
                                </div>
                              ),
                              okText: "打回",
                              cancelText: "取消",
                              onOk: () => {
                                if (!reason.trim()) {
                                  message.error("请填写打回原因");
                                  return Promise.reject();
                                }
                                return rejectMutation.mutateAsync({
                                  taskId: task.task_id || task.id,
                                  reason: reason.trim(),
                                });
                              },
                            });
                          }}
                        >
                          打回重做
                        </Button>
                      )}
                      <Popconfirm
                        title="删除任务"
                        description="确认删除该运营任务？此操作不可恢复。"
                        onConfirm={() => deleteMutation.mutate(task.task_id || task.id)}
                      >
                        <Button
                          type="link"
                          danger
                          size="small"
                          style={{ padding: isMobile ? "4px 8px" : 0, minHeight: isMobile ? 44 : "auto" }}
                          aria-label={`删除任务 ${task.title}`}
                        >
                          删除
                        </Button>
                      </Popconfirm>
                    </Space>
                  </div>
                </Card>
              );
            })}
          </div>
        )}
      </Skeleton>
    </div>
  );
}
