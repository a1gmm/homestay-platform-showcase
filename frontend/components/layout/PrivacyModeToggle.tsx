"use client";

import { Button, Tag } from "antd";
import { EyeInvisibleOutlined, EyeOutlined, SafetyCertificateOutlined } from "@ant-design/icons";

import { tokens } from "@/lib/design-tokens";


export function PrivacyModeToggle({
  active,
  compact = false,
  onToggle,
}: {
  active: boolean;
  compact?: boolean;
  onToggle: () => void;
}) {
  const label = active ? "关闭隐私演示模式" : "开启隐私演示模式";
  return (
    <Button
      type={active ? "primary" : "text"}
      icon={active ? <EyeOutlined /> : <EyeInvisibleOutlined />}
      aria-label={label}
      aria-pressed={active}
      title={label}
      onClick={onToggle}
      style={{ minWidth: compact ? 40 : undefined }}
    >
      {compact ? null : active ? "退出演示" : "隐私演示"}
    </Button>
  );
}


export function PrivacyModeBanner({ active }: { active: boolean }) {
  if (!active) return null;
  return (
    <div
      role="status"
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        gap: 8,
        marginBottom: 12,
        padding: "9px 12px",
        border: `1px solid ${tokens.color.status.warn}`,
        borderRadius: tokens.radius.md,
        background: tokens.color.status.warnSoft,
        color: tokens.color.text.primary,
        fontSize: tokens.font.size.sm,
        fontWeight: 500,
      }}
    >
      <Tag color="gold" icon={<SafetyCertificateOutlined />} style={{ marginInlineEnd: 0 }}>
        隐私演示
      </Tag>
      <span>隐私演示模式已开启，敏感信息已隐藏，禁止修改与导出</span>
    </div>
  );
}
