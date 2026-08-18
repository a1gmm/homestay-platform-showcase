"use client";

import { Suspense } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { message, Spin } from "antd";
import { customerAuthApi } from "@/lib/customer-api";
import { useCustomerStore } from "@/lib/customer-store";
import { PhoneLoginForm } from "@/components/auth/PhoneLoginForm";

export default function CustomerLoginPage() {
  return (
    <Suspense fallback={<PageFallback />}>
      <CustomerLoginInner />
    </Suspense>
  );
}

function PageFallback() {
  return (
    <div style={{ minHeight: "100dvh", display: "flex", alignItems: "center", justifyContent: "center" }}>
      <Spin />
    </div>
  );
}

function CustomerLoginInner() {
  const router = useRouter();
  const search = useSearchParams();
  const setAuth = useCustomerStore((s) => s.setAuth);
  const next = search.get("next") || "/booking";

  return (
    <PhoneLoginForm
      title="登录 / 注册"
      subtitle={() => "使用手机号接收验证码，即可完成登录"}
      onBack={() => router.back()}
      nameField
      sendOtp={async (phone) => {
        await customerAuthApi.sendOtp(phone);
      }}
      onOtpVerify={async (phone, code, name) => {
        const { data } = await customerAuthApi.verify(phone, code, name);
        setAuth(data.customer, data.access_token);
        message.success("登录成功");
        router.replace(next);
      }}
      footer="登录即代表您同意我们的《用户协议》和《隐私政策》"
    />
  );
}
