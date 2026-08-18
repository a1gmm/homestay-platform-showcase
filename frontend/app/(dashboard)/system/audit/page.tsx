"use client";

import { useMemo, useState, useEffect } from "react";
import { extractErrorMessage } from "@/lib/api-errors";
import { useQuery } from "@tanstack/react-query";
import { Card, Table, Typography, Tag, DatePicker, Space, message, Button } from "antd";
import { CloseCircleOutlined } from "@ant-design/icons";
import dayjs, { Dayjs } from "dayjs";
import { auditApi, usersApi } from "@/lib/api";
import { useAuthStore } from "@/lib/auth";
import { useRouter, useSearchParams } from "next/navigation";

const { Title, Text } = Typography;
const { RangePicker } = DatePicker;

const ACTION_LABELS: Record<string, string> = {
  create_user: "创建账号",
  update_user_status: "修改账号状态",
  reset_password: "重置密码",
};

const RESOURCE_LABELS: Record<string, string> = {
  user: "账号",
  order: "订单",
  room: "房间",
  payment: "收款",
  refund: "退款",
  expense: "支出",
  task: "任务",
};

// 点号动作(如 order.update / payment.create)的动词字典，避免审计页显示英文事件 key。
const VERB_LABELS: Record<string, string> = {
  create: "创建",
  update: "修改",
  delete: "删除",
  cancel: "取消",
  transition: "流转",
  refund: "退款",
  pay: "收款",
  lock: "锁房",
  unlock: "解锁",
  assign: "排房",
  checkin: "办理入住",
  checkout: "办理退房",
  import: "导入",
  export: "导出",
};

// 把后端 action 转成中文「人话」。优先整词字典；点号格式 <资源>.<动词> 拆开翻译；都缺则原样。
function humanizeAction(action: string): string {
  if (!action) return "";
  if (ACTION_LABELS[action]) return ACTION_LABELS[action];
  if (action.includes(".")) {
    const [res, verb] = action.split(".");
    const verbCn = VERB_LABELS[verb];
    const resCn = RESOURCE_LABELS[res];
    if (verbCn) return `${verbCn}${resCn ?? ""}`.trim();
  }
  if (VERB_LABELS[action]) return VERB_LABELS[action];
  return action;
}

export default function AuditPage() {
  const { user } = useAuthStore();
  const router = useRouter();
  const searchParams = useSearchParams();
  const resourceType = searchParams.get("resource_type") ?? undefined;
  const resourceId = searchParams.get("resource_id") ?? undefined;
  const [range, setRange] = useState<[Dayjs | null, Dayjs | null] | null>(null);
  const isAdmin = !!user && user.role === "admin";

  useEffect(() => {
    if (user && user.role !== "admin") {
      router.replace("/dashboard");
    }
  }, [user, router]);

  const { data: logsData, isLoading } = useQuery({
    queryKey: ["audit-logs", resourceType ?? null, resourceId ?? null],
    enabled: isAdmin,
    queryFn: () =>
      auditApi
        .list({ resource_type: resourceType, resource_id: resourceId, page_size: 200 })
        .then((r) => r.data)
        .catch((err) => {
          message.error(extractErrorMessage(err, "获取审计日志失败，请稍后重试"));
          return [];
        }),
  });

  const { data: usersData } = useQuery({
    queryKey: ["users-for-audit"],
    enabled: isAdmin,
    queryFn: () =>
      usersApi
        .list()
        .then((r) => r.data)
        .catch(() => []),
  });

  const userMap = useMemo(() => {
    const map: Record<string, { username: string; display_name: string }> = {};
    if (Array.isArray(usersData)) {
      usersData.forEach((u: any) => {
        map[u.user_id] = { username: u.username, display_name: u.display_name };
      });
    }
    return map;
  }, [usersData]);

  const logs = useMemo(() => {
    if (!Array.isArray(logsData)) return [];
    if (!range || (!range[0] && !range[1])) return logsData;
    const [start, end] = range;
    return logsData.filter((log: any) => {
      const t = dayjs(log.created_at);
      if (start && t.isBefore(start, "minute")) return false;
      if (end && t.isAfter(end, "minute")) return false;
      return true;
    });
  }, [logsData, range]);

  if (!isAdmin) return null;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
        }}
      >
        <div>
          <Title level={4} style={{ margin: 0 }}>
            审计日志
          </Title>
          <Text type="secondary">
            用「人话」记录关键操作，如：谁在什么时候，对哪个账号/订单做了什么修改。
          </Text>
        </div>
      </div>

      <Card
        bordered={false}
        style={{ borderRadius: 12, boxShadow: "0 2px 8px rgba(0,0,0,0.06)" }}
      >
        <Space wrap style={{ marginBottom: 16 }}>
          <Text type="secondary">时间范围：</Text>
          <RangePicker
            value={range as any}
            onChange={(v) => setRange(v as any)}
            allowClear
          />
          {(resourceType || resourceId) && (
            <Tag
              closable
              color="blue"
              closeIcon={<CloseCircleOutlined />}
              onClose={() => router.push("/system/audit")}
              style={{ fontSize: 13, padding: "4px 8px" }}
            >
              已筛选：
              {RESOURCE_LABELS[resourceType ?? ""] || resourceType || ""}
              {resourceId ? ` · ${resourceId}` : ""}
            </Tag>
          )}
        </Space>
        <Table
          rowKey={(row: any) => row.log_id}
          loading={isLoading}
          dataSource={logs}
          pagination={{ pageSize: 50 }}
          size="small"
          columns={[
            {
              title: "时间",
              dataIndex: "created_at",
              render: (v: string) =>
                dayjs(v).format("YYYY-MM-DD HH:mm:ss"),
              width: 180,
            },
            {
              title: "操作描述",
              render: (_: any, record: any) => {
                const operator =
                  (record.operator_id &&
                    userMap[record.operator_id]?.display_name) ||
                  record.operator_id ||
                  "系统";
                const targetUser =
                  record.resource_type === "user" &&
                  record.resource_id &&
                  userMap[record.resource_id]
                    ? userMap[record.resource_id].display_name +
                      `（${userMap[record.resource_id].username}）`
                    : record.resource_id;
                const actionLabel = humanizeAction(record.action);
                const resourceLabel =
                  RESOURCE_LABELS[record.resource_type] ||
                  record.resource_type ||
                  "";

                if (record.action === "update_user_status" && targetUser) {
                  return (
                    <span>
                      {operator} 修改了 {resourceLabel}{" "}
                      <Text strong>{targetUser}</Text> 的启用状态
                    </span>
                  );
                }
                if (record.action === "reset_password" && targetUser) {
                  return (
                    <span>
                      {operator} 为 {resourceLabel}{" "}
                      <Text strong>{targetUser}</Text> 重置了密码
                    </span>
                  );
                }
                if (record.action === "create_user" && targetUser) {
                  return (
                    <span>
                      {operator} 创建了新的 {resourceLabel}{" "}
                      <Text strong>{targetUser}</Text>
                    </span>
                  );
                }
                return (
                  <span>
                    {operator} 执行了 {actionLabel}
                    {resourceLabel && targetUser && (
                      <>
                        {" "}
                        ，对象：{resourceLabel}{" "}
                        <Text strong>{targetUser}</Text>
                      </>
                    )}
                  </span>
                );
              },
            },
          ]}
        />
      </Card>
    </div>
  );
}

