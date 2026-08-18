"use client";

import { usePathname, useRouter } from "next/navigation";
import { useState } from "react";
import { Dropdown } from "antd";
import type { MenuProps } from "antd";
import { AppstoreOutlined, PlusOutlined } from "@ant-design/icons";
import { tokens } from "@/lib/design-tokens";
import { useAuthStore } from "@/lib/auth";
import { NAV_ENTRIES, ROLE_NAV, FALLBACK_NAV, type NavKey } from "@/lib/nav-config";

// 导航入口/角色布局单一来源见 lib/nav-config；底栏与侧边栏集合由 nav-config.test 守卫一致。

export function BottomNav() {
  const pathname = usePathname();
  const router = useRouter();
  const role = useAuthStore((s) => s.user?.role);
  const [moreOpen, setMoreOpen] = useState(false);

  const nav = (role && ROLE_NAV[role]) || FALLBACK_NAV;

  const isActive = (key: string) => pathname === key || pathname.startsWith(key + "/");

  const moreItems: MenuProps["items"] = nav.more.map((k) => ({
    key: NAV_ENTRIES[k].key,
    label: NAV_ENTRIES[k].full,
    icon: NAV_ENTRIES[k].icon,
  }));

  const handleMoreClick: MenuProps["onClick"] = ({ key }) => {
    router.push(key);
    setMoreOpen(false);
  };

  const NavItemEl = ({ entryKey }: { entryKey: NavKey }) => {
    const item = NAV_ENTRIES[entryKey];
    const active = isActive(item.key);
    return (
      <div
        role="button"
        tabIndex={0}
        onClick={() => router.push(item.key)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            router.push(item.key);
          }
        }}
        style={{
          flex: 1,
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          cursor: "pointer",
          color: active ? tokens.color.brand.primary : tokens.color.text.secondary,
          gap: 3,
          padding: "6px 0",
          transition: "color 120ms ease",
          position: "relative",
        }}
      >
        {active && (
          <span
            style={{
              position: "absolute",
              top: 0,
              width: 28,
              height: 2,
              borderRadius: 1,
              background: tokens.color.brand.primary,
            }}
          />
        )}
        <span style={{ fontSize: 20, lineHeight: 1 }}>{item.icon}</span>
        <span style={{ fontSize: 10, lineHeight: 1.2, fontWeight: 500 }}>{item.label}</span>
      </div>
    );
  };

  return (
    <nav
      className="bottom-nav-container"
      style={{
        position: "fixed",
        bottom: 0,
        left: 0,
        right: 0,
        zIndex: 1000,
        background: tokens.color.bg.container,
        borderTop: `1px solid ${tokens.color.bg.border}`,
        paddingBottom: "env(safe-area-inset-bottom, 0px)",
        boxShadow: "0 -4px 16px rgba(16,24,40,.05)",
      }}
    >
      <div
        style={{
          height: 60,
          display: "flex",
          alignItems: "stretch",
          position: "relative",
        }}
      >
        {nav.left.map((k) => (
          <NavItemEl key={k} entryKey={k} />
        ))}

        {/* FAB - 开单（前台第一动作）。对称布局下此格居中，按钮再 translateX(-50%) 锚定屏幕正中。 */}
        {nav.fab && (
          <div
            style={{
              flex: `0 0 ${tokens.layout.fabSize + 16}px`,
              position: "relative",
            }}
          >
            <button
              aria-label="开单"
              onClick={() => router.push("/rooms?action=new")}
              style={{
                position: "absolute",
                top: -20,
                left: "50%",
                transform: "translateX(-50%)",
                width: tokens.layout.fabSize,
                height: tokens.layout.fabSize,
                borderRadius: 999,
                background: `linear-gradient(135deg, ${tokens.color.brand.primary}, #6366F1)`,
                border: "none",
                color: "#fff",
                display: "inline-flex",
                alignItems: "center",
                justifyContent: "center",
                fontSize: 22,
                cursor: "pointer",
                boxShadow: "0 8px 20px rgba(46,92,255,.35), 0 2px 6px rgba(46,92,255,.18)",
                transition: "transform 120ms ease",
              }}
              onTouchStart={(e) => ((e.currentTarget as HTMLButtonElement).style.transform = "translateX(-50%) scale(0.94)")}
              onTouchEnd={(e) => ((e.currentTarget as HTMLButtonElement).style.transform = "translateX(-50%) scale(1)")}
            >
              <PlusOutlined />
            </button>
            {/* 文字标签：说明加号是「开单」 */}
            <span
              style={{
                position: "absolute",
                bottom: 6,
                left: "50%",
                transform: "translateX(-50%)",
                fontSize: 10,
                lineHeight: 1.2,
                fontWeight: 500,
                color: tokens.color.brand.primary,
                whiteSpace: "nowrap",
              }}
            >
              开单
            </span>
          </div>
        )}

        {nav.right.map((k) => (
          <NavItemEl key={k} entryKey={k} />
        ))}

        {nav.more.length > 0 && (
          <Dropdown
            menu={{ items: moreItems, onClick: handleMoreClick }}
            placement="topRight"
            trigger={["click"]}
            open={moreOpen}
            onOpenChange={setMoreOpen}
          >
            <div
              role="button"
              tabIndex={0}
              style={{
                flex: 1,
                display: "flex",
                flexDirection: "column",
                alignItems: "center",
                justifyContent: "center",
                cursor: "pointer",
                color: moreOpen ? tokens.color.brand.primary : tokens.color.text.secondary,
                padding: "6px 0",
                gap: 3,
              }}
            >
              <span style={{ fontSize: 20, lineHeight: 1 }}>
                <AppstoreOutlined />
              </span>
              <span style={{ fontSize: 10, lineHeight: 1.2, fontWeight: 500 }}>更多</span>
            </div>
          </Dropdown>
        )}
      </div>
    </nav>
  );
}
