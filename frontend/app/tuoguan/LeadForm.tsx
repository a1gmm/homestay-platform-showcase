"use client";

import { useState } from "react";
import Image from "next/image";
import { useSubmitHostingLead, PHONE_RE } from "@/hooks/useHostingLead";
import { extractErrorMessage } from "@/lib/api";
import { TUOGUAN } from "./config";

const inputStyle: React.CSSProperties = {
  width: "100%",
  height: 48,
  fontSize: 16, // ≥16px 防 iOS 自动缩放
  padding: "0 14px",
  border: "1px solid #D9D9D9",
  borderRadius: 8,
  boxSizing: "border-box",
  // 不去掉 outline——键盘用户需要焦点指示（浏览器默认 focus ring）
};

// 微信聊天里复制的手机号常带 +86 和空格（如 "+86 138 0013 8000"）。
// 不能用 maxLength（浏览器会先截断粘贴文本再触发 onChange），在这里规整。
function normalizePhoneInput(raw: string): string {
  let digits = raw.replace(/\D/g, "");
  if (digits.startsWith("86") && digits.length > 11) digits = digits.slice(2);
  return digits.slice(0, 11);
}

export default function LeadForm() {
  const [name, setName] = useState("");
  const [phone, setPhone] = useState("");
  const [location, setLocation] = useState("");
  const [error, setError] = useState("");
  const mutation = useSubmitHostingLead();

  const submitted = mutation.isSuccess;

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    mutation.reset();
    setError("");
    if (!name.trim()) return setError("请填写您的称呼");
    if (!PHONE_RE.test(phone)) return setError("请填写正确的手机号");
    if (!location.trim()) return setError("请填写房源位置（如：灵山湾 ××小区）");
    mutation.mutate(
      { name: name.trim(), phone, property_location: location.trim() },
      {
        // 后端 detail（如 429 的「请求过于频繁」）透传给用户，比统一文案更可操作
        onError: (err) => setError(extractErrorMessage(err) || "提交失败，请稍后重试或直接拨打电话"),
      }
    );
  }

  if (submitted) {
    const dup = mutation.data?.status === "already_registered";
    return (
      <div style={{ textAlign: "center", padding: "8px 0" }}>
        <p style={{ fontSize: 18, fontWeight: 600, color: TUOGUAN.navy, margin: "0 0 4px" }}>
          {dup ? "您已登记过，我们会尽快联系您" : "登记成功！"}
        </p>
        <p style={{ fontSize: 14, color: "#666", margin: "0 0 16px" }}>
          顾问将在 24 小时内与您联系。添加微信，沟通更快：
        </p>
        <Image
          src={TUOGUAN.wechatQrSrc}
          alt="观海居民宿客服微信二维码"
          width={240}
          height={355}
          unoptimized
          style={{ borderRadius: 8, maxWidth: "100%", height: "auto" }}
        />
      </div>
    );
  }

  return (
    <>
    <form onSubmit={handleSubmit} style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <input
        style={inputStyle}
        aria-label="您的称呼"
        placeholder="您的称呼（如：张先生）"
        value={name}
        maxLength={50}
        onChange={(e) => setName(e.target.value)}
      />
      <input
        style={inputStyle}
        aria-label="手机号"
        placeholder="手机号"
        value={phone}
        inputMode="tel"
        onChange={(e) => setPhone(normalizePhoneInput(e.target.value))}
      />
      <input
        style={inputStyle}
        aria-label="房源位置"
        placeholder="房源位置（如：灵山湾 ××小区）"
        value={location}
        maxLength={200}
        onChange={(e) => setLocation(e.target.value)}
      />
      {error && <p role="alert" style={{ color: "#D4380D", fontSize: 14, margin: 0 }}>{error}</p>}
      <button
        type="submit"
        disabled={mutation.isPending}
        style={{
          height: 50,
          fontSize: 17,
          fontWeight: 600,
          color: "#fff",
          background: mutation.isPending ? "#F7A600AA" : TUOGUAN.orange,
          border: "none",
          borderRadius: 8,
          cursor: mutation.isPending ? "not-allowed" : "pointer",
        }}
      >
        {mutation.isPending ? "提交中…" : "免费评估我的房子"}
      </button>
      <p style={{ fontSize: 12, color: "#999", margin: 0, textAlign: "center" }}>
        提交即视为同意顾问通过电话/微信与您联系
      </p>
      <p style={{ fontSize: 13, color: "#888", margin: "8px 0 0", textAlign: "center", userSelect: "all" }}>
        也可直接致电：{TUOGUAN.phone}（微信内请长按复制）
      </p>
    </form>
    {/* 不想填表单的业主可直接扫码加微信——微信沟通是主转化路径，常驻可见 */}
    <div
      style={{
        borderTop: "1px dashed #E0E0E0",
        marginTop: 20,
        paddingTop: 18,
        textAlign: "center",
      }}
    >
      <p style={{ fontSize: 15, fontWeight: 600, color: TUOGUAN.navy, margin: "0 0 4px" }}>
        或直接扫码加微信咨询
      </p>
      <p style={{ fontSize: 13, color: "#888", margin: "0 0 12px" }}>
        不想填表单？长按识别二维码，直接和顾问聊
      </p>
      <Image
        src={TUOGUAN.wechatQrSrc}
        alt="观海居民宿客服微信二维码"
        width={190}
        height={281}
        unoptimized
        style={{ borderRadius: 8, maxWidth: "100%", height: "auto" }}
      />
    </div>
    </>
  );
}
