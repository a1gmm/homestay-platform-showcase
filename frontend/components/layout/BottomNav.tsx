"use client";

import { usePathname, useRouter } from "next/navigation";
import { AppstoreOutlined, PlusOutlined } from "@ant-design/icons";
import { tokens } from "@/lib/design-tokens";
import { useAuthStore } from "@/lib/auth";
import { NAV_ENTRIES, ROLE_NAV, FALLBACK_NAV, type NavKey } from "@/lib/nav-config";
import { requestAppNavigation } from "@/lib/app-navigation";

// 导航入口/角色布局单一来源见 lib/nav-config；底栏与侧边栏集合由 nav-config.test 守卫一致。

export function BottomNav() {
  const pathname = usePathname();
  const router = useRouter();
  const role = useAuthStore((s) => s.user?.role);

  const nav = (role && ROLE_NAV[role]) || FALLBACK_NAV;
  const primaryKeys = [...nav.left, ...nav.right];

  const isActive = (key: string) => pathname === key || pathname.startsWith(key + "/");

  const navigate = (href: string) => requestAppNavigation(href, () => router.push(href));
  const NavItemEl = ({ entryKey }: { entryKey: NavKey }) => {
    const item = NAV_ENTRIES[entryKey];
    const active = isActive(item.key);
    return (
      <div
        role="button"
        tabIndex={0}
        data-navigation-href={item.key}
        onClick={() => navigate(item.key)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            navigate(item.key);
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
        {primaryKeys.map((k) => (
          <NavItemEl key={k} entryKey={k} />
        ))}

        {nav.more.length > 0 && (
          <div
            role="button"
            tabIndex={0}
            aria-label="全部功能"
            data-navigation-href="/more"
            onClick={() => navigate("/more")}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                navigate("/more");
              }
            }}
            style={{
              flex: 1,
              display: "flex",
              flexDirection: "column",
              alignItems: "center",
              justifyContent: "center",
              cursor: "pointer",
              color: pathname === "/more" ? tokens.color.brand.primary : tokens.color.text.secondary,
              padding: "6px 0",
              gap: 3,
              position: "relative",
            }}
          >
            {pathname === "/more" && (
              <span style={{ position: "absolute", top: 0, width: 28, height: 2, borderRadius: 1, background: tokens.color.brand.primary }} />
            )}
            <span style={{ fontSize: 20, lineHeight: 1 }}><AppstoreOutlined /></span>
            <span style={{ fontSize: 10, lineHeight: 1.2, fontWeight: 500 }}>全部</span>
          </div>
        )}

        {/* 开单是独立业务动作，浮在底栏右上方，不再挤占或打乱任务入口。 */}
        {nav.fab && (
          <button
            aria-label="开单"
            data-navigation-href="/rooms?action=new"
            onClick={() => navigate("/rooms?action=new")}
            style={{
              position: "absolute",
              top: -52,
              right: 14,
              width: tokens.layout.fabSize,
              height: tokens.layout.fabSize,
              borderRadius: 999,
              background: tokens.anyu.color.ink.default,
              border: `2px solid ${tokens.anyu.color.shell}`,
              color: tokens.anyu.color.shell,
              display: "inline-flex",
              flexDirection: "column",
              alignItems: "center",
              justifyContent: "center",
              gap: 1,
              fontSize: 19,
              cursor: "pointer",
              boxShadow: tokens.shadow.sm,
              zIndex: 2,
            }}
          >
            <PlusOutlined />
            <span style={{ fontSize: 10, lineHeight: 1 }}>开单</span>
          </button>
        )}
      </div>
    </nav>
  );
}
