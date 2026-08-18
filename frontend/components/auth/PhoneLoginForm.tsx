"use client";

import { useState, useEffect, type ReactNode } from "react";
import { Button, Input, message, Segmented } from "antd";
import { LeftOutlined } from "@ant-design/icons";
import { extractErrorMessage } from "@/lib/api-errors";

export type LoginMode = "password" | "otp";

export interface PhoneLoginFormProps {
  title: string;
  /** 副标题：owner 会随 mode 变化，故传函数；单模式页忽略入参即可。 */
  subtitle: (mode: LoginMode) => string;
  onBack: () => void;
  /** 发送验证码（内部已校验手机号格式后调用）。 */
  sendOtp: (phone: string) => Promise<void>;
  /** 验证码登录成功链路（页面内做 setAuth + 成功提示 + 跳转）。name 仅 nameField 时透传。 */
  onOtpVerify: (phone: string, code: string, name?: string) => Promise<void>;
  /** 传入即启用「密码登录」——出现 Segmented，默认停在密码 tab（owner）。 */
  passwordLogin?: (username: string, password: string) => Promise<void>;
  /** 采集可选姓名（首次登录用，仅 booking C 端）。 */
  nameField?: boolean;
  /** 验证码发送成功提示（staff 带演示码提示），默认「验证码已发送」。 */
  otpSentMessage?: string;
  /** 「收不到验证码？」展开项文案（staff/owner 有，booking 无）。 */
  helpItems?: string[];
  /** 底部说明（用户协议 / 账号由管理员创建 等）。 */
  footer?: ReactNode;
}

const PHONE_RE = /^1[3-9]\d{9}$/;
const USERNAME_RE = /^[A-Za-z0-9_.]{4,50}$/;

/** 手机验证码 / 密码 登录表单的共享外壳。
 *  staff·booking·owner 三个登录页原为近乎复制的三份，收敛到此；
 *  各页只注入 API + setAuth + 跳转（关键鉴权链路仍留在页面，最小化误配风险）。
 *  /login 总登录页是独立超集（品牌分栏 + 多身份 + 跨子域交接），不并入。 */
export function PhoneLoginForm({
  title,
  subtitle,
  onBack,
  sendOtp,
  onOtpVerify,
  passwordLogin,
  nameField,
  otpSentMessage = "验证码已发送",
  helpItems,
  footer,
}: PhoneLoginFormProps) {
  const [mode, setMode] = useState<LoginMode>(passwordLogin ? "password" : "otp");
  const [phone, setPhone] = useState("");
  const [code, setCode] = useState("");
  const [name, setName] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [sending, setSending] = useState(false);
  const [verifying, setVerifying] = useState(false);
  const [cooldown, setCooldown] = useState(0);

  useEffect(() => {
    if (cooldown <= 0) return;
    const t = setTimeout(() => setCooldown((c) => c - 1), 1000);
    return () => clearTimeout(t);
  }, [cooldown]);

  const handleSendOtp = async () => {
    if (!PHONE_RE.test(phone)) {
      message.error("请输入正确的手机号");
      return;
    }
    setSending(true);
    try {
      await sendOtp(phone);
      message.success(otpSentMessage);
      setCooldown(60);
    } catch (e: any) {
      message.error(extractErrorMessage(e, "发送失败，请稍后重试"));
    } finally {
      setSending(false);
    }
  };

  const submit = async () => {
    setVerifying(true);
    try {
      if (mode === "password" && passwordLogin) {
        if (!USERNAME_RE.test(username)) {
          message.error("请输入正确的账号（4-50 位字母、数字、下划线、点）");
          return;
        }
        if (!password || password.length < 6) {
          message.error("请输入密码");
          return;
        }
        await passwordLogin(username.trim(), password);
      } else {
        if (!PHONE_RE.test(phone)) {
          message.error("请输入正确的手机号");
          return;
        }
        if (!code || code.length < 4) {
          message.error("请输入验证码");
          return;
        }
        await onOtpVerify(phone, code, nameField ? name || undefined : undefined);
      }
    } catch (e: any) {
      message.error(extractErrorMessage(e, "登录失败"));
    } finally {
      setVerifying(false);
    }
  };

  const submitDisabled =
    mode === "password" ? !username || !password : !phone || !code;

  return (
    <div style={{ minHeight: "100dvh", background: "#fff", padding: 20 }}>
      <button
        onClick={onBack}
        style={{
          background: "transparent",
          border: "none",
          fontSize: 18,
          color: "#333",
          padding: 8,
          marginLeft: -8,
          cursor: "pointer",
        }}
        aria-label="返回"
      >
        <LeftOutlined />
      </button>

      <div style={{ marginTop: 20, marginBottom: passwordLogin ? 24 : 32 }}>
        <div style={{ fontSize: 24, fontWeight: 700, marginBottom: 8 }}>{title}</div>
        <div style={{ fontSize: 14, color: "#888" }}>{subtitle(mode)}</div>
      </div>

      {passwordLogin && (
        <Segmented
          block
          size="large"
          value={mode}
          onChange={(v) => setMode(v as LoginMode)}
          options={[
            { value: "password", label: "密码登录" },
            { value: "otp", label: "验证码登录" },
          ]}
          style={{ marginBottom: 20 }}
        />
      )}

      {mode === "password" && passwordLogin ? (
        <>
          <div style={{ marginBottom: 20 }}>
            <div style={{ fontSize: 13, color: "#666", marginBottom: 6 }}>账号</div>
            <Input
              size="large"
              placeholder="请输入账号"
              value={username}
              autoComplete="username"
              maxLength={50}
              onChange={(e) => setUsername(e.target.value.replace(/\s/g, ""))}
            />
          </div>
          <div style={{ marginBottom: 32 }}>
            <div style={{ fontSize: 13, color: "#666", marginBottom: 6 }}>密码</div>
            <Input.Password
              size="large"
              placeholder="请输入密码"
              value={password}
              autoComplete="current-password"
              maxLength={64}
              onChange={(e) => setPassword(e.target.value)}
              onPressEnter={submit}
            />
          </div>
        </>
      ) : (
        <>
          <div style={{ marginBottom: 20 }}>
            <div style={{ fontSize: 13, color: "#666", marginBottom: 6 }}>手机号</div>
            <Input
              size="large"
              placeholder="请输入手机号"
              value={phone}
              inputMode="tel"
              maxLength={11}
              onChange={(e) => setPhone(e.target.value.replace(/\D/g, ""))}
            />
          </div>
          <div style={{ marginBottom: nameField ? 20 : 32 }}>
            <div style={{ fontSize: 13, color: "#666", marginBottom: 6 }}>验证码</div>
            <div style={{ display: "flex", gap: 8 }}>
              <Input
                size="large"
                placeholder="6 位验证码"
                value={code}
                inputMode="numeric"
                maxLength={6}
                onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))}
              />
              <Button
                size="large"
                onClick={handleSendOtp}
                loading={sending}
                disabled={cooldown > 0}
                style={{ minWidth: 110 }}
              >
                {cooldown > 0 ? `${cooldown}s 后重发` : "获取验证码"}
              </Button>
            </div>
          </div>
          {nameField && (
            <div style={{ marginBottom: 32 }}>
              <div style={{ fontSize: 13, color: "#666", marginBottom: 6 }}>
                姓名 <span style={{ color: "#bbb" }}>（可选，首次登录使用）</span>
              </div>
              <Input
                size="large"
                placeholder="称呼"
                value={name}
                maxLength={20}
                onChange={(e) => setName(e.target.value)}
              />
            </div>
          )}
        </>
      )}

      <Button
        type="primary"
        size="large"
        block
        loading={verifying}
        onClick={submit}
        disabled={submitDisabled}
      >
        登录
      </Button>

      {helpItems && helpItems.length > 0 && (
        <details style={{ marginTop: 20, fontSize: 12, color: "#888" }}>
          <summary
            style={{
              cursor: "pointer",
              textAlign: "center",
              listStyle: "none",
              minHeight: 44,
              lineHeight: "44px",
            }}
          >
            收不到验证码？
          </summary>
          <div style={{ padding: "4px 12px 0", lineHeight: 1.8 }}>
            {helpItems.map((item, i) => (
              <div key={i}>· {item}</div>
            ))}
          </div>
        </details>
      )}

      {footer && (
        <div
          style={{
            marginTop: helpItems && helpItems.length > 0 ? 8 : 24,
            fontSize: 12,
            color: "#aaa",
            textAlign: "center",
          }}
        >
          {footer}
        </div>
      )}
    </div>
  );
}
