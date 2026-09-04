"use client";

import Link from "next/link";
import { Badge, Button } from "antd";
import { NotificationOutlined } from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";

import { notificationsApi } from "@/lib/api";

export function NotificationBell() {
  const unread = useQuery({
    queryKey: ["notifications", "unread"],
    queryFn: async () => (await notificationsApi.list({ is_read: false, page_size: 100 })).data,
    refetchInterval: 60_000,
  });
  const count = unread.data?.length ?? 0;
  return (
    <Link href="/notifications" aria-label={`${count} 条未读通知`}>
      <Badge count={count} size="small" overflowCount={99}>
        <Button type="text" shape="circle" icon={<NotificationOutlined />} aria-hidden />
      </Badge>
    </Link>
  );
}
