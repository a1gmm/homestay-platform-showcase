"use client";

import { useEffect, useMemo, useState, type CSSProperties } from "react";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { Button, DatePicker, Result, Segmented, Skeleton } from "antd";
import { LeftOutlined, RightOutlined } from "@ant-design/icons";
import dayjs, { type Dayjs } from "dayjs";
import { ownerDataApi, type OwnerCalendarRoom } from "@/lib/owner-api";
import { useOwnerStore } from "@/lib/owner-store";
import { extractErrorMessage } from "@/lib/api";
import { useIsDesktop } from "@/lib/responsive";
import { OwnerGanttView, OwnerDailyList } from "@/components/owner";
import { tokens } from "@/lib/design-tokens";
import { groupByOwnerName } from "@/lib/owner-grouping";

type Layout = "gantt" | "daily";

// 月份导航按钮：44×44 触控目标（图标本身仍是小尺寸，视觉不臃肿）。
const calNavBtnStyle: CSSProperties = {
  width: 44,
  height: 44,
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  background: "transparent",
  border: "none",
  borderRadius: 8,
  color: tokens.anyu.color.ink.default,
  cursor: "pointer",
  padding: 0,
};

export default function OwnerCalendarPage() {
  const router = useRouter();
  const isLoggedIn = useOwnerStore((s) => s.isLoggedIn());
  const owner = useOwnerStore((s) => s.owner);
  const isMaster = !!owner?.is_master;
  const today = new Date();
  const [calMonth, setCalMonth] = useState({
    year: today.getFullYear(),
    month: today.getMonth() + 1,
  });
  const [layout, setLayout] = useState<Layout>("gantt");
  const isDesktop = useIsDesktop();

  useEffect(() => {
    if (!isLoggedIn) {
      router.replace(`/owner/login?next=${encodeURIComponent("/owner/calendar")}`);
    }
  }, [isLoggedIn, router]);

  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["owner", "calendar", calMonth.year, calMonth.month],
    queryFn: async () =>
      (await ownerDataApi.calendar(calMonth.year, calMonth.month)).data,
    enabled: isLoggedIn,
    staleTime: 60 * 1000,
  });

  const prevMonth = () =>
    setCalMonth((p) => {
      const d = new Date(p.year, p.month - 2, 1);
      return { year: d.getFullYear(), month: d.getMonth() + 1 };
    });
  const nextMonth = () =>
    setCalMonth((p) => {
      const d = new Date(p.year, p.month, 1);
      return { year: d.getFullYear(), month: d.getMonth() + 1 };
    });

  if (!isLoggedIn) return null;

  return (
    <div
      style={{
        padding: isDesktop ? "24px 26px 40px" : "20px 14px 80px",
        display: "flex",
        flexDirection: "column",
        gap: 16,
      }}
    >
      {/* Header */}
      <div
        style={{
          display: "flex",
          alignItems: "baseline",
          justifyContent: "space-between",
          gap: 8,
        }}
      >
        <h1
          style={{
            margin: 0,
            fontSize: isDesktop ? 24 : 22,
            fontFamily: tokens.anyu.font.serif,
            fontWeight: 400,
            color: tokens.anyu.color.ink.default,
            letterSpacing: 0,
          }}
        >
          房态
        </h1>
        <span style={{ fontSize: 11, color: tokens.anyu.color.driftwood }}>
          仅展示占用 · 不含客人信息
        </span>
      </div>

      {/* 月份导航 + Segmented */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 8,
          flexWrap: "wrap",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 2 }}>
          <button
            type="button"
            aria-label="上一月"
            onClick={prevMonth}
            style={calNavBtnStyle}
          >
            <LeftOutlined style={{ fontSize: 13 }} />
          </button>
          <span
            style={{
              fontSize: 14,
              fontWeight: 500,
              minWidth: 90,
              textAlign: "center",
              color: tokens.anyu.color.ink.default,
              fontFamily: tokens.anyu.font.serif,
            }}
          >
            {calMonth.year} 年 {calMonth.month} 月
          </span>
          <button
            type="button"
            aria-label="下一月"
            onClick={nextMonth}
            style={calNavBtnStyle}
          >
            <RightOutlined style={{ fontSize: 13 }} />
          </button>
          <DatePicker
            picker="month"
            value={dayjs(`${calMonth.year}-${String(calMonth.month).padStart(2, "0")}-01`)}
            onChange={(d: Dayjs | null) => {
              if (d) setCalMonth({ year: d.year(), month: d.month() + 1 });
            }}
            allowClear={false}
            style={{ width: 120, marginLeft: 4 }}
          />
        </div>
        <Segmented
          value={layout}
          onChange={(v) => setLayout(v as Layout)}
          options={[
            { label: "甘特", value: "gantt" },
            { label: "单日", value: "daily" },
          ]}
          style={{ minHeight: 44, display: "flex", alignItems: "center" }}
        />
      </div>

      {/* 主体 */}
      {isError ? (
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
      ) : isLoading || !data ? (
        <Skeleton active paragraph={{ rows: 6 }} />
      ) : isMaster ? (
        <OwnerGroupedCalendar rooms={data} calMonth={calMonth} layout={layout} />
      ) : layout === "gantt" ? (
        <OwnerGanttView rooms={data} calMonth={calMonth} />
      ) : (
        <OwnerDailyList rooms={data} rangeDays={14} />
      )}
    </div>
  );
}

// 总账号（is_master）：按 owner_name（= 子业主 = 楼层）分层，每层一个区块
// （深色 pill 楼层标题，样式对齐首页 OwnerGroupedRoomList），层内复用现有
// 甘特/单日视图组件，非 master 用户完全不会走到这个分支。
function OwnerGroupedCalendar({
  rooms,
  calMonth,
  layout,
}: {
  rooms: OwnerCalendarRoom[];
  calMonth: { year: number; month: number };
  layout: Layout;
}) {
  const groups = useMemo(() => groupByOwnerName(rooms, (r) => r.owner_name), [rooms]);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      {groups.map((g) => (
        <div key={g.ownerName}>
          <div
            style={{
              display: "flex",
              alignItems: "baseline",
              justifyContent: "space-between",
              padding: "8px 10px",
              marginBottom: 10,
              background: tokens.anyu.color.ink.default,
              borderRadius: 10,
            }}
          >
            <div
              className="serif"
              style={{ fontSize: 15, fontWeight: 400, color: tokens.anyu.color.shell, letterSpacing: 0 }}
            >
              {g.ownerName}
              <span
                style={{
                  fontSize: 11,
                  color: tokens.anyu.color.driftwood,
                  marginLeft: 8,
                  fontFamily: "var(--font-sans)",
                }}
              >
                {g.items.length} 套
              </span>
            </div>
          </div>
          {layout === "gantt" ? (
            <OwnerGanttView rooms={g.items} calMonth={calMonth} />
          ) : (
            <OwnerDailyList rooms={g.items} rangeDays={14} />
          )}
        </div>
      ))}
    </div>
  );
}
