"use client";

import { useState, useEffect } from "react";
import { extractErrorMessage } from "@/lib/api-errors";
import { useRouter } from "next/navigation";
import { Form, Input, Button, Tabs, message, Modal } from "antd";
import { useQueryClient } from "@tanstack/react-query";
import {
  UserOutlined, LockOutlined, ArrowRightOutlined,
  MobileOutlined, SafetyCertificateOutlined,
} from "@ant-design/icons";
import { authApi, unifiedAuthApi, handoffApi, type UnifiedIdentity, type HandoffPayload } from "@/lib/api";
import { useAuthStore } from "@/lib/auth";
import { useNewOrderStore } from "@/lib/new-order-store";
import { useStaffStore } from "@/lib/staff-store";
import { useOwnerStore } from "@/lib/owner-store";
import { useCustomerStore } from "@/lib/customer-store";
import { tokens } from "@/lib/design-tokens";
import { useIsMobile } from "@/lib/responsive";

export default function LoginPage() {
  const router = useRouter();
  const isMobile = useIsMobile();
  const queryClient = useQueryClient();
  const { setAuth } = useAuthStore();
  const setStaffAuth = useStaffStore((s) => s.setAuth);
  const setOwnerAuth = useOwnerStore((s) => s.setAuth);
  const setCustomerAuth = useCustomerStore((s) => s.setAuth);

  const [pwError, setPwError] = useState("");
  const [pwLoading, setPwLoading] = useState(false);

  const [phone, setPhone] = useState("");
  const [code, setCode] = useState("");
  const [sending, setSending] = useState(false);
  const [verifying, setVerifying] = useState(false);
  const [cooldown, setCooldown] = useState(0);

  const [identities, setIdentities] = useState<UnifiedIdentity[]>([]);

  // 跨子域交接:优先换一次性 code,目标子域用 code 换回 token,token 不进 URL (#46)。
  // 降级:换码失败时(如后端 Redis 不可用 → 503)退回「URL 携带 token」的老方式,
  // 保证登录永远不会因为 Redis 抖动而瘫痪。accept 页同时支持 code 与 at/rt 两种入参。
  const handoffRedirect = async (acceptOrigin: string, payload: HandoffPayload) => {
    const target = new URL(`${acceptOrigin}/auth/accept`);
    try {
      const { data } = await handoffApi.create(payload);
      target.searchParams.set("code", data.code);
    } catch {
      // fail-open 降级:把 token 直接放进 URL(安全性回退到老方式,但登录可用)
      target.searchParams.set("at", payload.at);
      if (payload.rt) target.searchParams.set("rt", payload.rt);
      if (payload.kind) target.searchParams.set("kind", payload.kind);
      if (payload.uid) target.searchParams.set("uid", payload.uid);
      if (payload.role) target.searchParams.set("role", payload.role);
      if (payload.name) target.searchParams.set("name", payload.name);
      if (payload.next) target.searchParams.set("next", payload.next);
    }
    window.location.href = target.toString();
  };

  // 进入登录页时主动清掉任何残留认证态 + react-query 缓存。
  // 防御性清理：哪怕上游 logout 路径漏清了，登录页也会重置到 clean state，
  // 避免 stale query 401 触发硬跳转打断用户在 tab / 表单上的操作。
  useEffect(() => {
    if (typeof window === "undefined") return;
    localStorage.removeItem("access_token");
    localStorage.removeItem("refresh_token");
    localStorage.removeItem("last_activity_at");
    queryClient.clear();
    // 新订单提示状态同属「残留态」：admin 域上登出→登录全程软跳转不刷页面，
    // 模块级 store 会带给下一个人（上一班关了提示音，下一班静音上岗且无从知晓）。
    useNewOrderStore.getState().reset();
  }, [queryClient]);

  useEffect(() => {
    if (cooldown <= 0) return;
    const t = setTimeout(() => setCooldown((c) => c - 1), 1000);
    return () => clearTimeout(t);
  }, [cooldown]);

  // 进入 staff 端口(保洁/管家)。staff 页面只在 C 端(www)提供,middleware 会把
  // admin 子域上的 /staff/* 重定向到 www;localStorage 是按 origin 隔离的,所以在
  // admin 子域登录后直接 push("/staff/*") 会跨域跳到 www,而 www 的 staff store 是空的,
  // 于是被 /staff/cleaner 判定未登录 → 踢回 /staff/login(手机号验证码页)。
  // 解法与后台登录一致:把 token 交接到 www 的 /auth/accept(kind=staff),由它在 www 写入 store。
  const enterStaffPortal = (
    u: { user_id: string; display_name: string; role: string },
    token: string
  ) => {
    const next = u.role === "keeper" ? "/staff/keeper" : "/staff/cleaner";
    if (typeof window !== "undefined" && window.location.host.startsWith("admin.")) {
      void handoffRedirect("http://localhost:3000", {
        at: token, kind: "staff", uid: u.user_id, role: u.role, name: u.display_name, next,
      });
      return;
    }
    // 已在 C 端(www / vercel.app / 本地),同源直接写 store 并跳转。
    setStaffAuth({ user_id: u.user_id, display_name: u.display_name, role: u.role }, token);
    router.push(next);
  };

  const onPwFinish = async (values: { username: string; password: string }) => {
    setPwLoading(true);
    setPwError("");
    try {
      const res = await authApi.login(values.username, values.password);
      const { access_token, refresh_token, user_id, role, display_name } = res.data;

      // 现场角色(保洁/管家)走各自 staff 端口,不进管理后台。
      // 之前这里不分角色一律 push("/dashboard"),保洁用账号密码登录后会落到管理后台:
      // 既看到订单/新建订单等越权入口,又拿不到自己的"开始打扫/提交完工"操作。
      // OTP 登录的 pickIdentity 早已按角色分流,这里与它对齐。
      if (!["admin", "operator", "finance"].includes(role)) {
        enterStaffPortal({ user_id, display_name, role }, access_token);
        return;
      }

      // Cross-subdomain handoff (matches pickIdentity admin path): if we're on the
      // root brand domain, the token must be written into admin.* via /auth/accept —
      // localStorage is origin-bound so a same-tab redirect to admin.* would land on
      // an empty store and bounce back to /login. OTP login already handles this;
      // password login was missing it, hence "first attempt fails, second succeeds".
      if (typeof window !== "undefined" && !window.location.host.startsWith("admin.")) {
        await handoffRedirect("http://localhost:3000", {
          at: access_token, rt: refresh_token, uid: user_id, role, name: display_name, next: "/dashboard",
        });
        return;
      }

      // Already on admin.*: write locally and navigate.
      setAuth({ user_id, role, display_name }, access_token, refresh_token);
      router.push("/dashboard");
    } catch (e: any) {
      setPwError(extractErrorMessage(e, "登录失败,请检查用户名和密码"));
    } finally {
      setPwLoading(false);
    }
  };

  const sendOtp = async () => {
    if (!/^1[3-9]\d{9}$/.test(phone)) {
      message.error("请输入正确的手机号");
      return;
    }
    setSending(true);
    try {
      await unifiedAuthApi.sendOtp(phone);
      message.success("验证码已发送 (演示期可用 888888)");
      setCooldown(60);
    } catch (e: any) {
      message.error(extractErrorMessage(e, "发送失败"));
    } finally {
      setSending(false);
    }
  };

  const pickIdentity = (id: UnifiedIdentity) => {
    if (id.kind === "user") {
      const isAdminLike = id.role && ["admin", "operator", "finance"].includes(id.role);
      if (isAdminLike) {
        // admin 后台跨子域: www 域写到 admin 域的 accept 页,admin 域直接跳
        if (typeof window !== "undefined" && window.location.host.startsWith("admin.")) {
          setAuth(
            { user_id: id.user_id || "", role: id.role || "admin", display_name: id.display_name || "" },
            id.access_token,
            id.refresh_token || ""
          );
          router.push("/dashboard");
        } else {
          void handoffRedirect("http://localhost:3000", {
            at: id.access_token, rt: id.refresh_token || "", uid: id.user_id || "",
            role: id.role || "admin", name: id.display_name || "", next: "/dashboard",
          });
        }
      } else {
        // 与密码登录同一套跨子域交接逻辑(在 admin 子域登录时把 token 交接到 www)。
        enterStaffPortal(
          { user_id: id.user_id || "", display_name: id.display_name || "", role: id.role || "" },
          id.access_token
        );
      }
    } else if (id.kind === "owner") {
      setOwnerAuth(
        {
          owner_id: id.owner_id || "",
          name: id.name || "",
          phone,
          room_count: 0,
        } as any,
        id.access_token
      );
      router.push("/owner");
    } else {
      setCustomerAuth(
        {
          phone,
          name: id.name || null,
          id_number: null,
          created_at: new Date().toISOString(),
        } as any,
        id.access_token
      );
      router.push("/booking");
    }
  };

  const verify = async () => {
    if (!code || code.length < 4) {
      message.error("请输入验证码");
      return;
    }
    setVerifying(true);
    try {
      const { data } = await unifiedAuthApi.verify(phone, code);
      if (data.identities.length === 0) {
        message.error("未找到账号,请联系管理员");
      } else if (data.identities.length === 1) {
        pickIdentity(data.identities[0]);
      } else {
        setIdentities(data.identities);
      }
    } catch (e: any) {
      message.error(extractErrorMessage(e, "验证失败"));
    } finally {
      setVerifying(false);
    }
  };

  return (
    <div
      style={{
        minHeight: "100dvh",
        display: "flex",
        background: tokens.color.bg.page,
      }}
    >
      {/* Brand panel — 仅桌面端显示。移动端隐藏整块品牌分屏，表单单栏铺满。 */}
      {!isMobile && (
      <div
        style={{
          flex: "1 1 58%",
          position: "relative",
          background: "#2B2721",
          color: "#FBF8F1",
          padding: "56px 64px",
          display: "flex",
          flexDirection: "column",
          justifyContent: "space-between",
          overflow: "hidden",
        }}
        className="login-brand"
      >
        <div
          aria-hidden
          style={{
            position: "absolute",
            inset: 0,
            backgroundImage:
              "linear-gradient(rgba(251, 248, 241, .04) 1px, transparent 1px), linear-gradient(90deg, rgba(251, 248, 241, .04) 1px, transparent 1px)",
            backgroundSize: "48px 48px",
            maskImage: "linear-gradient(180deg, rgba(0,0,0,.7), transparent 85%)",
            WebkitMaskImage: "linear-gradient(180deg, rgba(0,0,0,.7), transparent 85%)",
            pointerEvents: "none",
          }}
        />

        <div style={{ position: "relative", display: "flex", alignItems: "baseline", gap: 14 }}>
          <div
            className="serif"
            style={{
              width: 36, height: 36, borderRadius: "50%",
              background: "rgba(251, 248, 241, 0.08)",
              border: "0.5px solid rgba(251, 248, 241, 0.2)",
              display: "inline-flex", alignItems: "center", justifyContent: "center",
              fontWeight: 400, fontSize: 17, letterSpacing: 0,
              alignSelf: "center",
            }}
          >
            成
          </div>
          <div>
            <div className="serif" style={{ fontSize: 18, fontWeight: 400, letterSpacing: 0, lineHeight: 1.2 }}>
              成家民宿
            </div>
          </div>
        </div>

        <div style={{ position: "relative" }}>
          <div style={{ fontSize: 12, color: "#A89680", marginBottom: 24 }}>
            为每个角色设计
          </div>
          <h1
            className="serif"
            style={{
              fontSize: 48, fontWeight: 400, margin: 0, lineHeight: 1.25,
              letterSpacing: 0,
            }}
          >
            一个手机号,
            <br />
            <span style={{ color: "#A89680" }}>自动认出你是谁。</span>
          </h1>
          <p
            style={{
              marginTop: 24, fontSize: 14, color: "#A89680", lineHeight: 1.9, maxWidth: 460,
            }}
          >
            管理员 · 管家 · 保洁 · 业主 · 客人 —— 统一入口,各进各的视图,无需记住不同链接。
          </p>
        </div>

        <div style={{ position: "relative", fontSize: 12, color: "#6B665B" }}>
          © {new Date().getFullYear()} 成家民宿
        </div>
      </div>
      )}

      {/* Form panel */}
      <div
        style={{
          flex: 1,
          display: "flex",
          alignItems: isMobile ? "flex-start" : "center",
          justifyContent: "center",
          padding: isMobile ? "48px 20px 32px" : "32px 24px",
        }}
      >
        <div style={{ width: "100%", maxWidth: 380 }}>
          {/* 移动端品牌头(桌面端品牌在左栏) */}
          {isMobile && (
            <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 28 }}>
              <div
                className="serif"
                style={{
                  width: 32, height: 32, borderRadius: "50%",
                  background: tokens.color.brand.primary, color: "#FBF8F1",
                  display: "inline-flex", alignItems: "center", justifyContent: "center",
                  fontSize: 15,
                }}
                aria-hidden
              >
                成
              </div>
              <span className="serif" style={{ fontSize: 17, color: tokens.color.text.primary }}>
                成家民宿
              </span>
            </div>
          )}
          <div style={{ marginBottom: 24 }}>
            <h2
              style={{
                margin: 0, fontSize: 28, fontWeight: 600,
                color: tokens.color.text.primary, letterSpacing: "-.01em",
              }}
            >
              欢迎回来
            </h2>
            <div style={{ marginTop: 8, fontSize: 14, color: tokens.color.text.secondary }}>
              输入手机号,自动识别身份
            </div>
          </div>

          <Tabs
            defaultActiveKey="otp"
            items={[
              {
                key: "otp",
                label: "手机号登录",
                children: (
                  <div>
                    <div style={{ marginBottom: 16 }}>
                      <div
                        style={{ fontSize: 13, color: tokens.color.text.secondary, marginBottom: 6 }}
                      >
                        手机号
                      </div>
                      <Input
                        size="large"
                        prefix={<MobileOutlined style={{ color: tokens.color.text.tertiary }} />}
                        placeholder="请输入手机号"
                        value={phone}
                        inputMode="tel"
                        maxLength={11}
                        onChange={(e) => setPhone(e.target.value.replace(/\D/g, ""))}
                      />
                    </div>
                    <div style={{ marginBottom: 20 }}>
                      <div
                        style={{ fontSize: 13, color: tokens.color.text.secondary, marginBottom: 6 }}
                      >
                        验证码
                      </div>
                      <div style={{ display: "flex", gap: 8 }}>
                        <Input
                          size="large"
                          prefix={
                            <SafetyCertificateOutlined style={{ color: tokens.color.text.tertiary }} />
                          }
                          placeholder="6 位验证码"
                          value={code}
                          inputMode="numeric"
                          maxLength={6}
                          onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))}
                        />
                        <Button
                          size="large"
                          onClick={sendOtp}
                          loading={sending}
                          disabled={cooldown > 0}
                          style={{ minWidth: 110 }}
                        >
                          {cooldown > 0 ? `${cooldown}s 后重发` : "获取验证码"}
                        </Button>
                      </div>
                    </div>
                    <Button
                      type="primary"
                      size="large"
                      block
                      loading={verifying}
                      disabled={!phone || !code}
                      onClick={verify}
                      style={{ height: 44, fontWeight: 600, fontSize: 15 }}
                    >
                      登录 / 注册 <ArrowRightOutlined style={{ fontSize: 12, marginLeft: 4 }} />
                    </Button>
                    <div
                      style={{
                        marginTop: 16,
                        padding: 10,
                        background: tokens.color.bg.subtle,
                        borderRadius: tokens.radius.md,
                        fontSize: 11,
                        color: tokens.color.text.secondary,
                        lineHeight: 1.6,
                      }}
                    >
                      演示期验证码 <b style={{ color: tokens.color.text.primary }}>888888</b>。一个手机号对应多个身份时登录后弹窗选择。
                    </div>
                  </div>
                ),
              },
              {
                key: "pw",
                label: "账号密码",
                children: (
                  <div>
                    {pwError && (
                      <div
                        style={{
                          background: tokens.color.status.warnSoft,
                          border: `1px solid rgba(239,68,68,.22)`,
                          color: "#991B1B",
                          padding: "10px 12px",
                          borderRadius: tokens.radius.md,
                          fontSize: 13,
                          marginBottom: 16,
                        }}
                        role="alert"
                      >
                        {pwError}
                      </div>
                    )}
                    <Form
                      name="login-pw"
                      onFinish={onPwFinish}
                      size="large"
                      autoComplete="off"
                      layout="vertical"
                      requiredMark={false}
                    >
                      <Form.Item
                        name="username"
                        label="用户名"
                        rules={[{ required: true, message: "请输入用户名" }]}
                      >
                        <Input
                          prefix={<UserOutlined style={{ color: tokens.color.text.tertiary }} />}
                          placeholder="请输入用户名"
                          autoComplete="username"
                        />
                      </Form.Item>
                      <Form.Item
                        name="password"
                        label="密码"
                        rules={[{ required: true, message: "请输入密码" }]}
                      >
                        <Input.Password
                          prefix={<LockOutlined style={{ color: tokens.color.text.tertiary }} />}
                          placeholder="输入密码"
                          autoComplete="current-password"
                        />
                      </Form.Item>
                      <Form.Item style={{ marginBottom: 0 }}>
                        <Button
                          type="primary"
                          htmlType="submit"
                          loading={pwLoading}
                          block
                          style={{ height: 44, fontWeight: 600, fontSize: 15 }}
                        >
                          登录 <ArrowRightOutlined style={{ fontSize: 12, marginLeft: 4 }} />
                        </Button>
                      </Form.Item>
                    </Form>
                    <div
                      style={{
                        marginTop: 16,
                        padding: 10,
                        background: tokens.color.bg.subtle,
                        borderRadius: tokens.radius.md,
                        fontSize: 11,
                        color: tokens.color.text.secondary,
                        lineHeight: 1.6,
                      }}
                    >
                      使用运营分配的账号密码登录；如忘记密码，请联系系统管理员重置。
                    </div>
                  </div>
                ),
              },
            ]}
          />
        </div>
      </div>

      {/* 多身份选择 Modal */}
      <Modal
        open={identities.length > 1}
        onCancel={() => setIdentities([])}
        title="您有多个身份,请选择要进入的视图"
        footer={null}
        centered
        width={420}
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 10, marginTop: 12 }}>
          {identities.map((id, i) => (
            <div
              key={i}
              onClick={() => {
                setIdentities([]);
                pickIdentity(id);
              }}
              style={{
                padding: 16,
                borderRadius: 10,
                border: `1px solid ${tokens.color.bg.border}`,
                cursor: "pointer",
                transition: "all .15s",
                background: "#fff",
              }}
              onMouseEnter={(e) => {
                e.currentTarget.style.borderColor = tokens.color.brand.primary;
                e.currentTarget.style.background = tokens.color.brand.primarySoft;
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.borderColor = tokens.color.bg.border;
                e.currentTarget.style.background = "#fff";
              }}
            >
              <div
                style={{
                  fontSize: 15, fontWeight: 600, color: tokens.color.text.primary,
                }}
              >
                {id.label || id.name || id.kind}
              </div>
              {id.subtitle && (
                <div
                  style={{
                    fontSize: 12, color: tokens.color.text.secondary, marginTop: 4,
                  }}
                >
                  {id.subtitle}
                </div>
              )}
            </div>
          ))}
        </div>
      </Modal>
    </div>
  );
}
