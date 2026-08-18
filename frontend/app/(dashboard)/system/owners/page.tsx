"use client";

/**
 * 旧路由兼容 — 业主管理已合并到 /system/share-config?tab=owners。
 * 保留此文件无声 redirect,防止外部链接 / 浏览器书签失效。
 */
import { useEffect } from "react";
import { useRouter } from "next/navigation";

export default function OwnersRedirect() {
  const router = useRouter();
  useEffect(() => {
    router.replace("/system/share-config?tab=owners");
  }, [router]);
  return null;
}
