"use client";

import { usePathname, useRouter } from "next/navigation";
import { HomeOutlined, AppstoreOutlined, UserOutlined } from "@ant-design/icons";
import { useStaffStore } from "@/lib/staff-store";

export default function StaffLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const user = useStaffStore((s) => s.user);

  const showTabs =
    pathname === "/staff/cleaner" ||
    pathname === "/staff/keeper" ||
    pathname === "/staff/me";

  // 保洁: 任务 + 我的
  // 其他: 今日 + 房态 + 我的
  const isCleaner = user?.role === "cleaner";

  const tabs = isCleaner
    ? [
        { key: "/staff/cleaner", label: "任务", icon: <HomeOutlined /> },
        { key: "/staff/me", label: "我的", icon: <UserOutlined /> },
      ]
    : [
        { key: "/staff/keeper", label: "今日", icon: <HomeOutlined /> },
        { key: "/staff/keeper?tab=rooms", label: "房态", icon: <AppstoreOutlined /> },
        { key: "/staff/me", label: "我的", icon: <UserOutlined /> },
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
            const keyPath = t.key.split("?")[0];
            const active = pathname === keyPath;
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
