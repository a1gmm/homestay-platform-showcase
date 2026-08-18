"use client";

import { useState, useEffect, Suspense } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { message, Spin } from "antd";
import { ownerAuthApi } from "@/lib/owner-api";
import { useOwnerStore } from "@/lib/owner-store";
import { getStoredOwnerToken, resolveOwnerLoginEntry } from "@/lib/owner-session";
import { PhoneLoginForm } from "@/components/auth/PhoneLoginForm";

export default function OwnerLoginPage() {
  return (
    <Suspense
      fallback={
        <div style={{ minHeight: "100dvh", display: "flex", alignItems: "center", justifyContent: "center" }}>
          <Spin />
        </div>
      }
    >
      <OwnerLoginInner />
    </Suspense>
  );
}

function OwnerLoginInner() {
  const router = useRouter();
  const search = useSearchParams();
  const setAuth = useOwnerStore((s) => s.setAuth);
  const next = search.get("next") || "/owner";

  // 挂载时先判断本地是否已有有效登录态：有就直接进，业主 30 天内免输账号密码。
  // checking 为 true 时只显示 Spin，避免登录表单一闪而过。
  const [checking, setChecking] = useState(true);

  useEffect(() => {
    const target = resolveOwnerLoginEntry(getStoredOwnerToken(), next);
    if (target) {
      router.replace(target);
    } else {
      setChecking(false);
    }
  }, [next, router]);

  const handleSuccess = (data: any) => {
    setAuth(data.owner, data.access_token);
    message.success(`${data.owner.name || "业主"},欢迎回来`);
    router.replace(next);
  };

  if (checking) {
    return (
      <div style={{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center" }}>
        <Spin />
      </div>
    );
  }

  return (
    <PhoneLoginForm
      title="业主登录"
      subtitle={(mode) => (mode === "password" ? "用管理员发放的账号密码登录" : "用绑定手机接收验证码")}
      onBack={() => router.back()}
      passwordLogin={async (username, password) => {
        const { data } = await ownerAuthApi.loginPassword(username, password);
        handleSuccess(data);
      }}
      sendOtp={async (phone) => {
        await ownerAuthApi.sendOtp(phone);
      }}
      onOtpVerify={async (phone, code) => {
        const { data } = await ownerAuthApi.verify(phone, code);
        handleSuccess(data);
      }}
      helpItems={[
        "短信可能延迟 1-2 分钟，请稍等后再看",
        "检查手机的「骚扰拦截 / 垃圾短信」文件夹",
        "每天最多发 10 条，超了请明天再试",
        "手机号未绑定业主账号也收不到——请联系管家确认绑定",
      ]}
      footer="账号由管理员后台创建,如未绑定请联系管理员"
    />
  );
}
