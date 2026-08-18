"use client";

import { Button, Result } from "antd";

export default function GlobalError({
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
        padding: 24,
      }}
    >
      <Result
        status="500"
        title="系统错误"
        subTitle={error.message || "发生了意外错误"}
        extra={[
          <Button key="retry" type="primary" onClick={reset}>
            重试
          </Button>,
          <Button key="home" onClick={() => (window.location.href = "/")}>
            返回首页
          </Button>,
        ]}
      />
    </div>
  );
}
