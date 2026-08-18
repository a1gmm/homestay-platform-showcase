"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { Button, message, Tag } from "antd";
import { UserOutlined, PhoneOutlined, LogoutOutlined, IdcardOutlined } from "@ant-design/icons";
import { useStaffStore } from "@/lib/staff-store";

const ROLE_LABEL: Record<string, string> = {
  admin: "管理员", operator: "运营",
  finance: "财务", cleaner: "保洁", keeper: "管家", owner: "业主",
};

const ROLE_COLOR: Record<string, string> = {
  admin: "red", operator: "blue", finance: "gold",
  cleaner: "cyan", keeper: "green", owner: "purple",
};

export default function StaffMePage() {
  const router = useRouter();
  const user = useStaffStore((s) => s.user);
  const isLoggedIn = useStaffStore((s) => s.isLoggedIn());
  const clearAuth = useStaffStore((s) => s.clearAuth);

  useEffect(() => {
    if (!isLoggedIn) router.replace("/staff/login");
  }, [isLoggedIn, router]);

  if (!isLoggedIn || !user) return null;

  const logout = () => {
    clearAuth();
    message.success("已退出登录");
    router.replace("/staff/login");
  };

  return (
    <div>
      <div
        style={{
          padding: "32px 20px 28px",
          background: "#2B2721",
          color: "#FBF8F1",
        }}
      >
        <div
          className="serif"
          style={{
            width: 60,
            height: 60,
            borderRadius: "50%",
            background: "rgba(251, 248, 241, 0.1)",
            border: "0.5px solid rgba(251, 248, 241, 0.2)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            fontSize: 26,
            fontWeight: 400,
            marginBottom: 14,
          }}
        >
          {user.display_name.slice(0, 1)}
        </div>
        <div className="serif" style={{ fontSize: 22, fontWeight: 400, letterSpacing: 0 }}>
          {user.display_name}
        </div>
        <div style={{ fontSize: 12, color: "#A89680", marginTop: 6 }}>
          {ROLE_LABEL[user.role] || user.role}
        </div>
      </div>

      <div style={{ padding: 16 }}>
        <div style={{ background: "#F5F1EA", border: "0.5px solid #E5DDCB", borderRadius: 12, overflow: "hidden" }}>
          <Row icon={<UserOutlined />} label="姓名" value={user.display_name} />
          <Row icon={<IdcardOutlined />} label="角色" value={ROLE_LABEL[user.role] || user.role} />
          <Row icon={<PhoneOutlined />} label="员工 ID" value={user.user_id} last />
        </div>

        <Button
          block
          size="large"
          icon={<LogoutOutlined />}
          onClick={logout}
          style={{ marginTop: 24 }}
          danger
        >
          退出登录
        </Button>
      </div>
    </div>
  );
}

function Row({
  icon, label, value, last,
}: {
  icon: React.ReactNode; label: string; value: string; last?: boolean;
}) {
  return (
    <div
      style={{
        padding: "14px 16px",
        display: "flex",
        alignItems: "center",
        gap: 12,
        borderBottom: last ? "none" : "0.5px solid #E5DDCB",
      }}
    >
      <span style={{ color: "#2B2721", fontSize: 15 }}>{icon}</span>
      <span style={{ fontSize: 13, color: "#5C5547" }}>{label}</span>
      <span style={{ marginLeft: "auto", fontSize: 13, color: "#2B2721" }}>{value}</span>
    </div>
  );
}
