"use client";

import { Button, Result } from "antd";
import { useRouter } from "next/navigation";

export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  const router = useRouter();

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
          <Button key="home" onClick={() => router.push("/")}>
            返回首页
          </Button>,
        ]}
      />
    </div>
  );
}
