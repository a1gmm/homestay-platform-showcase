"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { Empty, Skeleton, Drawer } from "antd";
import { ownerDataApi } from "@/lib/owner-api";
import { useOwnerStore } from "@/lib/owner-store";
import { useIsDesktop } from "@/lib/responsive";
import { groupByOwnerName, sumField } from "@/lib/owner-grouping";
import { SettlementDetailView, SettlementCard, fmtMille } from "./SettlementDetailView";

export default function OwnerSettlementsPage() {
  const router = useRouter();
  const isLoggedIn = useOwnerStore((s) => s.isLoggedIn());
  const owner = useOwnerStore((s) => s.owner);
  const isMaster = !!owner?.is_master;
  const isDesktop = useIsDesktop();
  const [openId, setOpenId] = useState<string | null>(null);

  useEffect(() => {
    if (!isLoggedIn) {
      router.replace(`/owner/login?next=${encodeURIComponent("/owner/settlements")}`);
    }
  }, [isLoggedIn, router]);

  const { data: list, isLoading } = useQuery({
    queryKey: ["owner", "settlements"],
    queryFn: async () => (await ownerDataApi.settlements()).data,
    enabled: isLoggedIn,
  });

  // 桌面版：列表加载后默认选中第一条，右侧详情不空着。
  useEffect(() => {
    if (isDesktop && !openId && list && list.length > 0) {
      setOpenId(list[0].settlement_id);
    }
  }, [isDesktop, openId, list]);

  const { data: detail } = useQuery({
    queryKey: ["owner", "settlement", openId],
    queryFn: async () => (await ownerDataApi.settlementDetail(openId!)).data,
    enabled: !!openId,
  });

  if (!isLoggedIn) return null;

  const listBody =
    isLoading ? (
      <Skeleton active />
    ) : !list || list.length === 0 ? (
      <Empty description="还没有结算单" style={{ marginTop: 60 }} />
    ) : isMaster ? (
      <>
        {groupByOwnerName(list, (s) => s.owner_name).map((g) => {
          // 到厘：分组小计按各单到厘值累加（旧单无 precise 回退到分值）
          const subtotal = sumField(g.items, (s) => s.actual_owner_amount_precise ?? s.actual_owner_amount);
          return (
            <div key={g.ownerName} style={{ marginBottom: 20 }}>
              <div
                style={{
                  display: "flex",
                  alignItems: "baseline",
                  justifyContent: "space-between",
                  padding: "8px 10px",
                  marginBottom: 8,
                  background: "#2B2721",
                  borderRadius: 10,
                }}
              >
                <div className="serif" style={{ fontSize: 14, fontWeight: 400, color: "#FBF8F1", letterSpacing: 0 }}>
                  {g.ownerName}
                  <span style={{ fontSize: 11, color: "#A89680", marginLeft: 8, fontFamily: "var(--font-sans)" }}>
                    {g.items.length} 单
                  </span>
                </div>
                <div className="serif" style={{ fontSize: 14, fontWeight: 400, color: "#FBF8F1", letterSpacing: 0, textAlign: "right" }}>
                  ¥{fmtMille(subtotal, subtotal)}
                </div>
              </div>
              {g.items.map((s) => (
                <SettlementCard key={s.settlement_id} s={s} onClick={() => setOpenId(s.settlement_id)} selected={isDesktop && openId === s.settlement_id} />
              ))}
            </div>
          );
        })}
      </>
    ) : (
      list.map((s) => (
        <SettlementCard key={s.settlement_id} s={s} onClick={() => setOpenId(s.settlement_id)} selected={isDesktop && openId === s.settlement_id} />
      ))
    );

  // ── 桌面版：左列表 + 右详情双栏 ─────────────────────────────────────────
  if (isDesktop) {
    return (
      <div style={{ padding: "26px 26px 40px" }}>
        <div style={{ marginBottom: 20 }}>
          <div className="serif" style={{ fontSize: 24, fontWeight: 400, color: "#2B2721", letterSpacing: 0 }}>业主结算单</div>
          <div style={{ fontSize: 12, color: "#A89680", marginTop: 4 }}>每月自动生成 · 点击左侧查看每套房明细</div>
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "minmax(320px, 380px) 1fr", gap: 20, alignItems: "start" }}>
          <div style={{ maxHeight: "calc(100dvh - 150px)", overflowY: "auto", paddingRight: 4 }}>{listBody}</div>
          <div style={{ position: "sticky", top: 26 }}>
            {!openId ? (
              <div style={{ background: "#F5F1EA", border: "0.5px solid #E5DDCB", borderRadius: 16, padding: 40, textAlign: "center" }}>
                <Empty description="选择左侧结算单查看明细" />
              </div>
            ) : !detail ? (
              <div style={{ background: "#F5F1EA", border: "0.5px solid #E5DDCB", borderRadius: 16, padding: 24 }}>
                <Skeleton active paragraph={{ rows: 6 }} />
              </div>
            ) : (
              <SettlementDetailView detail={detail} />
            )}
          </div>
        </div>
      </div>
    );
  }

  // ── 移动版：列表 + 底部 Drawer（沿用原有体验，不动）──────────────────────
  return (
    <div>
      <div style={{ padding: "28px 20px 20px", background: "#2B2721", color: "#FBF8F1" }}>
        <div className="serif" style={{ fontSize: 22, fontWeight: 400, letterSpacing: 0 }}>业主结算单</div>
        <div style={{ fontSize: 12, color: "#A89680", marginTop: 4 }}>每月自动生成 · 可查看每套房明细</div>
      </div>

      <div style={{ padding: 12 }}>{listBody}</div>

      <Drawer
        title={detail ? `${detail.billing_month} 结算单` : "加载中"}
        placement="bottom"
        height="88vh"
        onClose={() => setOpenId(null)}
        open={!!openId}
      >
        {!detail ? <Skeleton active /> : <SettlementDetailView detail={detail} />}
      </Drawer>
    </div>
  );
}
