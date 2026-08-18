"use client";

/**
 * 业主与分成配置 — 整合页
 *
 * 取代原来分散的三个入口:
 * - /system/owners(已 redirect 到这里 ?tab=owners)
 * - /system/share-ratios(已 redirect 到这里 ?tab=rooms)
 * - /rooms 房间编辑 Modal 里的「业主/分成/费用占比」(已改只读 + 链接到这里)
 *
 * Tab 1「按业主看」承载:业主 CRUD、批量关联房间、v1 老分账规则、密码重置
 * Tab 2「按房间看」承载:三类比例 inline 编辑、批量设置、CostShareEditor 14×3 矩阵
 */
import { useEffect } from "react";
import { useRouter, useSearchParams, usePathname } from "next/navigation";
import { Tabs, Alert, Typography } from "antd";
import { useAuthStore } from "@/lib/auth";
import { PageHeader } from "@/components/ui/PageHeader";
import OwnersTab from "@/components/share-config/OwnersTab";
import RoomsTab from "@/components/share-config/RoomsTab";

const { Text } = Typography;

export default function ShareConfigPage() {
  const { user } = useAuthStore();
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const activeTab = searchParams.get("tab") === "rooms" ? "rooms" : "owners";

  useEffect(() => {
    if (user && user.role !== "admin") {
      router.replace("/dashboard");
    }
  }, [user, router]);

  if (!user || user.role !== "admin") return null;

  const handleTabChange = (key: string) => {
    // 切 Tab 时同步 URL,但保留其他 query 参数(比如 room_id 高亮)
    const params = new URLSearchParams(searchParams.toString());
    params.set("tab", key);
    // 切到非 rooms tab 时清掉 room_id(它只对 rooms tab 有意义)
    if (key !== "rooms") params.delete("room_id");
    router.replace(`${pathname}?${params.toString()}`);
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <PageHeader
        title="业主与分成"
        subtitle="管理房间业主关联、三类订单分成比例、费用占比规则。所有变更立即生效,影响下一次结算。"
      />

      <Alert
        type="info"
        showIcon
        message={
          <Text style={{ fontSize: 13 }}>
            按业主看 = 业主档案 + 一次性给业主关联多房 + 老的「分账策略」配置(三态:公司担/业主担/不计入)。
            按房间看 = 单房三类比例(普通/试住/自住)inline 改 + 14 类目 × 3 订单类型矩阵的费用占比规则。
          </Text>
        }
        closable
      />

      <Tabs
        activeKey={activeTab}
        onChange={handleTabChange}
        items={[
          { key: "owners", label: "按业主看", children: <OwnersTab /> },
          { key: "rooms", label: "按房间看", children: <RoomsTab /> },
        ]}
      />
    </div>
  );
}
