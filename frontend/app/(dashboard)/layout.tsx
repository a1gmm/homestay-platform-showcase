"use client";

import { useEffect, useState } from "react";
import { useRouter, usePathname } from "next/navigation";
import Link from "next/link";
import { Layout, Menu, Avatar, Dropdown, Space, Skeleton } from "antd";
import { useQueryClient } from "@tanstack/react-query";
import {
  LogoutOutlined,
  UserOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  LockOutlined,
  QuestionCircleOutlined,
  SoundOutlined,
  NotificationOutlined,
} from "@ant-design/icons";
import { ChangePasswordModal } from "@/components/auth/ChangePasswordModal";
import { useAuthStore } from "@/lib/auth";
import { useStaffStore } from "@/lib/staff-store";
import { useOwnerStore } from "@/lib/owner-store";
import { useCustomerStore } from "@/lib/customer-store";
import { authApi } from "@/lib/api";
import GlobalSearch from "@/components/GlobalSearch";
import { BottomNav } from "@/components/layout/BottomNav";
import NewOrderWatcher from "@/components/orders/NewOrderWatcher";
import { useNewOrderStore } from "@/lib/new-order-store";
import { useIsMobile } from "@/lib/responsive";
import { useFocusStore, shouldHideChrome } from "@/lib/focus-store";
import { tokens } from "@/lib/design-tokens";
import { NAV_ENTRIES, ROLE_LABEL, navForRole } from "@/lib/nav-config";

const { Sider, Header, Content } = Layout;

export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const { user, clearAuth } = useAuthStore();
  const clearStaff = useStaffStore((s) => s.clearAuth);
  const clearOwner = useOwnerStore((s) => s.clearAuth);
  const clearCustomer = useCustomerStore((s) => s.clearAuth);
  const queryClient = useQueryClient();
  const resetNewOrder = useNewOrderStore((s) => s.reset);
  const soundMuted = useNewOrderStore((s) => s.muted);
  const toggleSound = useNewOrderStore((s) => s.toggleMuted);
  const hydrateMuted = useNewOrderStore((s) => s.hydrateMuted);
  const [collapsed, setCollapsed] = useState(false);
  const [checkedAuth, setCheckedAuth] = useState(false);
  const [pwModalOpen, setPwModalOpen] = useState(false);
  const isMobile = useIsMobile();
  // 全屏专注模式：在 /rooms 桌面端隐藏侧栏+顶栏，把最大空间让给甘特图
  const focusMode = useFocusStore((s) => s.focusMode);
  const hideChrome = shouldHideChrome({ isMobile, pathname, focusMode });

  // 从 localStorage 恢复静音偏好。放 effect 而非 store 初值：SSR 期读不到
  // localStorage，写进初值会造成 hydration 前后不一致。
  useEffect(() => {
    hydrateMuted();
  }, [hydrateMuted]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const accessToken = localStorage.getItem("access_token");
    if (!accessToken) {
      clearAuth();
      router.push("/login");
      return;
    }
    setCheckedAuth(true);
  }, [clearAuth, router]);

  // 纵深防御:保洁/管家是现场角色,有各自的 staff 端口(/staff/cleaner、/staff/keeper)。
  // 即便他们用旧 session 或书签直接打开管理后台 /dashboard,也立即弹回对应端口,
  // 从根上杜绝订单管理 / 新建订单等越权入口(移动端 BottomNav 不做角色门控)。
  useEffect(() => {
    if (!user) return;
    if (user.role === "cleaner" || user.role === "keeper") {
      router.replace(user.role === "keeper" ? "/staff/keeper" : "/staff/cleaner");
    }
  }, [user, router]);

  const handleLogout = async () => {
    await authApi.logout().catch(() => {});
    // 清掉所有可能残留的认证态：admin / staff / owner / customer + react-query 缓存。
    // 不清 queryClient.cache 时，之前 dashboard 页面拉过的 query 会留在内存（gcTime 30min），
    // 下次再访问时可能用旧 token / 已失效的认证态触发后台 401，进而触发 axios 拦截器的硬跳转
    // (window.location.href = '/login')，把用户在登录页的操作（如点 tab）打断。
    clearAuth();
    clearStaff();
    clearOwner();
    clearCustomer();
    queryClient.clear();
    // 新订单提示状态也要清：admin.* 上「登出→登录」全程是 router.push 软跳转，不刷页面，
    // 模块级 store 会原封不动带给下一个登录的人。前台换班就是这个场景——上一班关了
    // 提示音，下一班会静音上岗且毫不知情（系统里没有取消静音的入口）。
    resetNewOrder();
    router.push("/login");
  };

  if (!checkedAuth || !user) {
    return (
      <div style={{ padding: 40 }}>
        <Skeleton active paragraph={{ rows: 6 }} />
      </div>
    );
  }

  // 角色→菜单单一来源见 lib/nav-config（桌面顺序 = navForRole，与移动底栏集合由测试守卫一致）。
  const navItems = navForRole(user.role).map((k) => NAV_ENTRIES[k]);

  const selectedKey =
    navItems.find((item) => pathname === item.key || pathname.startsWith(item.key + "/"))?.key ||
    "/dashboard";

  const userMenuItems = [
    {
      key: "user-info",
      label: (
        <div style={{ padding: "4px 4px", lineHeight: 1.4, minWidth: 160 }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: tokens.color.text.primary }}>
            {user.display_name}
          </div>
          <div style={{ fontSize: 11, color: tokens.color.text.tertiary }}>
            {ROLE_LABEL[user.role] ?? user.role}
          </div>
        </div>
      ),
      disabled: true,
    },
    { type: "divider" as const },
    {
      key: "change-password",
      icon: <LockOutlined />,
      label: "修改密码",
      onClick: () => setPwModalOpen(true),
    },
    { type: "divider" as const },
    { key: "logout", icon: <LogoutOutlined />, label: "退出登录", danger: true, onClick: handleLogout },
  ];

  const SIDEBAR_WIDTH = tokens.layout.sidebarWidth;

  return (
    <>
    {/* 新订单到店提示（仅 operator 生效，内部自带角色闸与 contextHolder）。
        必须渲染在上面 `if (!checkedAuth || !user)` 鉴权闸之后：做成子组件而非
        在本函数体内起 hook，否则要么 hooks 顺序违规，要么未登录时起 30s 打 401 的死循环。 */}
    <NewOrderWatcher />
    <Layout style={{ minHeight: "100dvh", background: tokens.color.bg.page }}>
      {/* 侧边栏桌面专属。移动端导航完全交给底部 BottomNav + 顶栏，
          避免「汉堡抽屉侧边栏」与「底部导航」两套并行入口的冗余。 */}
      {!isMobile && !hideChrome && (
      <Sider
        collapsible
        collapsed={collapsed}
        onCollapse={setCollapsed}
        trigger={null}
        breakpoint="lg"
        collapsedWidth={tokens.layout.sidebarCollapsedWidth}
        width={SIDEBAR_WIDTH}
        style={{
          background: tokens.color.bg.container,
          borderRight: `1px solid ${tokens.color.bg.border}`,
          position: "sticky",
          top: 0,
          height: "100dvh",
          overflow: "auto",
        }}
      >
        {/* Brand */}
        <div
          style={{
            height: tokens.layout.headerHeight,
            display: "flex",
            alignItems: "center",
            gap: 10,
            padding: collapsed ? 0 : "0 16px",
            justifyContent: collapsed ? "center" : "flex-start",
            borderBottom: `1px solid ${tokens.color.bg.border}`,
          }}
        >
          <div
            className="serif"
            style={{
              width: 28,
              height: 28,
              borderRadius: 999,
              background: tokens.anyu.color.ink.default,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              color: tokens.anyu.color.shell,
              fontWeight: 400,
              fontSize: 14,
              flex: "0 0 28px",
              letterSpacing: 0,
            }}
            aria-hidden
          >
            成
          </div>
          {!collapsed && (
            <div style={{ lineHeight: 1.2, minWidth: 0 }}>
              <div
                className="serif"
                style={{
                  fontSize: 15,
                  fontWeight: 400,
                  color: tokens.color.text.primary,
                  whiteSpace: "nowrap",
                  letterSpacing: 0,
                }}
              >
                成家民宿
              </div>
            </div>
          )}
        </div>

        <nav aria-label="主导航" style={{ padding: "8px 0" }}>
          <Menu
            mode="inline"
            selectedKeys={[selectedKey]}
            style={{ borderRight: 0, background: "transparent" }}
            items={navItems.map((item) => ({
              key: item.key,
              icon: item.icon,
              label: <Link href={item.key}>{item.full}</Link>,
            }))}
          />
        </nav>

        {!collapsed && (
          <div
            style={{
              position: "absolute",
              bottom: 12,
              left: 12,
              right: 12,
              padding: "10px 12px",
              borderRadius: tokens.radius.md,
              background: tokens.color.bg.subtle,
              fontSize: tokens.font.size.xs,
              color: tokens.color.text.tertiary,
              lineHeight: 1.5,
            }}
          >
            v1.0 · {new Date().getFullYear()} © 民宿运营
          </div>
        )}
      </Sider>
      )}

      <Layout style={{ background: tokens.color.bg.page }}>
        {!hideChrome && (
        <Header
          style={{
            position: "sticky",
            top: 0,
            zIndex: 100,
            background: tokens.color.bg.container,
            padding: isMobile ? "0 12px" : "0 20px",
            paddingTop: "env(safe-area-inset-top, 0px)",
            height: `calc(${tokens.layout.headerHeight}px + env(safe-area-inset-top, 0px))`,
            lineHeight: `${tokens.layout.headerHeight}px`,
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            borderBottom: `1px solid ${tokens.color.bg.border}`,
            boxShadow: "none",
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            {isMobile ? (
              /* 移动端无侧边栏：用品牌标识占位，导航走底部 BottomNav。 */
              <div
                className="serif"
                aria-hidden
                style={{
                  width: 32,
                  height: 32,
                  borderRadius: 999,
                  background: tokens.anyu.color.ink.default,
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  color: tokens.anyu.color.shell,
                  fontSize: 15,
                  flex: "0 0 32px",
                }}
              >
                成
              </div>
            ) : (
              <div
                role="button"
                tabIndex={0}
                aria-label="切换侧边栏"
                aria-expanded={!collapsed}
                onClick={() => setCollapsed(!collapsed)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    setCollapsed(!collapsed);
                  }
                }}
                style={{
                  width: 32,
                  height: 32,
                  display: "inline-flex",
                  alignItems: "center",
                  justifyContent: "center",
                  borderRadius: tokens.radius.md,
                  cursor: "pointer",
                  color: tokens.color.text.secondary,
                  transition: "background 200ms ease",
                }}
                onMouseEnter={(e) => ((e.currentTarget as HTMLDivElement).style.background = tokens.color.bg.hover)}
                onMouseLeave={(e) => ((e.currentTarget as HTMLDivElement).style.background = "transparent")}
              >
                {collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
              </div>
            )}
            <span className="global-search-wrapper">
              <GlobalSearch />
            </span>
          </div>

          <Space size={isMobile ? 8 : 12}>
            <div
              role="button"
              tabIndex={0}
              aria-label="使用说明"
              style={{
                width: isMobile ? 40 : 32,
                height: isMobile ? 40 : 32,
                display: "inline-flex",
                alignItems: "center",
                justifyContent: "center",
                borderRadius: tokens.radius.md,
                color: tokens.color.text.secondary,
                cursor: "pointer",
              }}
              onMouseEnter={(e) => ((e.currentTarget as HTMLDivElement).style.background = tokens.color.bg.hover)}
              onMouseLeave={(e) => ((e.currentTarget as HTMLDivElement).style.background = "transparent")}
              onClick={() => router.push("/help")}
            >
              <QuestionCircleOutlined style={{ fontSize: 16 }} />
            </div>
            {/* 新订单提示音开关。只给 operator 显示——admin/finance 本来就不会响
                （见 NewOrderWatcher 的角色闸），给他们一个点了没用的喇叭只会让人困惑。
                ⚠️ 这个图标是「静音可持久化」的前提：没有它，一键静音就成了单向阀，
                误点一次声音永久没了且无人知晓。删它之前先把 store 的持久化一并撤回。 */}
            {user.role === "operator" && (
              <div
                title={soundMuted ? "新订单提示音已关闭，点击开启" : "新订单提示音已开启，点击关闭"}
                role="button"
                aria-label={soundMuted ? "开启新订单提示音" : "关闭新订单提示音"}
                aria-pressed={soundMuted}
                style={{
                  width: 32,
                  height: 32,
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  borderRadius: tokens.radius.md,
                  color: soundMuted ? tokens.color.text.tertiary : tokens.color.text.secondary,
                  cursor: "pointer",
                }}
                onMouseEnter={(e) => ((e.currentTarget as HTMLDivElement).style.background = tokens.color.bg.hover)}
                onMouseLeave={(e) => ((e.currentTarget as HTMLDivElement).style.background = "transparent")}
                onClick={toggleSound}
              >
                {soundMuted ? (
                  <NotificationOutlined style={{ fontSize: 16, opacity: 0.45 }} />
                ) : (
                  <SoundOutlined style={{ fontSize: 16 }} />
                )}
              </div>
            )}
            {/* 顶栏通知铃铛隐藏(2026-07-07 拍板):后端没有站内通知生产者,通知中心永远空列表,
                告警实际走飞书。/notifications 路由保留,日后接生产者时恢复铃铛 + more 页入口即可。 */}
            <Dropdown menu={{ items: userMenuItems }} placement="bottomRight">
              <Space style={{ cursor: "pointer", padding: "4px 8px", borderRadius: tokens.radius.md }}>
                <Avatar
                  style={{
                    backgroundColor: tokens.color.brand.primary,
                    fontWeight: 600,
                    fontSize: 13,
                  }}
                  size={30}
                >
                  {user.display_name?.charAt(0) ?? <UserOutlined />}
                </Avatar>
                <div className="user-info-text" style={{ lineHeight: 1.25, textAlign: "left" }}>
                  <div
                    style={{
                      fontSize: 13,
                      fontWeight: 600,
                      color: tokens.color.text.primary,
                    }}
                  >
                    {user.display_name}
                  </div>
                  <div
                    style={{
                      fontSize: 11,
                      color: tokens.color.text.tertiary,
                    }}
                  >
                    {ROLE_LABEL[user.role] ?? user.role}
                  </div>
                </div>
              </Space>
            </Dropdown>
          </Space>
        </Header>
        )}

        <Content
          style={{
            margin: isMobile ? 12 : hideChrome ? 8 : 20,
            minHeight: `calc(100dvh - ${tokens.layout.headerHeight}px - ${isMobile ? 24 : 40}px)`,
            paddingBottom: isMobile ? 88 : 0,
          }}
        >
          <div className="fade-in">{children}</div>
        </Content>
        <BottomNav />
      </Layout>
    </Layout>
    <ChangePasswordModal
      open={pwModalOpen}
      onClose={() => setPwModalOpen(false)}
      onSuccess={handleLogout}
    />
    </>
  );
}
