"use client";

import { useEffect, useMemo, useState, type CSSProperties } from "react";
import { useRouter } from "next/navigation";
import { useQuery, useQueries } from "@tanstack/react-query";
import { Button, Empty, Result, Skeleton, Tag } from "antd";
import { LeftOutlined, RightOutlined, DownOutlined } from "@ant-design/icons";
import { ownerDataApi, type MonthlySummary, type RoomMonthlyStat } from "@/lib/owner-api";
import { useOwnerStore } from "@/lib/owner-store";
import { extractErrorMessage } from "@/lib/api";
import { useIsDesktop } from "@/lib/responsive";
import { groupByRoomType, UNGROUPED_LABEL } from "@/lib/room-types";
import { groupByOwnerName, sumField } from "@/lib/owner-grouping";
import {
  type YearMonth,
  shiftMonth,
  isSameMonth,
  lastNMonths,
  barHeightPct,
} from "@/lib/owner-trend";
import {
  totalOccupancyNights,
  deriveOccupancyRate,
  occupancySubtitle,
} from "@/lib/owner-occupancy";

const TREND_MONTHS = 6;

const fmt = (n: string | number | null | undefined) => {
  // null/undefined = 隐藏金额账号后端置空 → 显示「——」（真实 0 是 0，不是 null）。
  if (n === null || n === undefined) return "——";
  const v = typeof n === "string" ? parseFloat(n) : n;
  if (Number.isNaN(v)) return "——";
  return v.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
};

const toNum = (n: string | number | null | undefined): number => {
  const v = typeof n === "string" ? parseFloat(n) : n ?? 0;
  return Number.isNaN(v as number) ? 0 : (v as number);
};

// 派生出来的收益分配四段（详见设计规格「数据契约」）：
// 平台佣金 = revenue - net_revenue；运营费用 = owner_expenses；业主应得 = owner_net；
// 其他分成 = (net_revenue - owner_expenses) - owner_net，负数按 0 处理。
function deriveAllocation(data: MonthlySummary) {
  const revenue = toNum(data.revenue);
  const netRevenue = toNum(data.net_revenue);
  const ownerExpenses = toNum(data.owner_expenses);
  const ownerNet = toNum(data.owner_net);
  const commission = Math.max(0, revenue - netRevenue);
  const otherShareRaw = netRevenue - ownerExpenses - ownerNet;
  const otherShare = otherShareRaw > 0 ? otherShareRaw : 0;
  return { revenue, netRevenue, ownerExpenses, ownerNet, commission, otherShare };
}

// 「结算到昨天」说明：查看当前月时后端返回 as_of_date（=昨天），提示业主这是
// 阶段性数字——本月还没退房走人的订单不计入，退房后自动加进来。历史月无此字段。
function asOfNote(data: MonthlySummary): string | null {
  if (!data.as_of_date) return null;
  const [, mm, dd] = data.as_of_date.split("-");
  return `统计截止 ${Number(mm)}/${Number(dd)}，本月未退房的订单会在退房后陆续计入`;
}

export default function OwnerHomePage() {
  const router = useRouter();
  const isLoggedIn = useOwnerStore((s) => s.isLoggedIn());
  const owner = useOwnerStore((s) => s.owner);
  const isDesktop = useIsDesktop();

  useEffect(() => {
    if (!isLoggedIn) {
      router.replace(`/owner/login?next=${encodeURIComponent("/owner")}`);
    }
  }, [isLoggedIn, router]);

  // 「本月」按浏览器本地时间算一次（客户端组件,SSR 不跑到这）；作为可选月份的上界,
  // 业主看不了未来收益。选中月默认就是本月。
  const currentYM = useMemo<YearMonth>(() => {
    const now = new Date();
    return { year: now.getFullYear(), month: now.getMonth() + 1 };
  }, []);
  const [selectedYM, setSelectedYM] = useState<YearMonth>(currentYM);
  const atCurrent = isSameMonth(selectedYM, currentYM);

  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["owner", "summary", selectedYM.year, selectedYM.month],
    queryFn: async () => (await ownerDataApi.summary(selectedYM.year, selectedYM.month)).data,
    enabled: isLoggedIn,
  });

  // 环比：与上一月的 owner_net 比较，用于 hero 里的百分比标注。
  const prevYM = useMemo(() => shiftMonth(selectedYM, -1), [selectedYM]);
  const { data: prevData } = useQuery({
    queryKey: ["owner", "summary", prevYM.year, prevYM.month],
    queryFn: async () => (await ownerDataApi.summary(prevYM.year, prevYM.month)).data,
    enabled: isLoggedIn,
  });

  if (!isLoggedIn) return null;

  if (isError) {
    return (
      <div style={{ padding: 32 }}>
        <Result
          status="warning"
          title="加载失败"
          subTitle={extractErrorMessage(error)}
          extra={
            <Button type="primary" onClick={() => refetch()}>
              重新加载
            </Button>
          }
        />
      </div>
    );
  }

  const billingMonth = `${selectedYM.year}年${selectedYM.month}月`;
  const goPrev = () => setSelectedYM((ym) => shiftMonth(ym, -1));
  const goNext = () => {
    if (!atCurrent) setSelectedYM((ym) => shiftMonth(ym, 1));
  };

  const isMaster = !!owner?.is_master;

  const momPct = (() => {
    if (!data || !prevData) return null;
    const cur = toNum(data.owner_net);
    const prev = toNum(prevData.owner_net);
    if (prev <= 0) return null;
    return Math.round(((cur - prev) / prev) * 1000) / 10;
  })();

  const monthNav = { billingMonth, atCurrent, goPrev, goNext, backToCurrent: () => setSelectedYM(currentYM) };

  // ── 桌面版：仪表盘布局 ─────────────────────────────────────────────────
  if (isDesktop) {
    return (
      <DesktopHome
        data={data}
        isLoading={isLoading}
        selectedYM={selectedYM}
        onPickMonth={setSelectedYM}
        monthNav={monthNav}
        isMaster={isMaster}
        owner={owner}
        momPct={momPct}
        onRoom={(rid) => router.push(`/owner/rooms/${rid}`)}
        onSettlements={() => router.push("/owner/settlements")}
      />
    );
  }

  // ── 移动版：单列（沿用原有体验，不动）─────────────────────────────────
  return (
    <div>
      {/* 深棕 hero：月份切换 + 业主应得（税后分成）大数字 */}
      <div
        style={{
          padding: "32px 20px 56px",
          background: "#2B2721",
          color: "#FBF8F1",
        }}
      >
        <div style={{ fontSize: 12, color: "#A89680", marginBottom: 8 }}>
          欢迎,{owner?.name || "业主"}
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
          <button type="button" aria-label="上一月" onClick={goPrev} style={monthNavBtnStyle(false)}>
            <LeftOutlined style={{ fontSize: 13 }} />
          </button>
          <div
            className="serif"
            style={{ fontSize: 24, fontWeight: 400, letterSpacing: 0, minWidth: 132, textAlign: "center" }}
          >
            {billingMonth}
          </div>
          <button
            type="button"
            aria-label="下一月"
            onClick={goNext}
            disabled={atCurrent}
            style={monthNavBtnStyle(atCurrent)}
          >
            <RightOutlined style={{ fontSize: 13 }} />
          </button>
          {!atCurrent && (
            <button
              type="button"
              onClick={() => setSelectedYM(currentYM)}
              style={{
                marginLeft: 4,
                background: "transparent",
                border: "0.5px solid #5C5547",
                color: "#A89680",
                borderRadius: 999,
                fontSize: 11,
                padding: "0 12px",
                cursor: "pointer",
                minHeight: 44,
                display: "flex",
                alignItems: "center",
              }}
            >
              回本月
            </button>
          )}
        </div>
        <div style={{ fontSize: 12, color: "#A89680", marginTop: 4 }}>
          {atCurrent ? "收益概览" : "历史月份 · 收益概览"}
        </div>

        {isLoading ? (
          <Skeleton active paragraph={{ rows: 2 }} style={{ marginTop: 20 }} />
        ) : data ? (
          <>
            <div style={{ fontSize: 12, color: "#A89680", marginTop: 20 }}>
              本月订单 {data.order_count} · 房费收入 ¥{fmt(data.revenue)}
            </div>
            <div style={{ fontSize: 12, color: "#A89680", marginTop: 12 }}>业主应得（税后分成）</div>
            <div style={{ display: "flex", alignItems: "baseline", gap: 10, marginTop: 6 }}>
              <div className="serif" style={{ fontSize: 38, fontWeight: 400, color: "#FBF8F1", letterSpacing: 0 }}>
                ¥{fmt(data.owner_net)}
              </div>
              {momPct !== null && (
                <div style={{ fontSize: 12, color: momPct >= 0 ? "#9FE1CB" : "#D98E73", fontWeight: 600 }}>
                  {momPct >= 0 ? "+" : ""}
                  {momPct}%
                </div>
              )}
            </div>
            <div style={{ fontSize: 11, color: "#A89680", marginTop: 10 }}>
              {isMaster
                ? `共管 ${owner?.sub_owners?.length ?? 0} 层 · ${sumRoomCount(owner?.sub_owners || [])} 套房`
                : `名下 ${data.rooms_total} 套`}
            </div>
            {asOfNote(data) && (
              <div style={{ fontSize: 11, color: "#C9B79A", marginTop: 8, lineHeight: 1.5 }}>
                📅 {asOfNote(data)}
              </div>
            )}
          </>
        ) : (
          <Empty description="暂无数据" style={{ marginTop: 20 }} />
        )}
      </div>

      {/* 收益分配卡：浮在 hero 上 */}
      <div style={{ padding: "0 12px", marginTop: -16 }}>
        {isLoading ? (
          <div style={{ background: "#F5F1EA", border: "0.5px solid #E5DDCB", borderRadius: 16, padding: 20 }}>
            <Skeleton active paragraph={{ rows: 3 }} />
          </div>
        ) : data ? (
          <AllocationCard data={data} isMaster={isMaster} onViewSettlements={() => router.push("/owner/settlements")} />
        ) : null}
      </div>

      {/* 入住率环形 + 近 6 个月业主应得趋势 */}
      <div style={{ padding: "16px 12px 0", display: "flex", gap: 12 }}>
        <div style={{ flex: "0 0 auto" }}>
          {isLoading ? (
            <div style={{ width: 112, height: 176, background: "#FBF8F1", border: "0.5px solid #E5DDCB", borderRadius: 16 }} />
          ) : data ? (
            <OccupancyRing rate={deriveOccupancyRate(data, selectedYM)} />
          ) : null}
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <RevenueTrend selected={selectedYM} onPick={setSelectedYM} />
        </div>
      </div>

      {/* Room list — master 按层(owner_name)分组,单业主按房型分组 */}
      <div style={{ padding: "20px 12px 0" }}>
        <div style={{ fontSize: 15, fontWeight: 600, color: "#2B2721", marginBottom: 10, paddingLeft: 4 }}>
          名下房源 {data ? `(${data.rooms_total})` : ""}
        </div>
        {isLoading ? (
          <Skeleton active />
        ) : !data || data.rooms.length === 0 ? (
          <Empty description="暂未关联房源,请联系管理员" style={{ marginTop: 40 }} />
        ) : isMaster ? (
          <OwnerGroupedRoomList rooms={data.rooms} onClick={(rid) => router.push(`/owner/rooms/${rid}`)} />
        ) : (
          <RoomTypeGroupedRoomList rooms={data.rooms} onClick={(rid) => router.push(`/owner/rooms/${rid}`)} />
        )}
        <div style={{ height: 16 }} />
      </div>
    </div>
  );
}

// ════════════════════════════════════════════════════════════════════════════
//  桌面版首页
// ════════════════════════════════════════════════════════════════════════════

interface MonthNav {
  billingMonth: string;
  atCurrent: boolean;
  goPrev: () => void;
  goNext: () => void;
  backToCurrent: () => void;
}

function DesktopHome({
  data,
  isLoading,
  selectedYM,
  onPickMonth,
  monthNav,
  isMaster,
  owner,
  momPct,
  onRoom,
  onSettlements,
}: {
  data?: MonthlySummary;
  isLoading: boolean;
  selectedYM: YearMonth;
  onPickMonth: (ym: YearMonth) => void;
  monthNav: MonthNav;
  isMaster: boolean;
  owner: ReturnType<typeof useOwnerStore.getState>["owner"];
  momPct: number | null;
  onRoom: (roomId: string) => void;
  onSettlements: () => void;
}) {
  return (
    <div>
      {/* 顶栏：月份导航 */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          padding: "16px 26px",
          borderBottom: "0.5px solid #E5DDCB",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <button type="button" aria-label="上一月" onClick={monthNav.goPrev} style={topNavBtnStyle(false)}>
            <LeftOutlined style={{ fontSize: 13 }} />
          </button>
          <div className="serif" style={{ fontSize: 20, minWidth: 120, textAlign: "center", letterSpacing: 0 }}>
            {monthNav.billingMonth}
          </div>
          <button
            type="button"
            aria-label="下一月"
            onClick={monthNav.goNext}
            disabled={monthNav.atCurrent}
            style={topNavBtnStyle(monthNav.atCurrent)}
          >
            <RightOutlined style={{ fontSize: 13 }} />
          </button>
          {!monthNav.atCurrent && (
            <button
              type="button"
              onClick={monthNav.backToCurrent}
              style={{
                marginLeft: 4,
                background: "transparent",
                border: "0.5px solid #E5DDCB",
                color: "#6B665B",
                borderRadius: 999,
                fontSize: 12,
                padding: "5px 12px",
                cursor: "pointer",
              }}
            >
              回本月
            </button>
          )}
        </div>
        <div style={{ fontSize: 12, color: "#A89680" }}>
          {isMaster ? "只读汇总" : "收益概览"} · {monthNav.atCurrent ? "本月" : "历史月份"}
        </div>
      </div>

      {/* 画布 */}
      <div style={{ padding: "22px 26px 40px", display: "flex", flexDirection: "column", gap: 16 }}>
        {/* hero 卡 */}
        {isLoading ? (
          <div style={{ background: "#2B2721", borderRadius: 16, padding: 26 }}>
            <Skeleton active paragraph={{ rows: 2 }} />
          </div>
        ) : data ? (
          <DesktopHero data={data} isMaster={isMaster} owner={owner} momPct={momPct} />
        ) : (
          <div style={{ background: "#2B2721", borderRadius: 16, padding: 40 }}>
            <Empty description={<span style={{ color: "#A89680" }}>暂无数据</span>} />
          </div>
        )}

        {/* 收益分配 + 入住率环形 + 近 6 月趋势 并排 */}
        <div style={{ display: "grid", gridTemplateColumns: "1.5fr 0.8fr 1.4fr", gap: 16, alignItems: "stretch" }}>
          <div>
            {isLoading ? (
              <CardSkeleton rows={3} />
            ) : data ? (
              <AllocationCard data={data} isMaster={isMaster} onViewSettlements={onSettlements} />
            ) : (
              <CardSkeleton rows={3} />
            )}
          </div>
          <div>
            {isLoading ? (
              <CardSkeleton rows={3} />
            ) : data ? (
              <OccupancyRing rate={deriveOccupancyRate(data, selectedYM)} size={112} subtitle={occupancySubtitle(data, selectedYM)} />
            ) : (
              <CardSkeleton rows={3} />
            )}
          </div>
          <div>
            <RevenueTrend selected={selectedYM} onPick={onPickMonth} />
          </div>
        </div>

        {/* 房源 */}
        <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", margin: "6px 2px 0" }}>
          <div style={{ fontSize: 15, fontWeight: 600, color: "#2B2721" }}>
            {isMaster ? "分层明细" : "名下房源"} {data ? `(${data.rooms_total})` : ""}
          </div>
          <div style={{ fontSize: 12, color: "#A89680" }}>
            {isMaster ? "左右分栏 · 两层并排" : "按房型 · 网格"}
          </div>
        </div>
        {isLoading ? (
          <Skeleton active />
        ) : !data || data.rooms.length === 0 ? (
          <Empty description="暂未关联房源,请联系管理员" style={{ marginTop: 40 }} />
        ) : isMaster ? (
          <DesktopFloorColumns rooms={data.rooms} onClick={onRoom} />
        ) : (
          <DesktopRoomTypeGrid rooms={data.rooms} onClick={onRoom} />
        )}
      </div>
    </div>
  );
}

function DesktopHero({
  data,
  isMaster,
  owner,
  momPct,
}: {
  data: MonthlySummary;
  isMaster: boolean;
  owner: ReturnType<typeof useOwnerStore.getState>["owner"];
  momPct: number | null;
}) {
  const foot = isMaster
    ? `较上月 · 共管 ${owner?.sub_owners?.length ?? 0} 层 · ${sumRoomCount(owner?.sub_owners || [])} 套房 · 本月订单 ${data.order_count}`
    : `较上月 · 名下 ${data.rooms_total} 套 · 本月订单 ${data.order_count}`;

  const kpis = [
    { label: "房费收入", value: `¥${fmt(data.revenue)}` },
    { label: "房费净收入", value: `¥${fmt(data.net_revenue)}` },
    { label: "入住晚数", value: String(totalOccupancyNights(data)) },
  ];

  return (
    <div
      style={{
        background: "#2B2721",
        color: "#FBF8F1",
        borderRadius: 16,
        padding: "26px 28px",
        display: "grid",
        gridTemplateColumns: "1.3fr 1fr",
        gap: 24,
        alignItems: "center",
      }}
    >
      <div>
        <div style={{ fontSize: 12, color: "#A89680" }}>
          {isMaster ? "业主应得合计（税后分成）" : "业主应得（税后分成）"}
        </div>
        <div style={{ display: "flex", alignItems: "baseline", gap: 12, marginTop: 8 }}>
          <div className="serif" style={{ fontSize: 46, lineHeight: 1.05, letterSpacing: 0 }}>
            ¥{fmt(data.owner_net)}
          </div>
          {momPct !== null && (
            <div style={{ fontSize: 14, color: momPct >= 0 ? "#9FE1CB" : "#D98E73", fontWeight: 600 }}>
              {momPct >= 0 ? "+" : ""}
              {momPct}%
            </div>
          )}
        </div>
        <div style={{ fontSize: 11, color: "#A89680", marginTop: 12 }}>{foot}</div>
        {asOfNote(data) && (
          <div style={{ fontSize: 11, color: "#C9B79A", marginTop: 8, lineHeight: 1.5 }}>
            📅 {asOfNote(data)}
          </div>
        )}
      </div>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "1fr 1fr 1fr",
          gap: 18,
          borderLeft: "0.5px solid #4a4437",
          paddingLeft: 24,
        }}
      >
        {kpis.map((k) => (
          <div key={k.label}>
            <div style={{ fontSize: 10, letterSpacing: "0.16em", textTransform: "uppercase", color: "#A89680" }}>
              {k.label}
            </div>
            <div className="serif" style={{ fontSize: 22, marginTop: 6, letterSpacing: 0 }}>
              {k.value}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function CardSkeleton({ rows }: { rows: number }) {
  return (
    <div style={{ background: "#F5F1EA", border: "0.5px solid #E5DDCB", borderRadius: 16, padding: 20, height: "100%" }}>
      <Skeleton active paragraph={{ rows }} />
    </div>
  );
}

// 桌面单业主：按房型分组，全展开成 3 列网格。
function DesktopRoomTypeGrid({ rooms, onClick }: { rooms: RoomMonthlyStat[]; onClick: (roomId: string) => void }) {
  const groups = useMemo(
    () =>
      groupByRoomType(
        rooms,
        (r) => r.room_type,
        (a, b) => {
          const aActive = a.occupancy_nights > 0 || a.order_count > 0 ? 1 : 0;
          const bActive = b.occupancy_nights > 0 || b.order_count > 0 ? 1 : 0;
          if (aActive !== bActive) return bActive - aActive;
          return a.room_id.localeCompare(b.room_id);
        },
      ),
    [rooms],
  );

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      {groups.map(({ groupName, items }) => {
        const activeCount = items.filter((r) => r.occupancy_nights > 0 || r.order_count > 0).length;
        const displayName = groupName === UNGROUPED_LABEL ? "其他房型" : groupName;
        const netTotal = sumField(items, (r) => r.owner_net);
        return (
          <div key={groupName}>
            <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", padding: "2px 2px", margin: "14px 0 10px" }}>
              <div className="serif" style={{ fontSize: 14, color: "#5C5547", letterSpacing: 0 }}>
                {displayName}
                <span style={{ fontSize: 11, color: "#A89680", marginLeft: 8, fontFamily: "var(--font-sans)" }}>
                  {items.length} 套{activeCount > 0 ? ` · 本月 ${activeCount} 套在住` : ""}
                </span>
              </div>
              <div style={{ fontSize: 11, color: "#A89680" }}>
                合计应得 <span className="serif" style={{ color: "#2B2721", fontSize: 13 }}>¥{fmt(netTotal)}</span>
              </div>
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 10 }}>
              {items.map((r) => (
                <DesktopRoomCard key={r.room_id} room={r} onClick={() => onClick(r.room_id)} />
              ))}
            </div>
          </div>
        );
      })}
    </div>
  );
}

// 桌面总账号：按楼层左右分栏（两层并排的网格），每层深色组头 + 层内 2 列房间小卡。
function DesktopFloorColumns({
  rooms,
  onClick,
}: {
  rooms: RoomMonthlyStat[];
  onClick: (roomId: string) => void;
}) {
  const ownerGroups = useMemo(() => groupByOwnerName(rooms, (r) => r.owner_name), [rooms]);

  return (
    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16, alignItems: "start" }}>
      {ownerGroups.map((g) => {
        const netTotal = sumField(g.items, (r) => r.owner_net);
        const netRevenueTotal = sumField(g.items, (r) => r.net_revenue);
        const ownerExpensesTotal = sumField(g.items, (r) => r.owner_expenses);
        const orderCountTotal = sumField(g.items, (r) => r.order_count);

        return (
          <div key={g.ownerName} style={{ background: "#FBF8F1", border: "0.5px solid #E5DDCB", borderRadius: 14, padding: 14 }}>
            <div style={{ background: "#2B2721", borderRadius: 12, padding: "12px 14px", color: "#FBF8F1" }}>
              <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between" }}>
                <div style={{ minWidth: 0, flex: 1 }}>
                  <div className="serif" style={{ fontSize: 15, letterSpacing: 0 }}>{g.ownerName}</div>
                  <div style={{ fontSize: 11, color: "#A89680", marginTop: 3 }}>
                    订单 {orderCountTotal} · 净收入 ¥{fmt(netRevenueTotal)} · 支出 ¥{fmt(ownerExpensesTotal)}
                  </div>
                </div>
                <div style={{ textAlign: "right", marginLeft: 8 }}>
                  <div className="serif" style={{ fontSize: 16, letterSpacing: 0 }}>¥{fmt(netTotal)}</div>
                  <div style={{ fontSize: 10, color: "#A89680" }}>应得</div>
                </div>
              </div>
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8, marginTop: 12 }}>
              {g.items.map((r) => (
                <DesktopRoomCard key={r.room_id} room={r} onClick={() => onClick(r.room_id)} compact />
              ))}
            </div>
          </div>
        );
      })}
    </div>
  );
}

// 桌面房间卡：full = 房型网格用（名 + No. + 在住 + meta + 应得）；compact = 楼层分栏内用（名 + No. + 应得）。
function DesktopRoomCard({ room, onClick, compact }: { room: RoomMonthlyStat; onClick: () => void; compact?: boolean }) {
  const isActive = room.occupancy_nights > 0 || room.order_count > 0;
  return (
    <div
      onClick={onClick}
      className="card-hoverable"
      style={{
        background: "#F5F1EA",
        border: isActive ? "0.5px solid #C9B89A" : "0.5px solid #E5DDCB",
        borderRadius: compact ? 10 : 12,
        padding: compact ? 11 : 14,
        cursor: "pointer",
      }}
    >
      <div className="serif" style={{ fontSize: compact ? 14 : 16, letterSpacing: 0, display: "flex", alignItems: "center", gap: 7, flexWrap: "wrap" }}>
        {room.room_name}
        <span style={{ fontSize: 11, color: "#A89680", fontFamily: "var(--font-sans)" }}>No. {room.room_id}</span>
        {isActive && !compact && (
          <span style={{ fontSize: 10, color: "#8A6E5A", border: "0.5px solid #C9B89A", borderRadius: 999, padding: "1px 7px" }}>
            本月在住
          </span>
        )}
      </div>
      {!compact && (
        <div style={{ fontSize: 11, color: "#7A6F5F", marginTop: 6, lineHeight: 1.6 }}>
          本月 {room.order_count} 单 · 入住 {room.occupancy_nights} 晚
        </div>
      )}
      <div
        style={{
          display: "flex",
          alignItems: "baseline",
          justifyContent: "space-between",
          marginTop: compact ? 8 : 12,
          paddingTop: compact ? 8 : 10,
          borderTop: "0.5px solid #E5DDCB",
        }}
      >
        <div className="serif" style={{ fontSize: compact ? 15 : 18, color: "#2B2721", letterSpacing: 0 }}>
          ¥{fmt(room.owner_net)}
        </div>
        <div style={{ fontSize: 11, color: "#A89680" }}>应得</div>
      </div>
    </div>
  );
}

// ════════════════════════════════════════════════════════════════════════════
//  共享组件（移动端 + 桌面端复用）
// ════════════════════════════════════════════════════════════════════════════

// 收益分配卡：横向堆叠条（4 段）+ 图例 + 底部小字。
// is_master：4 段全显示,其他分成用 #8A8072 并列入图例。
// 单业主：其他分成段用浅灰 #E5DDCB 且不列入图例(只列前 3 项)。
function AllocationCard({
  data,
  isMaster,
  onViewSettlements,
}: {
  data: MonthlySummary;
  isMaster: boolean;
  onViewSettlements: () => void;
}) {
  // 隐藏金额账号（演示号）：后端把金额置 None → 不画收益分配环，显示占位，避免误导成 ¥0。
  if (data.revenue == null) {
    return (
      <div style={{ background: "#F5F1EA", border: "0.5px solid #E5DDCB", borderRadius: 16, padding: 20, height: "100%" }}>
        <div style={{ fontSize: 13, fontWeight: 600, color: "#5C5547", marginBottom: 14 }}>收益分配</div>
        <div style={{ fontSize: 12, color: "#A89680", textAlign: "center", padding: "28px 0" }}>
          金额已隐藏（演示账号）
        </div>
      </div>
    );
  }

  const { revenue, netRevenue, ownerExpenses, ownerNet, commission, otherShare } = deriveAllocation(data);
  const safeRevenue = revenue > 0 ? revenue : 1; // 避免除 0，revenue<=0 时所有段宽度渲染为 0

  const segments = [
    { key: "commission", label: "平台佣金", value: commission, color: "#B5875A" },
    { key: "expenses", label: "运营费用", value: ownerExpenses, color: "#C9B89A" },
    { key: "owner_net", label: "业主应得", value: ownerNet, color: "#2B2721" },
    {
      key: "other",
      label: "其他分成",
      value: otherShare,
      color: isMaster ? "#8A8072" : "#E5DDCB",
    },
  ];

  // 单业主不在图例里列「其他分成」，只列前 3 项。
  const legendItems = isMaster ? segments : segments.slice(0, 3);

  const denomForShare = netRevenue - ownerExpenses;
  const shareRatioPct = denomForShare > 0 ? Math.round((ownerNet / denomForShare) * 100) : 0;

  return (
    <div style={{ background: "#F5F1EA", border: "0.5px solid #E5DDCB", borderRadius: 16, padding: 20, height: "100%" }}>
      <div style={{ fontSize: 13, fontWeight: 600, color: "#5C5547", marginBottom: 14 }}>收益分配</div>

      {/* 堆叠条 */}
      <div style={{ display: "flex", width: "100%", height: 10, borderRadius: 999, overflow: "hidden", background: "#EFE8DA" }}>
        {segments.map((seg) => (
          <div
            key={seg.key}
            style={{ width: `${(Math.max(0, seg.value) / safeRevenue) * 100}%`, background: seg.color, height: "100%" }}
          />
        ))}
      </div>

      {/* 图例 2 列 */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(2, 1fr)", gap: "10px 8px", marginTop: 16 }}>
        {legendItems.map((seg) => (
          <div key={seg.key} style={{ display: "flex", alignItems: "center", gap: 6, minHeight: 20 }}>
            <span style={{ width: 8, height: 8, borderRadius: 2, background: seg.color, flexShrink: 0 }} />
            <span style={{ fontSize: 11, color: "#7A6F5F" }}>{seg.label}</span>
            <span style={{ fontSize: 11, color: "#2B2721", marginLeft: "auto" }}>¥{fmt(seg.value)}</span>
          </div>
        ))}
      </div>

      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          marginTop: 16,
          paddingTop: 12,
          borderTop: "0.5px solid #E5DDCB",
          fontSize: 11,
          color: "#A89680",
          flexWrap: "wrap",
          gap: 6,
        }}
      >
        <span>
          房费净收入 ¥{fmt(netRevenue)} · 分成比例 {isMaster ? "各层不同" : `${shareRatioPct}%`}
        </span>
        <button
          type="button"
          onClick={onViewSettlements}
          style={{
            background: "transparent",
            border: "none",
            color: "#B5875A",
            fontSize: 11,
            cursor: "pointer",
            padding: 0,
            minHeight: 44,
            display: "flex",
            alignItems: "center",
          }}
        >
          查看结算明细 →
        </button>
      </div>
    </div>
  );
}

// 入住率环形：SVG 圆环。size = 内圈直径（移动端默认 78，桌面 112）；stroke/半径按 size 派生。
function OccupancyRing({ rate, size = 78, subtitle }: { rate: number; size?: number; subtitle?: string }) {
  const stroke = Math.round(size * 0.1);
  const radius = size / 2 - stroke / 2 - 4;
  const center = size / 2;
  const circumference = 2 * Math.PI * radius;
  const clamped = Math.max(0, Math.min(100, rate));
  const dashOffset = circumference * (1 - clamped / 100);

  return (
    <div
      style={{
        background: "#FBF8F1",
        border: "0.5px solid #E5DDCB",
        borderRadius: 16,
        padding: "16px 12px",
        height: "100%",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        minWidth: 112,
        gap: 10,
      }}
    >
      <div style={{ fontSize: 11, color: "#A89680" }}>入住率</div>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
        <circle cx={center} cy={center} r={radius} fill="none" stroke="#EFE8DA" strokeWidth={stroke} />
        <circle
          cx={center}
          cy={center}
          r={radius}
          fill="none"
          stroke="#C9B89A"
          strokeWidth={stroke}
          strokeDasharray={circumference}
          strokeDashoffset={dashOffset}
          strokeLinecap="round"
          transform={`rotate(-90 ${center} ${center})`}
        />
        <text
          x={center}
          y={center}
          textAnchor="middle"
          dominantBaseline="central"
          fontSize={Math.round(size * 0.19)}
          fill="#2B2721"
          className="serif"
        >
          {clamped.toFixed(1)}%
        </text>
      </svg>
      {subtitle && <div style={{ fontSize: 11, color: "#6B665B" }}>{subtitle}</div>}
    </div>
  );
}

// 单业主：按房型折叠分组；默认折叠,组头 toggle。（移动端用）
function RoomTypeGroupedRoomList({ rooms, onClick }: { rooms: RoomMonthlyStat[]; onClick: (roomId: string) => void }) {
  const groups = useMemo(
    () =>
      groupByRoomType(
        rooms,
        (r) => r.room_type,
        (a, b) => {
          const aActive = a.occupancy_nights > 0 || a.order_count > 0 ? 1 : 0;
          const bActive = b.occupancy_nights > 0 || b.order_count > 0 ? 1 : 0;
          if (aActive !== bActive) return bActive - aActive;
          return a.room_id.localeCompare(b.room_id);
        },
      ),
    [rooms],
  );

  const [openGroups, setOpenGroups] = useState<Record<string, boolean>>({});
  const toggle = (name: string) => setOpenGroups((s) => ({ ...s, [name]: !s[name] }));

  return (
    <>
      {groups.map(({ groupName, items }) => {
        const activeCount = items.filter((r) => r.occupancy_nights > 0 || r.order_count > 0).length;
        const displayName = groupName === UNGROUPED_LABEL ? "其他房型" : groupName;
        const netTotal = sumField(items, (r) => r.owner_net);
        const isOpen = !!openGroups[groupName];

        return (
          <div key={groupName} style={{ marginBottom: 12 }}>
            <button
              type="button"
              onClick={() => toggle(groupName)}
              style={{
                width: "100%",
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                padding: "12px 14px",
                background: "#F5F1EA",
                border: "0.5px solid #E5DDCB",
                borderRadius: 12,
                cursor: "pointer",
                minHeight: 44,
                textAlign: "left",
              }}
            >
              <div style={{ minWidth: 0, flex: 1 }}>
                <div className="serif" style={{ fontSize: 14, fontWeight: 400, color: "#2B2721", letterSpacing: 0 }}>
                  {displayName}
                </div>
                <div style={{ fontSize: 11, color: "#A89680", marginTop: 2 }}>
                  {items.length} 套{activeCount > 0 ? ` · 本月 ${activeCount} 套在住` : ""}
                </div>
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: 8, marginLeft: 8 }}>
                <div style={{ textAlign: "right" }}>
                  <div className="serif" style={{ fontSize: 16, fontWeight: 400, color: "#2B2721", letterSpacing: 0 }}>
                    ¥{fmt(netTotal)}
                  </div>
                  <div style={{ fontSize: 10, color: "#A89680" }}>应得</div>
                </div>
                {isOpen ? (
                  <DownOutlined style={{ fontSize: 12, color: "#A89680" }} />
                ) : (
                  <RightOutlined style={{ fontSize: 12, color: "#A89680" }} />
                )}
              </div>
            </button>

            {isOpen && (
              <div style={{ marginTop: 8 }}>
                {items.map((r) => (
                  <RoomRow key={r.room_id} room={r} onClick={() => onClick(r.room_id)} />
                ))}
              </div>
            )}
          </div>
        );
      })}
    </>
  );
}

// 总账号（is_master）：按 owner_name（=楼层）折叠分组，组头显示订单/净收入/分成比例
// + 应得金额；默认折叠。（移动端用）
function OwnerGroupedRoomList({
  rooms,
  onClick,
}: {
  rooms: RoomMonthlyStat[];
  onClick: (roomId: string) => void;
}) {
  const ownerGroups = useMemo(() => groupByOwnerName(rooms, (r) => r.owner_name), [rooms]);
  const [openGroups, setOpenGroups] = useState<Record<string, boolean>>({});
  const toggle = (name: string) => setOpenGroups((s) => ({ ...s, [name]: !s[name] }));

  return (
    <>
      {ownerGroups.map((g) => {
        const netTotal = sumField(g.items, (r) => r.owner_net); // 该层业主应得
        const netRevenueTotal = sumField(g.items, (r) => r.net_revenue);
        const ownerExpensesTotal = sumField(g.items, (r) => r.owner_expenses);
        const orderCountTotal = sumField(g.items, (r) => r.order_count);
        const isOpen = !!openGroups[g.ownerName];

        return (
          <div key={g.ownerName} style={{ marginBottom: 14 }}>
            <button
              type="button"
              onClick={() => toggle(g.ownerName)}
              style={{
                width: "100%",
                display: "flex",
                flexDirection: "column",
                gap: 8,
                padding: "12px 14px",
                background: "#2B2721",
                border: "none",
                borderRadius: 12,
                cursor: "pointer",
                minHeight: 44,
                textAlign: "left",
              }}
            >
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                <div style={{ minWidth: 0, flex: 1 }}>
                  <div className="serif" style={{ fontSize: 15, fontWeight: 400, color: "#FBF8F1", letterSpacing: 0 }}>
                    {g.ownerName}
                  </div>
                  <div style={{ fontSize: 11, color: "#A89680", marginTop: 2 }}>
                    订单 {orderCountTotal} · 净收入 ¥{fmt(netRevenueTotal)} · 支出 ¥{fmt(ownerExpensesTotal)}
                  </div>
                </div>
                <div style={{ display: "flex", alignItems: "center", gap: 8, marginLeft: 8 }}>
                  <div style={{ textAlign: "right" }}>
                    <div className="serif" style={{ fontSize: 16, fontWeight: 400, color: "#FBF8F1", letterSpacing: 0 }}>
                      ¥{fmt(netTotal)}
                    </div>
                    <div style={{ fontSize: 10, color: "#A89680" }}>应得</div>
                  </div>
                  {isOpen ? (
                    <DownOutlined style={{ fontSize: 12, color: "#A89680" }} />
                  ) : (
                    <RightOutlined style={{ fontSize: 12, color: "#A89680" }} />
                  )}
                </div>
              </div>
            </button>

            {isOpen && (
              <div style={{ marginTop: 8 }}>
                {g.items.map((r) => (
                  <RoomRow key={r.room_id} room={r} onClick={() => onClick(r.room_id)} />
                ))}
              </div>
            )}
          </div>
        );
      })}
    </>
  );
}

// 单个房间行：房间名 [在住?] · 应得 ¥owner_net。（移动端列表用）
function RoomRow({ room, onClick }: { room: RoomMonthlyStat; onClick: () => void }) {
  const isActive = room.occupancy_nights > 0 || room.order_count > 0;
  return (
    <div
      onClick={onClick}
      className="card-hoverable"
      style={{
        background: "#F5F1EA",
        border: isActive ? "0.5px solid #C9B89A" : "0.5px solid #E5DDCB",
        borderRadius: 12,
        padding: 14,
        marginBottom: 8,
        cursor: "pointer",
        minHeight: 44,
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div style={{ minWidth: 0, flex: 1 }}>
          <div className="serif" style={{ fontSize: 16, fontWeight: 400, letterSpacing: 0 }}>
            {room.room_name}
            <span style={{ fontSize: 12, color: "#A89680", marginLeft: 8, fontFamily: "var(--font-sans)" }}>
              No. {room.room_id}
            </span>
            {isActive && (
              <Tag color="gold" style={{ marginLeft: 8, fontSize: 10, lineHeight: "16px", padding: "0 6px" }}>
                本月在住
              </Tag>
            )}
          </div>
          <div style={{ fontSize: 11, color: "#7A6F5F", marginTop: 4, lineHeight: 1.6 }}>
            本月 {room.order_count} 单 · 入住 {room.occupancy_nights} 晚
          </div>
        </div>
        <div style={{ textAlign: "right", marginLeft: 8 }}>
          <div className="serif" style={{ fontSize: 18, fontWeight: 400, color: "#2B2721", letterSpacing: 0 }}>
            ¥{fmt(room.owner_net)}
          </div>
          <div style={{ fontSize: 11, color: "#A89680", marginTop: 1 }}>应得</div>
        </div>
      </div>
    </div>
  );
}

// 总账号 header 用：跨层房源总数 = 各子业主 room_count 之和。
function sumRoomCount(subOwners: { room_count: number }[]): number {
  return subOwners.reduce((acc, s) => acc + (s.room_count || 0), 0);
}

function monthNavBtnStyle(disabled: boolean): CSSProperties {
  return {
    width: 44,
    height: 44,
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    background: "transparent",
    border: "none",
    borderRadius: 8,
    color: disabled ? "#5C5547" : "#FBF8F1",
    cursor: disabled ? "not-allowed" : "pointer",
    padding: 0,
  };
}

// 桌面顶栏月份按钮：浅底描边（区别于移动端 hero 里的深底白字）。
function topNavBtnStyle(disabled: boolean): CSSProperties {
  return {
    width: 32,
    height: 32,
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    background: "transparent",
    border: "0.5px solid #E5DDCB",
    borderRadius: 8,
    color: disabled ? "#C9B89A" : "#2B2721",
    cursor: disabled ? "not-allowed" : "pointer",
    padding: 0,
  };
}

// 收益趋势卡：最近 TREND_MONTHS 个月「业主应得」(owner_net) 的柱状图。趋势的每根柱都复用
// 首页 summary 查询（同 queryKey），所以切月/回看不会重复打后端；点柱即把该月设为选中月。
// 故意不新增后端趋势接口——业主端 owner_portal.py 正在别处改，避免撞车。
function RevenueTrend({ selected, onPick }: { selected: YearMonth; onPick: (ym: YearMonth) => void }) {
  const months = useMemo(() => lastNMonths(selected, TREND_MONTHS), [selected]);

  const results = useQueries({
    queries: months.map((ym) => ({
      queryKey: ["owner", "summary", ym.year, ym.month],
      queryFn: async () => (await ownerDataApi.summary(ym.year, ym.month)).data,
    })),
  });

  const points = months.map((ym, i) => {
    const r = results[i];
    const value = r.data ? parseFloat(r.data.owner_net) || 0 : 0;
    return { ym, value, loading: r.isLoading };
  });
  const anyLoading = points.some((p) => p.loading);
  const max = Math.max(0, ...points.map((p) => p.value));
  const allZero = max <= 0;
  // 隐藏金额账号（演示号）：某月已加载但 owner_net 为 None → 是「隐藏」而非「无数据」。
  const hidden = results.some((r) => r.data != null && r.data.owner_net == null);

  return (
    <div style={{ background: "#FBF8F1", border: "0.5px solid #E5DDCB", borderRadius: 16, padding: "16px 14px 12px", height: "100%" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 12 }}>
        <div style={{ fontSize: 13, fontWeight: 600, color: "#5C5547" }}>近 6 个月业主应得</div>
        <div style={{ fontSize: 10, color: "#A89680" }}>点柱切换月份</div>
      </div>

      {hidden ? (
        <div style={{ fontSize: 12, color: "#A89680", textAlign: "center", padding: "18px 0" }}>
          金额已隐藏（演示账号）
        </div>
      ) : anyLoading && allZero ? (
        <Skeleton active paragraph={{ rows: 2 }} />
      ) : allZero ? (
        <div style={{ fontSize: 12, color: "#A89680", textAlign: "center", padding: "18px 0" }}>
          近 6 个月暂无收益数据
        </div>
      ) : (
        <div style={{ display: "flex", alignItems: "flex-end", gap: 6, height: 96 }}>
          {points.map((p) => {
            const isSel = isSameMonth(p.ym, selected);
            const hPct = barHeightPct(p.value, max);
            return (
              <button
                key={`${p.ym.year}-${p.ym.month}`}
                type="button"
                onClick={() => onPick(p.ym)}
                title={`${p.ym.year}年${p.ym.month}月 · ¥${fmt(p.value)}`}
                style={{
                  flex: 1,
                  display: "flex",
                  flexDirection: "column",
                  alignItems: "center",
                  justifyContent: "flex-end",
                  gap: 4,
                  height: "100%",
                  background: "transparent",
                  border: "none",
                  padding: 0,
                  cursor: "pointer",
                }}
              >
                <div
                  style={{
                    fontSize: 9,
                    color: isSel ? "#2B2721" : "#A89680",
                    fontWeight: isSel ? 600 : 400,
                    whiteSpace: "nowrap",
                    minHeight: 12,
                  }}
                >
                  {p.value > 0 ? shortMoney(p.value) : ""}
                </div>
                <div
                  style={{
                    width: "100%",
                    maxWidth: 34,
                    height: `${Math.max(hPct, p.value > 0 ? 4 : 1)}%`,
                    minHeight: p.value > 0 ? 4 : 2,
                    background: isSel ? "#2B2721" : "#D8C9AD",
                    borderRadius: 5,
                    transition: "height 200ms ease, background 200ms ease",
                  }}
                />
                <div style={{ fontSize: 10, color: isSel ? "#2B2721" : "#A89680", fontWeight: isSel ? 600 : 400 }}>
                  {p.ym.month}月
                </div>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

// 柱顶金额标注：压缩成「1.2万 / 3800」这类短串，避免窄柱上挤不下完整千分位。
function shortMoney(v: number): string {
  if (v >= 10000) return `${(v / 10000).toFixed(v >= 100000 ? 0 : 1)}万`;
  return String(Math.round(v));
}
