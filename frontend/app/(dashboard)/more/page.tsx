"use client";

import Link from "next/link";
import { RightOutlined } from "@ant-design/icons";
import { PageHeader } from "@/components/ui/PageHeader";
import { useAuthStore } from "@/lib/auth";
import { allFeaturesForRole } from "@/lib/nav-config";
import { tokens } from "@/lib/design-tokens";

export default function MorePage() {
  const role = useAuthStore((state) => state.user?.role);
  const groups = allFeaturesForRole(role);

  return (
    <div style={{ maxWidth: 880, margin: "0 auto" }}>
      <PageHeader
        title="全部功能"
        subtitle="按日常工作分组展示；这里只会出现当前账号有权限使用的入口。"
      />

      <div style={{ display: "flex", flexDirection: "column", gap: 24 }}>
        {groups.map((group) => (
          <section key={group.key} aria-labelledby={`feature-group-${group.key}`}>
            <h2
              id={`feature-group-${group.key}`}
              className="serif"
              style={{
                margin: "0 0 10px",
                fontSize: tokens.font.size.lg,
                fontWeight: tokens.font.weight.medium,
                letterSpacing: 0,
                color: tokens.color.text.primary,
              }}
            >
              {group.label}
            </h2>
            <div
              style={{
                background: tokens.color.bg.container,
                border: `1px solid ${tokens.color.bg.border}`,
                borderRadius: tokens.radius.lg,
                overflow: "hidden",
              }}
            >
              {group.entries.map((entry, index) => (
                <Link
                  key={entry.key}
                  href={entry.key}
                  aria-label={`${entry.full}：${entry.description}`}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 12,
                    padding: "14px 16px",
                    borderTop: index === 0 ? "none" : `1px solid ${tokens.color.bg.borderSubtle}`,
                    color: tokens.color.text.primary,
                    minHeight: 64,
                  }}
                >
                  <div
                    style={{
                      width: 40,
                      height: 40,
                      flex: "0 0 40px",
                      borderRadius: tokens.radius.full,
                      background: tokens.color.brand.primarySoft,
                      color: tokens.color.brand.primary,
                      display: "inline-flex",
                      alignItems: "center",
                      justifyContent: "center",
                      fontSize: 18,
                    }}
                  >
                    {entry.icon}
                  </div>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: tokens.font.size.base, fontWeight: tokens.font.weight.medium, lineHeight: 1.3 }}>
                      {entry.full}
                    </div>
                    <div style={{ fontSize: tokens.font.size.xs, color: tokens.color.text.tertiary, marginTop: 3, lineHeight: 1.5 }}>
                      {entry.description}
                    </div>
                  </div>
                  <RightOutlined style={{ fontSize: 12, color: tokens.color.text.tertiary, flex: "0 0 auto" }} />
                </Link>
              ))}
            </div>
          </section>
        ))}
      </div>
    </div>
  );
}
