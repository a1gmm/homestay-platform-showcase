"use client";

import { Button, Result } from "antd";
import { useRouter } from "next/navigation";

export default function DashboardError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  const router = useRouter();

  return (
    <div style={{ padding: 40, display: "flex", justifyContent: "center" }}>
      <Result
        status="error"
        title="页面出错了"
        subTitle={error.message || "发生了意外错误，请刷新重试"}
        extra={[
          <Button key="retry" type="primary" onClick={reset}>
            重试
          </Button>,
          <Button key="home" onClick={() => router.push("/dashboard")}>
            返回首页
          </Button>,
        ]}
      />
    </div>
  );
}
