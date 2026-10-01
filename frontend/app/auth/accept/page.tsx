"use client";

import { useEffect, useRef, useState, Suspense } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Button, Spin } from "antd";
import { useAuthStore } from "@/lib/auth";
import { useStaffStore } from "@/lib/staff-store";
import { handoffApi } from "@/lib/api";
import { safeHandoffNext } from "@/lib/auth-handoff";
import { tokens } from "@/lib/design-tokens";

export default function AcceptTokenPage() {
  return (
    <Suspense
      fallback={
        <div style={{ minHeight: "100dvh", display: "flex", alignItems: "center", justifyContent: "center" }}>
          <Spin />
        </div>
      }
    >
      <AcceptTokenInner />
    </Suspense>
  );
}

function AcceptTokenInner() {
  const router = useRouter();
  const search = useSearchParams();
  // Capture before scrubbing: Next.js updates useSearchParams after replaceState.
  const [{ code, hasLegacyTokens }] = useState(() => ({
    code: search.get("code"),
    hasLegacyTokens: search.has("at") || search.has("rt"),
  }));
  const [error, setError] = useState("");
  const exchange = useRef<ReturnType<typeof handoffApi.exchange> | null>(null);
  const setAuth = useAuthStore((s) => s.setAuth);
  const setStaffAuth = useStaffStore((s) => s.setAuth);

  useEffect(() => {
    let cancelled = false;
    // Remove the entire query/hash from the current history entry before network IO.
    window.history.replaceState(window.history.state, "", window.location.pathname);

    // 仅使用后端一次性交换返回的身份；旧 URL token 不再受理。
    const apply = (d: {
      at?: string | null; rt?: string | null; kind?: string | null;
      uid?: string | null; role?: string | null; name?: string | null; next?: string | null;
    }) => {
      const at = d.at || "";
      const uid = d.uid || "";
      const role = d.role || "admin";
      const name = d.name || "管理员";
      const fallback = d.kind === "staff"
        ? (role === "keeper" ? "/staff/keeper" : "/staff/cleaner")
        : "/dashboard";
      const next = safeHandoffNext(d.next, fallback);
      if (!at) {
        setError("登录信息不完整，请返回登录页重试。");
        return;
      }
      if (d.kind === "staff") {
        // 员工端(保洁/管家):staff store 只需 access token,无 refresh token。
        setStaffAuth({ user_id: uid, display_name: name, role }, at);
        router.replace(next);
        return;
      }
      if (!d.rt) {
        setError("登录信息不完整，请返回登录页重试。");
        return;
      }
      setAuth({ user_id: uid, role, display_name: name }, at, d.rt);
      router.replace(next);
    };

    if (!code || hasLegacyTokens) {
      setError("此登录链接已失效，请返回登录页重新登录。");
      return;
    }
    (async () => {
      try {
        // React Strict Mode replays effects; reuse the same redemption request.
        exchange.current ??= handoffApi.exchange(code);
        const { data } = await exchange.current;
        if (cancelled) return;
        apply(data);
      } catch {
        if (!cancelled) setError("登录交接失败或已过期，请返回登录页重试。");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [code, hasLegacyTokens, router, setAuth, setStaffAuth]);

  return (
    <div
      style={{
        minHeight: "100dvh",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        flexDirection: "column",
        gap: 12,
      }}
    >
      {error ? (
        <>
          <div role="alert" style={{ color: tokens.color.text.secondary, fontSize: 14 }}>{error}</div>
          <Button type="primary" size="large" onClick={() => router.replace("/login")}>返回登录</Button>
        </>
      ) : (
        <>
          <Spin size="large" />
          <div style={{ color: tokens.color.text.secondary, fontSize: 14 }}>登录中，请稍候…</div>
        </>
      )}
    </div>
  );
}
