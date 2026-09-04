"use client";

import { useState } from "react";
import { Button } from "antd";
import { ArrowLeftOutlined } from "@ant-design/icons";
import { useRouter } from "next/navigation";
import { safeMonthlyCloseReturnTarget } from "@/lib/monthly-close";

function safeReturnTarget(): string | null {
  if (typeof window === "undefined") return null;
  return safeMonthlyCloseReturnTarget(window.location.search, window.location.origin);
}

export function ReturnToMonthlyClose() {
  const router = useRouter();
  const [returnTo] = useState(safeReturnTarget);
  if (!returnTo) return null;
  return (
    <Button aria-label="返回月结中心" icon={<ArrowLeftOutlined />} onClick={() => router.push(returnTo)}>
      返回月结中心
    </Button>
  );
}
