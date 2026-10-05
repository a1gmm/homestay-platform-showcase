"use client";

import React, { useState } from "react";
import { extractErrorMessage } from "@/lib/api-errors";
import { useTasksWorkspace } from "@/hooks/useTasksWorkspace";
import { usePrivacyMode } from "@/hooks/usePrivacyMode";
import { readPrivacyMode, PrivacyModeReadOnlyError } from "@/lib/privacy-mode";
import type { TaskOut } from "@/lib/types";
import { taskDisplayStatus as displayStatus, isActiveTask } from "@/lib/task-workspace";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import dayjs from "dayjs";
import { tasksApi, staffApi } from "@/lib/api";
import {
  Card,
  Alert,
  DatePicker,
  Pagination,
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
import { useAuthStore } from "@/lib/auth";
import { HistoricalTaskArchiveModal } from "@/components/tasks/HistoricalTaskArchiveModal";

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

export default function TasksPage() {
  const isMobile = useIsMobile();
  const qc = useQueryClient();
  const privacyMode = usePrivacyMode();
  const role = useAuthStore(state => state.user?.role);
  const canManage = ["admin", "operator", "keeper"].includes(role || "");
  const canDelete = ["admin", "operator"].includes(role || "");
  const [editingTask, setEditingTask] = useState<TaskOut | null>(null);
  const [archiveTask, setArchiveTask] = useState<TaskOut | null>(null);
  const [editDeadline, setEditDeadline] = useState<string | null>(null);
  const [editAssignee, setEditAssignee] = useState<string | null>(null);
  const workspace = useTasksWorkspace();
  const { statusFilter, setStatusFilter, overdueOnly, setOverdueOnly } = workspace;
  const { isLoading, isError, error, refetch } = workspace.query;

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
      if (readPrivacyMode()) return Promise.reject(new PrivacyModeReadOnlyError());
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
    mutationFn: ({ taskId, reason }: { taskId: string; reason: string }) => {
      if (readPrivacyMode()) return Promise.reject(new PrivacyModeReadOnlyError());
      return tasksApi.review(taskId, false, reason);
    },
    onSuccess: () => {
      message.success("已打回,保洁可重做");
      qc.invalidateQueries({ queryKey: ["tasks"] });
    },
    onError: (e: any) => message.error(extractErrorMessage(e, "打回失败")),
  });

  const deleteMutation = useMutation({
    mutationFn: (taskId: string) => {
      if (readPrivacyMode()) return Promise.reject(new PrivacyModeReadOnlyError());
      return tasksApi.delete(taskId);
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["tasks"] });
    },
  });

  const detailsMutation = useMutation({
    mutationFn: () => {
      if (readPrivacyMode()) return Promise.reject(new PrivacyModeReadOnlyError());
      if (!editingTask) return Promise.reject(new Error("请选择任务"));
      return tasksApi.update(editingTask.task_id, { deadline: editDeadline, assignee_id: editAssignee });
    },
    onSuccess: () => {
      setEditingTask(null);
      qc.invalidateQueries({ queryKey: ["tasks"] });
      qc.invalidateQueries({ queryKey: ["dashboard"] });
      message.success("任务安排已保存");
    },
    onError: (e: unknown) => message.error(extractErrorMessage(e, "保存失败")),
  });

  const taskList = workspace.items;
  const pending = workspace.active;
  const overdue = workspace.overdue;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <PageHeader
        title="运营任务"
        subtitle={
          <>
            未完成 <b style={{ color: tokens.color.text.primary }}>{pending}</b> 项
            {overdue > 0 && (
              <>
                {" "}
                · <span style={{ color: tokens.color.status.warn }}>逾期 {overdue} 项</span>
              </>
            )}
          </>
        }
      />

      {privacyMode && <Alert type="info" showIcon message="隐私演示模式：任务操作已禁用" />}
      {!isError && (workspace.historical || 0) > 0 && <Alert type="info" showIcon
        message={`历史保洁待核查 ${workspace.historical} 项`}
        description={<Space direction="vertical">
          <span>这些任务创建已满 7 天，关联订单已完成或取消。请核实后安排继续执行，或由管理员填写原因归档。</span>
          <Button style={{ minHeight: 44 }} onClick={() => { workspace.setWorkScope("historical"); setStatusFilter("active"); }}>查看历史待办</Button>
        </Space>} />}
      {!isLoading && !isError && workspace.needs_attention > 0 && <Alert type="warning" showIcon
        message={`待核查 ${workspace.needs_attention} 项`}
        description={`未设截止时间 ${workspace.no_deadline} 项 · 未分配负责人 ${workspace.unassigned} 项 · 积压 7 天以上 ${workspace.aged} 项。没有逾期不代表任务已处理，请补全安排并核实历史任务。`} />}

      {/* Summary cards */}
      <Row gutter={[12, 12]}>
        {Object.entries(STATUS_CONFIG).map(([k, v]) => {
          const count = workspace.counts[k] || 0;
          const active = statusFilter === k;
          return (
            <Col key={k} xs={12} sm={6}>
              <div
                role="button"
                tabIndex={0}
                aria-pressed={active}
                onKeyDown={event => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    setStatusFilter(statusFilter === k ? "active" : k);
                  }
                }}
                onClick={() => setStatusFilter(statusFilter === k ? "active" : k)}
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
                    style={{ fontSize: 24, fontWeight: 500, marginTop: 2, letterSpacing: 0 }}
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
        <Select aria-label="任务范围" value={workspace.workScope} onChange={workspace.setWorkScope}
          style={{ minWidth: 150 }} options={[
            { value: "current", label: "日常任务" }, { value: "historical", label: "历史保洁待核查" }, { value: "all", label: "全部范围" },
          ]} />
        <Select
          aria-label="任务状态"
          placeholder="未完成任务"
          value={statusFilter}
          onChange={setStatusFilter}
          style={{ width: 140 }}
          options={[
            { value: "active", label: "未完成任务" },
            { value: "all", label: "全部任务" },
            ...Object.entries(STATUS_CONFIG).map(([k, v]) => ({ value: k, label: v.label })),
            { value: "cancelled", label: "已取消" },
          ]}
        />
        <Input aria-label="搜索任务" placeholder="房号 / 订单号 / 任务名称" allowClear value={workspace.keyword} onChange={e => workspace.setKeyword(e.target.value)} style={{ width: isMobile ? "100%" : 240 }} />
        <Select aria-label="任务负责人" placeholder="全部负责人" allowClear value={workspace.assignee} onChange={workspace.setAssignee} style={{ minWidth: 140 }} options={[{ value: "unassigned", label: "未分配" }, ...(cleaners || []).map(u => ({ value: u.user_id, label: u.display_name }))]} />
        <DatePicker aria-label="任务截止日期" placeholder="截止日期" value={workspace.deadline ? dayjs(workspace.deadline) : null} onChange={value => workspace.setDeadline(value?.format("YYYY-MM-DD"))} />
        <Select aria-label="任务待核查原因" placeholder="全部核查原因" allowClear value={workspace.attention}
          onChange={workspace.setAttention} style={{ minWidth: 150 }} options={[
            { value: "needs_attention", label: "需核查 / 补全" },
            { value: "no_deadline", label: "未设截止时间" },
            { value: "unassigned", label: "未分配负责人" },
            { value: "aged", label: "积压 7 天以上" },
          ]} />
        <Space>
          <Switch aria-label="仅显示逾期" size="small" checked={overdueOnly} onChange={setOverdueOnly} />
          <Text style={{ fontSize: 13 }}>仅显示逾期</Text>
        </Space>
      </div>

      {/* Task list */}
      <Skeleton loading={isLoading} active>
        {isError ? (
          <Alert type="error" showIcon message="任务加载失败" description={extractErrorMessage(error, "请重试，不能将加载失败视为没有待办。")} action={<Button onClick={() => refetch()}>重试</Button>} />
        ) : taskList.length === 0 ? (
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
            {workspace.visibleItems.map((task: any) => {
              const isOverdue =
                task.deadline && new Date(task.deadline) < new Date() && isActiveTask(task);
              const isDone = task.status === "done";
              const priority = PRIORITY_CONFIG[task.priority] || PRIORITY_CONFIG.medium;
              const dispStatus = displayStatus(task);
              const isCleaning = task.task_type === "cleaning";
              const isPendingReview = task.review_status === "pending_review";
              const isRejected = task.review_status === "rejected";
              // 清扫任务必须保洁提交后才能审核完成;非清扫任务管家随时可勾完成。
              const checkboxDisabled =
                privacyMode || !canManage || isDone || task.status === "cancelled" || (isCleaning && !isPendingReview);
              const checkboxTooltip = privacyMode ? "隐私演示模式下不可操作" : isDone
                ? "已完成"
                : isCleaning && !isPendingReview
                ? "保洁尚未提交完工,不能直接完成"
                : isCleaning && isPendingReview
                ? "查房通过（房间恢复可入住）"
                : "标记完成";
              const assigneeName = task.assignee_id ? userNameById[task.assignee_id] : null;

              return (
                <Card
                  key={task.task_id || task.id}
                  variant="borderless"
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
                      {isActiveTask(task) && (!task.deadline || !task.assignee_id) && (
                        <div style={{ fontSize: 12, color: tokens.color.text.secondary }}>
                          需补全：{[!task.deadline && "截止时间", !task.assignee_id && "负责人"].filter(Boolean).join("、")}
                        </div>
                      )}

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
                      {task.historical_review && <Tag>历史待核查</Tag>}
                      {task.archived && <Tag>已归档 · 保留记录</Tag>}
                      {role === "admin" && task.historical_review && <Button size="small" disabled={privacyMode}
                        onClick={() => setArchiveTask(task)}>核实并归档</Button>}
                      {canManage && isActiveTask(task) && <Button size="small" disabled={privacyMode}
                        onClick={() => {
                          setEditingTask(task);
                          setEditDeadline(task.deadline || null);
                          setEditAssignee(task.assignee_id || null);
                        }}>安排任务</Button>}
                      {canManage && isCleaning && isPendingReview && isActiveTask(task) && (
                        <Button
                          size="small"
                          danger
                          disabled={privacyMode}
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
                      {canDelete && !task.historical_review && !task.archived && <Popconfirm
                        title="删除任务"
                        description="确认删除该运营任务？此操作不可恢复。"
                        disabled={privacyMode}
                        onConfirm={() => deleteMutation.mutate(task.task_id || task.id)}
                      >
                        <Button
                          type="link"
                          danger
                          size="small"
                          disabled={privacyMode}
                          style={{ padding: isMobile ? "4px 8px" : 0, minHeight: isMobile ? 44 : "auto" }}
                          aria-label={`删除任务 ${task.title}`}
                        >
                          删除
                        </Button>
                      </Popconfirm>}
                    </Space>
                  </div>
                </Card>
              );
            })}
          </div>
        )}
      </Skeleton>
      {!isError && workspace.total > 30 && <Pagination current={workspace.page} pageSize={30} total={workspace.total} onChange={workspace.setPage} showSizeChanger={false} showTotal={total => `共 ${total} 项`} />}
      <Modal title="安排任务" open={Boolean(editingTask)} onCancel={() => setEditingTask(null)}
        onOk={() => detailsMutation.mutate()} confirmLoading={detailsMutation.isPending}
        okText="保存安排" cancelText="取消" okButtonProps={{ disabled: privacyMode }}>
        <Space direction="vertical" style={{ width: "100%" }}>
          <Text>{editingTask?.title}</Text>
          <Text type="secondary">根据实际安排填写负责人和截止时间；保存不会将任务标为完成。</Text>
          <Select aria-label="安排负责人" placeholder="选择负责人" allowClear disabled={privacyMode}
            style={{ width: "100%" }} value={editAssignee} onChange={value => setEditAssignee(value || null)}
            options={[
              ...(editAssignee && !userNameById[editAssignee] ? [{ value: editAssignee, label: editAssignee }] : []),
              ...(cleaners || []).map(u => ({ value: u.user_id, label: u.display_name })),
            ]} />
          <DatePicker aria-label="安排截止时间" showTime disabled={privacyMode} style={{ width: "100%" }}
            value={editDeadline ? dayjs(editDeadline) : null}
            onChange={value => setEditDeadline(value?.toISOString() || null)} />
        </Space>
      </Modal>
      {archiveTask && <HistoricalTaskArchiveModal key={archiveTask.task_id} task={archiveTask} onClose={() => setArchiveTask(null)} />}
    </div>
  );
}
