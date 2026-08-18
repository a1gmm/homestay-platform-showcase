"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { Button, message } from "antd";
import { PhoneOutlined, UserOutlined, LogoutOutlined } from "@ant-design/icons";
import { useCustomerStore } from "@/lib/customer-store";

export default function CustomerMePage() {
  const router = useRouter();
  const customer = useCustomerStore((s) => s.customer);
  const isLoggedIn = useCustomerStore((s) => s.isLoggedIn());
  const clearAuth = useCustomerStore((s) => s.clearAuth);

  useEffect(() => {
    if (!isLoggedIn) {
      router.replace(`/booking/login?next=${encodeURIComponent("/booking/me")}`);
    }
  }, [isLoggedIn, router]);

  if (!isLoggedIn || !customer) return null;

  const logout = () => {
    clearAuth();
    message.success("已退出登录");
    router.replace("/booking");
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
          {(customer.name || customer.phone).slice(0, 1)}
        </div>
        <div className="serif" style={{ fontSize: 22, fontWeight: 400, letterSpacing: 0 }}>
          {customer.name || "未设置昵称"}
        </div>
        <div style={{ fontSize: 12, color: "#A89680", marginTop: 4 }}>{customer.phone}</div>
      </div>

      <div style={{ padding: 16 }}>
        <div style={{ background: "#F5F1EA", border: "0.5px solid #E5DDCB", borderRadius: 12, overflow: "hidden" }}>
          <MeRow icon={<UserOutlined />} label="姓名" value={customer.name || "未设置"} />
          <MeRow icon={<PhoneOutlined />} label="手机号" value={customer.phone} />
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

function MeRow({ icon, label, value }: { icon: React.ReactNode; label: string; value: string }) {
  return (
    <div
      style={{
        padding: "14px 16px",
        display: "flex",
        alignItems: "center",
        gap: 12,
        borderBottom: "0.5px solid #E5DDCB",
      }}
    >
      <span style={{ color: "#2B2721", fontSize: 15 }}>{icon}</span>
      <span style={{ fontSize: 13, color: "#5C5547" }}>{label}</span>
      <span style={{ marginLeft: "auto", fontSize: 13, color: "#2B2721" }}>{value}</span>
    </div>
  );
}
