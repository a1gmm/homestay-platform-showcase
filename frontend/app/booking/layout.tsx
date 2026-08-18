"use client";

import { usePathname, useRouter } from "next/navigation";
import { HomeOutlined, FileTextOutlined, UserOutlined } from "@ant-design/icons";

export default function BookingLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();

  const showTabs =
    pathname === "/booking" ||
    pathname === "/booking/orders" ||
    pathname === "/booking/me";

  const tabs = [
    { key: "/booking", label: "首页", icon: <HomeOutlined /> },
    { key: "/booking/orders", label: "订单", icon: <FileTextOutlined /> },
    { key: "/booking/me", label: "我的", icon: <UserOutlined /> },
  ];

  return (
    <div
      style={{
        minHeight: "100dvh",
        background: "var(--shell)",
        paddingBottom: showTabs ? 68 : 0,
      }}
    >
      {children}

      {showTabs && (
        <nav
          style={{
            position: "fixed",
            left: 0,
            right: 0,
            bottom: 0,
            height: 56,
            display: "flex",
            background: "var(--shell)",
            borderTop: "0.5px solid var(--linen)",
            paddingBottom: "env(safe-area-inset-bottom)",
            zIndex: 100,
          }}
        >
          {tabs.map((t) => {
            const active = pathname === t.key;
            return (
              <button
                key={t.key}
                onClick={() => router.push(t.key)}
                style={{
                  flex: 1,
                  border: "none",
                  background: "transparent",
                  color: active ? "var(--ink)" : "var(--driftwood)",
                  fontSize: 11,
                  letterSpacing: 0,
                  display: "flex",
                  flexDirection: "column",
                  alignItems: "center",
                  justifyContent: "center",
                  gap: 4,
                  cursor: "pointer",
                }}
              >
                <span style={{ fontSize: 20 }}>{t.icon}</span>
                {t.label}
              </button>
            );
          })}
        </nav>
      )}
    </div>
  );
}
