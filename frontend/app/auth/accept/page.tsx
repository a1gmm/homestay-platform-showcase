"use client";

import { useEffect, Suspense } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Spin } from "antd";
import { useAuthStore } from "@/lib/auth";
import { useStaffStore } from "@/lib/staff-store";
import { handoffApi } from "@/lib/api";

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
  const setAuth = useAuthStore((s) => s.setAuth);
  const setStaffAuth = useStaffStore((s) => s.setAuth);

  useEffect(() => {
    let cancelled = false;

    // 把交接来的身份写入对应 store 并跳转。code 路径与 URL 降级路径共用此逻辑。
    const apply = (d: {
      at?: string | null; rt?: string | null; kind?: string | null;
      uid?: string | null; role?: string | null; name?: string | null; next?: string | null;
    }) => {
      const at = d.at || "";
      const uid = d.uid || "";
      const role = d.role || "admin";
      const name = d.name || "管理员";
      const next = d.next || "/dashboard";
      if (!at) {
        router.replace("/login");
        return;
      }
      if (d.kind === "staff") {
        // 员工端(保洁/管家):staff store 只需 access token,无 refresh token。
        setStaffAuth({ user_id: uid, display_name: name, role }, at);
        router.replace(next || "/staff");
        return;
      }
      if (!d.rt) {
        router.replace("/login");
        return;
      }
      setAuth({ user_id: uid, role, display_name: name }, at, d.rt);
      router.replace(next);
    };

    // 优先走一次性 code(token 不进 URL,#46);没有 code 时支持降级的 at/rt 直传。
    const code = search.get("code");
    if (!code) {
      // 降级路径:Redis 不可用时登录侧会把 token 拼进 URL。
      apply({
        at: search.get("at"), rt: search.get("rt"), kind: search.get("kind"),
        uid: search.get("uid"), role: search.get("role"),
        name: search.get("name"), next: search.get("next"),
      });
      return;
    }
    (async () => {
      try {
        const { data } = await handoffApi.exchange(code);
        if (cancelled) return;
        apply(data);
      } catch {
        if (!cancelled) router.replace("/login");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [search, router, setAuth, setStaffAuth]);

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
      <Spin size="large" />
      <div style={{ color: "#888", fontSize: 14 }}>登录中,请稍候...</div>
    </div>
  );
}
