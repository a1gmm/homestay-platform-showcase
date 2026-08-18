"use client";

import { Suspense } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { message, Spin } from "antd";
import { staffAuthApi } from "@/lib/staff-api";
import { useStaffStore } from "@/lib/staff-store";
import { PhoneLoginForm } from "@/components/auth/PhoneLoginForm";

export default function StaffLoginPage() {
  return (
    <Suspense
      fallback={
        <div style={{ minHeight: "100dvh", display: "flex", alignItems: "center", justifyContent: "center" }}>
          <Spin />
        </div>
      }
    >
      <StaffLoginInner />
    </Suspense>
  );
}

function destinationByRole(role: string): string {
  if (role === "cleaner") return "/staff/cleaner";
  return "/staff/keeper";
}

function StaffLoginInner() {
  const router = useRouter();
  const search = useSearchParams();
  const setAuth = useStaffStore((s) => s.setAuth);
  const next = search.get("next");

  return (
    <PhoneLoginForm
      title="员工登录"
      subtitle={() => "用管理员预先绑定的手机号接收验证码"}
      onBack={() => router.back()}
      otpSentMessage="验证码已发送(演示期可用 888888)"
      sendOtp={async (phone) => {
        await staffAuthApi.sendOtp(phone);
      }}
      onOtpVerify={async (phone, code) => {
        const { data } = await staffAuthApi.verify(phone, code);
        setAuth(
          { user_id: data.user_id, display_name: data.display_name, role: data.role },
          data.access_token,
        );
        message.success(`${data.display_name},欢迎回来`);
        router.replace(next || destinationByRole(data.role));
      }}
      helpItems={[
        "短信可能延迟 1-2 分钟，请稍等后再看",
        "检查手机的「骚扰拦截 / 垃圾短信」文件夹",
        "每天最多发 10 条，超了请明天再试或找管理员",
        "手机号未绑定账号也收不到——请联系店长确认绑定",
      ]}
      footer="账号由管理员创建,如未绑定请联系管理员"
    />
  );
}
