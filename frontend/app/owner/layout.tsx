"use client";

import { usePathname, useRouter } from "next/navigation";
import {
  HomeOutlined,
  FileDoneOutlined,
  UserOutlined,
  CalendarOutlined,
} from "@ant-design/icons";
import { useIsDesktop } from "@/lib/responsive";
import { useOwnerStore } from "@/lib/owner-store";

const NAV = [
  { key: "/owner", label: "首页", icon: <HomeOutlined /> },
  { key: "/owner/calendar", label: "房态", icon: <CalendarOutlined /> },
  { key: "/owner/settlements", label: "结算", icon: <FileDoneOutlined /> },
  { key: "/owner/me", label: "我的", icon: <UserOutlined /> },
];

// 桌面版内容最大宽度：宽屏不铺满，两侧留白，信息不至于拉太散。
const DESKTOP_MAX_WIDTH = 1240;

export default function OwnerLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const isDesktop = useIsDesktop();
  const owner = useOwnerStore((s) => s.owner);

  const isLogin = pathname?.startsWith("/owner/login");

  // 移动端底部导航：只在 4 个主页面显示（房间详情 / 登录不显示）。
  const showTabs =
    pathname === "/owner" ||
    pathname === "/owner/calendar" ||
    pathname === "/owner/settlements" ||
    pathname === "/owner/me";

  // ── 桌面版：左侧固定侧栏 shell（登录页除外）──────────────────────────────
  if (isDesktop && !isLogin) {
    return (
      <div style={{ minHeight: "100dvh", background: "var(--shell)", display: "flex" }}>
        <DesktopSidebar
          pathname={pathname || ""}
          onNav={(k) => router.push(k)}
          ownerName={owner?.name}
          isMaster={!!owner?.is_master}
          roomCount={owner?.room_count}
          floorCount={owner?.sub_owners?.length}
        />
        <main style={{ flex: 1, minWidth: 0, height: "100dvh", overflowY: "auto" }}>
          <div style={{ maxWidth: DESKTOP_MAX_WIDTH, margin: "0 auto" }}>{children}</div>
        </main>
      </div>
    );
  }

  // ── 移动端：单列 + 底部导航（沿用原有体验，不动）────────────────────────
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
          {NAV.map((t) => {
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

// 桌面侧栏：品牌 + 4 项导航 + 底部业主身份。样式对齐岸屿（sand 底 / ink 选中 pill）。
function DesktopSidebar({
  pathname,
  onNav,
  ownerName,
  isMaster,
  roomCount,
  floorCount,
}: {
  pathname: string;
  onNav: (key: string) => void;
  ownerName?: string;
  isMaster: boolean;
  roomCount?: number;
  floorCount?: number;
}) {
  const identity = isMaster
    ? `总账号 · ${floorCount ?? 0} 层 ${roomCount ?? 0} 套`
    : `业主 · ${roomCount ?? 0} 套`;

  return (
    <aside
      style={{
        width: 212,
        flex: "0 0 212px",
        height: "100dvh",
        position: "sticky",
        top: 0,
        background: "var(--sand)",
        borderRight: "0.5px solid var(--linen)",
        display: "flex",
        flexDirection: "column",
        padding: "22px 14px",
      }}
    >
      <div style={{ padding: "4px 10px 22px" }}>
        <div className="serif" style={{ fontSize: 19, color: "var(--ink)", letterSpacing: 0 }}>
          成家民宿
        </div>
        <div style={{ fontSize: 9, letterSpacing: "0.24em", color: "var(--driftwood)", marginTop: 3 }}>
          CHÉNG JIĀ MÍN SÙ
        </div>
      </div>

      <nav style={{ display: "flex", flexDirection: "column", gap: 2 }}>
        {NAV.map((t) => {
          const active = pathname === t.key;
          return (
            <button
              key={t.key}
              type="button"
              onClick={() => onNav(t.key)}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 11,
                padding: "11px 12px",
                borderRadius: 10,
                border: "none",
                background: active ? "var(--ink)" : "transparent",
                color: active ? "var(--shell)" : "var(--stone)",
                fontSize: 14,
                letterSpacing: 0,
                cursor: "pointer",
                textAlign: "left",
                minHeight: 44,
                transition: "background 200ms var(--ease-breath)",
              }}
            >
              <span style={{ fontSize: 16, opacity: active ? 1 : 0.75, display: "flex" }}>{t.icon}</span>
              {t.label}
            </button>
          );
        })}
      </nav>

      <div
        style={{
          marginTop: "auto",
          padding: "12px 10px 2px",
          borderTop: "0.5px solid var(--linen)",
          display: "flex",
          alignItems: "center",
          gap: 10,
        }}
      >
        <div
          className="serif"
          style={{
            width: 34,
            height: 34,
            flex: "0 0 34px",
            borderRadius: "50%",
            background: "var(--ink)",
            color: "var(--shell)",
            fontSize: 16,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
          }}
        >
          {(ownerName || "业主").slice(0, 1)}
        </div>
        <div style={{ minWidth: 0 }}>
          <div
            style={{
              fontSize: 13,
              color: "var(--ink)",
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {ownerName || "业主"}
          </div>
          <div style={{ fontSize: 11, color: "var(--driftwood)" }}>{identity}</div>
        </div>
      </div>
    </aside>
  );
}
