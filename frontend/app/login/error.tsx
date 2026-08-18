"use client";

import { Button, Result } from "antd";

export default function LoginError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <div
      style={{
        minHeight: "100dvh",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        background: "#f5f5f5",
      }}
    >
      <Result
        status="error"
        title="登录页加载失败"
        subTitle={error.message || "请刷新页面重试"}
        extra={
          <Button type="primary" onClick={reset}>
            重试
          </Button>
        }
      />
    </div>
  );
}
