"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Tag } from "antd";
import {
  PieChartOutlined,
  TeamOutlined,
  FileSearchOutlined,
  RightOutlined,
} from "@ant-design/icons";
import { useAuthStore } from "@/lib/auth";
import { PageHeader } from "@/components/ui/PageHeader";
import { useIsMobile } from "@/lib/responsive";
import { tokens } from "@/lib/design-tokens";

// 系统管理里真正能操作的三件事。描述全部用「进去能干嘛」的大白话，
// 不再罗列后端接口 —— 使用者只需要知道点进去做什么。
const ENTRIES: {
  href: string;
  title: string;
  desc: string;
  icon: React.ReactNode;
  accent: string;
  accentSoft: string;
}[] = [
  {
    href: "/system/share-config",
    title: "业主与分成",
    desc: "设置每间房归哪位业主，配置三类订单的分成比例和费用占比。改完立即生效，影响下一次结算。",
    icon: <PieChartOutlined />,
    accent: tokens.color.status.active,
    accentSoft: tokens.color.status.activeSoft,
  },
  {
    href: "/system/users",
    title: "账号管理",
    desc: "为员工、保洁、财务、业主开通登录账号，重置密码，或临时禁用离职人员的账号。",
    icon: <TeamOutlined />,
    accent: tokens.color.status.pending,
    accentSoft: tokens.color.status.pendingSoft,
  },
  {
    href: "/system/audit",
    title: "审计日志",
    desc: "查看谁在什么时间做了哪些关键操作（改价、退款、改分成等），用于事后追溯和对账。",
    icon: <FileSearchOutlined />,
    accent: tokens.color.status.info,
    accentSoft: tokens.color.status.infoSoft,
  },
];

function EntryCard({
  entry,
  isMobile,
}: {
  entry: (typeof ENTRIES)[number];
  isMobile: boolean;
}) {
  const [hover, setHover] = useState(false);
  return (
    <Link
      href={entry.href}
      style={{ textDecoration: "none", display: "block", height: "100%" }}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
    >
      <div
        style={{
          height: "100%",
          background: "#fff",
          border: `1px solid ${tokens.color.bg.border}`,
          borderRadius: 16,
          padding: isMobile ? 18 : 24,
          display: "flex",
          flexDirection: "column",
          gap: 14,
          cursor: "pointer",
          transition: "transform .18s ease, box-shadow .18s ease, border-color .18s ease",
          transform: hover && !isMobile ? "translateY(-3px)" : "translateY(0)",
          boxShadow: hover
            ? "0 8px 24px rgba(43,39,33,0.10)"
            : "0 2px 8px rgba(43,39,33,0.05)",
          borderColor: hover ? entry.accent : tokens.color.bg.border,
        }}
      >
        <div
          style={{
            width: 48,
            height: 48,
            borderRadius: 12,
            background: entry.accentSoft,
            color: entry.accent,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            fontSize: 22,
          }}
        >
          {entry.icon}
        </div>
        <div
          style={{
            fontSize: tokens.font.size.xl,
            fontWeight: tokens.font.weight.semibold,
            color: tokens.color.text.primary,
          }}
        >
          {entry.title}
        </div>
        <div
          style={{
            flex: 1,
            fontSize: tokens.font.size.base,
            color: tokens.color.text.secondary,
            lineHeight: 1.65,
          }}
        >
          {entry.desc}
        </div>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 4,
            fontSize: tokens.font.size.sm,
            fontWeight: tokens.font.weight.medium,
            color: hover ? entry.accent : tokens.color.text.tertiary,
            transition: "color .18s ease",
          }}
        >
          进入 <RightOutlined style={{ fontSize: 11 }} />
        </div>
      </div>
    </Link>
  );
}

export default function SystemPage() {
  const { user } = useAuthStore();
  const router = useRouter();
  const isMobile = useIsMobile();

  useEffect(() => {
    if (user && user.role !== "admin") {
      router.replace("/dashboard");
    }
  }, [user, router]);

  if (!user || user.role !== "admin") return null;

  return (
    <div style={{ maxWidth: 960, margin: "0 auto" }}>
      <PageHeader
        title="系统管理"
        subtitle="配置业主分成、管理登录账号、查看操作日志。这些设置只对管理员开放，改动会影响结算和其他人的使用。"
      />

      <div
        style={{
          display: "grid",
          gridTemplateColumns: isMobile
            ? "1fr"
            : "repeat(auto-fit, minmax(260px, 1fr))",
          gap: 16,
          marginBottom: 24,
        }}
      >
        {ENTRIES.map((entry) => (
          <EntryCard key={entry.href} entry={entry} isMobile={isMobile} />
        ))}
      </div>

      <div
        style={{
          background: tokens.color.bg.subtle,
          border: `1px solid ${tokens.color.bg.border}`,
          borderRadius: 16,
          padding: isMobile ? 18 : 20,
          display: "flex",
          flexWrap: "wrap",
          alignItems: "center",
          gap: 12,
        }}
      >
        <span
          style={{
            fontSize: tokens.font.size.md,
            fontWeight: tokens.font.weight.semibold,
            color: tokens.color.text.primary,
          }}
        >
          当前账号
        </span>
        <span
          style={{ fontSize: tokens.font.size.base, color: tokens.color.text.secondary }}
        >
          {user.display_name || "管理员"}
        </span>
        <Tag color="red" style={{ marginInlineEnd: 0 }}>
          管理员
        </Tag>
        <span
          style={{
            marginLeft: "auto",
            fontSize: tokens.font.size.sm,
            color: tokens.color.text.tertiary,
          }}
        >
          成家民宿运营系统 · v1.0
        </span>
      </div>
    </div>
  );
}
