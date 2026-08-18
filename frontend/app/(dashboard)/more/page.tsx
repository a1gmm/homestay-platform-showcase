"use client";

import Link from "next/link";
import {
  DollarOutlined,
  CheckSquareOutlined,
  TeamOutlined,
  SettingOutlined,
  FileTextOutlined,
  RightOutlined,
} from "@ant-design/icons";
import { tokens } from "@/lib/design-tokens";

const MORE_ITEMS = [
  { href: "/finance", icon: <DollarOutlined />, label: "财务管理", desc: "收款、支出、按房号分账" },
  { href: "/settlements", icon: <DollarOutlined />, label: "业主结算", desc: "月度分成与结算单" },
  { href: "/tasks", icon: <CheckSquareOutlined />, label: "运营任务", desc: "保洁派单、待办跟踪" },
  { href: "/guests", icon: <TeamOutlined />, label: "客人档案", desc: "客源与回头客" },
  // 通知中心入口隐藏(2026-07-07 拍板):后端没有任何写入站内通知的代码,页面永远空列表,
  // 告警实际走飞书。/notifications 路由保留,若日后接站内通知生产者,恢复此行 + 顶栏铃铛即可。
  // { href: "/notifications", icon: <BellOutlined />, label: "通知中心", desc: "系统与业务通知" },
  { href: "/system/owners", icon: <TeamOutlined />, label: "业主管理", desc: "档案 · 房间关联 · 分成" },
  { href: "/system/users", icon: <TeamOutlined />, label: "账号管理", desc: "员工账号与权限" },
  { href: "/system/audit", icon: <FileTextOutlined />, label: "审计日志", desc: "操作记录" },
  { href: "/settings", icon: <SettingOutlined />, label: "系统设置", desc: "基础配置" },
];

export default function MorePage() {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <h1 style={{ fontSize: 20, fontWeight: 600, color: tokens.color.text.primary }}>
        更多功能
      </h1>
      <div
        style={{
          background: tokens.color.bg.container,
          border: `1px solid ${tokens.color.bg.border}`,
          borderRadius: tokens.radius.lg,
          overflow: "hidden",
        }}
      >
        {MORE_ITEMS.map(({ href, icon, label, desc }, idx) => (
          <Link
            key={href}
            href={href}
            style={{
              display: "flex",
              alignItems: "center",
              gap: 12,
              padding: "14px 16px",
              borderTop: idx === 0 ? "none" : `1px solid ${tokens.color.bg.borderSubtle}`,
              color: tokens.color.text.primary,
              minHeight: 56,
            }}
          >
            <div
              style={{
                width: 40,
                height: 40,
                flex: "0 0 40px",
                borderRadius: "50%",
                background: tokens.color.brand.primarySoft,
                color: tokens.color.brand.primary,
                display: "inline-flex",
                alignItems: "center",
                justifyContent: "center",
                fontSize: 18,
              }}
            >
              {icon}
            </div>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontSize: 14, fontWeight: 500, lineHeight: 1.3 }}>{label}</div>
              <div
                style={{
                  fontSize: 12,
                  color: tokens.color.text.tertiary,
                  marginTop: 2,
                }}
              >
                {desc}
              </div>
            </div>
            <RightOutlined
              style={{ fontSize: 12, color: tokens.color.text.tertiary, flex: "0 0 auto" }}
            />
          </Link>
        ))}
      </div>
    </div>
  );
}
