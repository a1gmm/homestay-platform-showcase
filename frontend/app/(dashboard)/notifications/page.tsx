"use client";

import { useState } from "react";
import {
  Card,
  List,
  Button,
  Tag,
  Space,
  Typography,
  Empty,
  Skeleton,
  Badge,
  Segmented,
  message,
} from "antd";
import {
  BellOutlined,
  CheckOutlined,
  ReadOutlined,
} from "@ant-design/icons";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { notificationsApi } from "@/lib/api";
import type { NotificationOut } from "@/lib/types";
import dayjs from "dayjs";
import relativeTime from "dayjs/plugin/relativeTime";

// 全局 zh-cn locale 已在 app/providers.tsx 设置，本页只需扩展 relativeTime 插件。
dayjs.extend(relativeTime);

const { Title, Text, Paragraph } = Typography;

const TYPE_LABELS: Record<string, { label: string; color: string }> = {
  checkin_guide: { label: "入住指南", color: "blue" },
  system: { label: "系统", color: "default" },
  task: { label: "任务", color: "orange" },
  order: { label: "订单", color: "green" },
  settlement: { label: "结算", color: "purple" },
  alert: { label: "告警", color: "red" },
};

type FilterType = "all" | "unread" | "read";

export default function NotificationsPage() {
  const [filter, setFilter] = useState<FilterType>("all");
  const queryClient = useQueryClient();

  const isReadParam = filter === "unread" ? false : filter === "read" ? true : undefined;

  const { data: notifications, isLoading } = useQuery({
    queryKey: ["notifications", filter],
    queryFn: () =>
      notificationsApi.list({ is_read: isReadParam }).then((r) => r.data),
  });

  const markReadMutation = useMutation({
    mutationFn: (id: string) => notificationsApi.markAsRead(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["notifications"] });
    },
  });

  const markAllReadMutation = useMutation({
    mutationFn: () => notificationsApi.markAllAsRead(),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["notifications"] });
      message.success("已全部标记为已读");
    },
  });

  const unreadCount = notifications?.filter((n: NotificationOut) => !n.is_read).length ?? 0;

  return (
    <div>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          marginBottom: 16,
        }}
      >
        <Space>
          <BellOutlined style={{ fontSize: 24 }} />
          <Title level={4} style={{ margin: 0 }}>
            通知中心
          </Title>
          {unreadCount > 0 && (
            <Badge count={unreadCount} style={{ marginLeft: 8 }} />
          )}
        </Space>
        <Button
          icon={<CheckOutlined />}
          onClick={() => markAllReadMutation.mutate()}
          loading={markAllReadMutation.isPending}
          disabled={unreadCount === 0}
        >
          全部已读
        </Button>
      </div>

      <Card>
        <Segmented
          value={filter}
          onChange={(val) => setFilter(val as FilterType)}
          options={[
            { label: "全部", value: "all" },
            { label: "未读", value: "unread" },
            { label: "已读", value: "read" },
          ]}
          style={{ marginBottom: 16 }}
        />

        {isLoading ? (
          <Skeleton active paragraph={{ rows: 5 }} />
        ) : !notifications || notifications.length === 0 ? (
          <Empty description="暂无通知" />
        ) : (
          <List
            dataSource={notifications}
            renderItem={(item: NotificationOut) => {
              const typeInfo = TYPE_LABELS[item.type] || {
                label: item.type,
                color: "default",
              };
              return (
                <List.Item
                  key={item.notification_id}
                  style={{
                    background: item.is_read
                      ? "transparent"
                      : "rgba(22, 119, 255, 0.04)",
                    padding: "12px 16px",
                    borderRadius: 8,
                    marginBottom: 4,
                  }}
                  actions={
                    !item.is_read
                      ? [
                          <Button
                            key="read"
                            type="link"
                            size="small"
                            icon={<ReadOutlined />}
                            onClick={() =>
                              markReadMutation.mutate(item.notification_id)
                            }
                            loading={markReadMutation.isPending}
                          >
                            标记已读
                          </Button>,
                        ]
                      : undefined
                  }
                >
                  <List.Item.Meta
                    title={
                      <Space>
                        {!item.is_read && (
                          <Badge status="processing" />
                        )}
                        <Text strong={!item.is_read}>{item.title}</Text>
                        <Tag color={typeInfo.color}>{typeInfo.label}</Tag>
                      </Space>
                    }
                    description={
                      <div>
                        {item.content && (
                          <Paragraph
                            style={{
                              marginBottom: 4,
                              color: "rgba(0,0,0,0.65)",
                              whiteSpace: "pre-line",
                            }}
                            ellipsis={{ rows: 2, expandable: true }}
                          >
                            {item.content}
                          </Paragraph>
                        )}
                        <Text type="secondary" style={{ fontSize: 12 }}>
                          {dayjs(item.created_at).fromNow()}
                        </Text>
                      </div>
                    }
                  />
                </List.Item>
              );
            }}
          />
        )}
      </Card>
    </div>
  );
}
