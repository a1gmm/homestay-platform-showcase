"use client";

import { useState, type JSX } from "react";
import Link from "next/link";
import {
  DashboardOutlined,
  HomeOutlined,
  FileTextOutlined,
  TeamOutlined,
  DollarOutlined,
  CheckSquareOutlined,
  FileDoneOutlined,
  SettingOutlined,
  RightOutlined,
} from "@ant-design/icons";
import { PageHeader } from "@/components/ui/PageHeader";
import { useIsMobile } from "@/lib/responsive";
import { tokens } from "@/lib/design-tokens";

const INK = tokens.color.text.primary;
const SUB = tokens.color.text.secondary;
const MUTED = tokens.color.text.tertiary;
const BORDER = tokens.color.bg.border;
const SAND = tokens.color.bg.subtle;
const ACCENT = tokens.color.status.active; // 岸屿 sage
const ACCENT_SOFT = tokens.color.status.activeSoft;

// ————————————————————————————————————————————————
// 第一块：订单全流程（可点击的状态图）
// ————————————————————————————————————————————————
const FLOW: {
  key: string;
  label: string;
  mean: string;
  action: string;
  next: string;
}[] = [
  {
    key: "1",
    label: "待确认",
    mean: "刚建好的新订单，客人还没付钱。",
    action: "跟客人确认，收订金或房费。",
    next: "收款后 →「已收款待排房」",
  },
  {
    key: "2",
    label: "已收款待排房",
    mean: "钱收到了，但还没定客人住哪一间。",
    action: "在订单详情里点「排房」，选一间空房。",
    next: "排好房 →「已排房待入住」",
  },
  {
    key: "3",
    label: "已排房待入住",
    mean: "房间定好了，就等客人到店。",
    action: "客人到了，在订单详情办理入住。",
    next: "入住后 →「已入住」",
  },
  {
    key: "4",
    label: "已入住",
    mean: "客人正在住店。",
    action: "到退房那天，办理退房、结清尾款。",
    next: "退房 →「待退房 / 已完成」",
  },
  {
    key: "5",
    label: "待退房",
    mean: "到期该走了，可能还有尾款没结。",
    action: "确认客人离店、收齐余款。",
    next: "结清 →「已完成」",
  },
  {
    key: "6",
    label: "已完成",
    mean: "这一单彻底结束，计入当月收入。",
    action: "不用再做什么，可在报表里查到它。",
    next: "流程结束 ✓",
  },
];

function OrderFlow() {
  const isMobile = useIsMobile();
  const [active, setActive] = useState(0);
  const cur = FLOW[active];
  return (
    <div>
      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          alignItems: "stretch",
          gap: 8,
          rowGap: 10,
        }}
      >
        {FLOW.map((s, i) => {
          const on = i === active;
          const done = i < active;
          return (
            <div key={s.key} style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <button
                onClick={() => setActive(i)}
                style={{
                  cursor: "pointer",
                  border: `1.5px solid ${on ? ACCENT : BORDER}`,
                  background: on ? ACCENT_SOFT : done ? SAND : "#fff",
                  color: on ? INK : done ? SUB : INK,
                  borderRadius: 999,
                  padding: "7px 14px",
                  fontSize: 13,
                  fontWeight: on ? tokens.font.weight.semibold : tokens.font.weight.regular,
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 7,
                  transition: "all .15s ease",
                  whiteSpace: "nowrap",
                }}
              >
                <span
                  style={{
                    width: 18,
                    height: 18,
                    borderRadius: 999,
                    background: on ? ACCENT : done ? tokens.color.gray[400] : SAND,
                    color: on || done ? "#fff" : MUTED,
                    fontSize: 11,
                    fontWeight: tokens.font.weight.semibold,
                    display: "inline-flex",
                    alignItems: "center",
                    justifyContent: "center",
                  }}
                >
                  {done ? "✓" : s.key}
                </span>
                {s.label}
              </button>
              {i < FLOW.length - 1 && !isMobile && (
                <RightOutlined style={{ fontSize: 10, color: MUTED }} />
              )}
            </div>
          );
        })}
      </div>

      <div
        style={{
          marginTop: 16,
          background: "#fff",
          border: `1px solid ${BORDER}`,
          borderRadius: 12,
          padding: 18,
          display: "flex",
          flexDirection: isMobile ? "column" : "row",
          gap: isMobile ? 12 : 20,
        }}
      >
        <div style={{ flex: 1 }}>
          <div style={{ fontSize: 12, color: MUTED, marginBottom: 4 }}>这个状态是什么意思</div>
          <div style={{ fontSize: 15, color: INK, fontWeight: tokens.font.weight.semibold }}>
            {cur.label}
          </div>
          <div style={{ marginTop: 4, fontSize: 14, color: SUB, lineHeight: 1.6 }}>{cur.mean}</div>
        </div>
        <div
          style={{
            flex: 1,
            paddingLeft: isMobile ? 0 : 20,
            borderLeft: isMobile ? "none" : `1px solid ${BORDER}`,
            paddingTop: isMobile ? 12 : 0,
            borderTop: isMobile ? `1px solid ${BORDER}` : "none",
          }}
        >
          <div style={{ fontSize: 12, color: MUTED, marginBottom: 4 }}>你要做的</div>
          <div style={{ fontSize: 14, color: INK, lineHeight: 1.6 }}>{cur.action}</div>
          <div
            style={{
              marginTop: 10,
              display: "inline-block",
              fontSize: 12.5,
              color: tokens.color.status.active,
              background: ACCENT_SOFT,
              borderRadius: 999,
              padding: "3px 10px",
            }}
          >
            {cur.next}
          </div>
        </div>
      </div>
      <div style={{ marginTop: 10, fontSize: 12.5, color: MUTED, lineHeight: 1.6 }}>
        提示：任何阶段都能「取消」订单——取消后会保留记录、但不计入收入。
      </div>
    </div>
  );
}

// ————————————————————————————————————————————————
// 第二块：常用操作图文分步（点步骤，图上高亮）
// ————————————————————————————————————————————————
function Highlight({ show, x, y, w, h, r = 8 }: { show: boolean; x: number; y: number; w: number; h: number; r?: number }) {
  if (!show) return null;
  return (
    <rect
      x={x}
      y={y}
      width={w}
      height={h}
      rx={r}
      fill={ACCENT_SOFT}
      stroke={ACCENT}
      strokeWidth={2}
    >
      <animate attributeName="opacity" values="0.5;1;0.5" dur="1.6s" repeatCount="indefinite" />
    </rect>
  );
}

// —— 新建订单 界面示意 ——
function NewOrderArt({ active }: { active: string }) {
  return (
    <svg viewBox="0 0 300 210" width="100%" style={{ display: "block" }}>
      <rect x="4" y="4" width="292" height="202" rx="14" fill="#fff" stroke={BORDER} />
      <text x="20" y="30" fontSize="13" fontWeight="600" fill={INK}>新建订单</text>
      <line x1="16" y1="42" x2="284" y2="42" stroke={BORDER} />
      {/* 客人信息 */}
      <Highlight show={active === "guest"} x={14} y={50} w={272} h={34} />
      <text x="24" y="64" fontSize="10" fill={MUTED}>客人姓名 / 手机号</text>
      <rect x="24" y="70" width="120" height="10" rx="3" fill={SAND} />
      <rect x="152" y="70" width="120" height="10" rx="3" fill={SAND} />
      {/* 日期 */}
      <Highlight show={active === "date"} x={14} y={90} w={272} h={30} />
      <text x="24" y="103" fontSize="10" fill={MUTED}>入住 / 退房日期</text>
      <rect x="24" y="108" width="90" height="10" rx="3" fill={SAND} />
      <rect x="122" y="108" width="90" height="10" rx="3" fill={SAND} />
      {/* 房间 */}
      <Highlight show={active === "room"} x={14} y={126} w={134} h={30} />
      <text x="24" y="139" fontSize="10" fill={MUTED}>房间（可先留空）</text>
      <rect x="24" y="144" width="110" height="10" rx="3" fill={SAND} />
      {/* 价格 */}
      <Highlight show={active === "price"} x={152} y={126} w={134} h={30} />
      <text x="162" y="139" fontSize="10" fill={MUTED}>价格</text>
      <rect x="162" y="144" width="110" height="10" rx="3" fill={SAND} />
      {/* 保存 */}
      <Highlight show={active === "save"} x={196} y={168} w={90} h={30} r={999} />
      <rect x="200" y="172" width="82" height="22" rx="11" fill={active === "save" ? ACCENT : INK} />
      <text x="241" y="187" fontSize="11" fill="#fff" textAnchor="middle" fontWeight="600">保存</text>
    </svg>
  );
}

// —— 排房 界面示意 ——
function AssignRoomArt({ active }: { active: string }) {
  const rooms = [
    { n: "201", busy: true },
    { n: "202", busy: false },
    { n: "203", busy: false },
    { n: "301", busy: true },
    { n: "302", busy: false },
    { n: "303", busy: false },
  ];
  return (
    <svg viewBox="0 0 300 210" width="100%" style={{ display: "block" }}>
      <rect x="4" y="4" width="292" height="202" rx="14" fill="#fff" stroke={BORDER} />
      {/* 待排房订单 */}
      <Highlight show={active === "find"} x={14} y={16} w={272} h={40} r={10} />
      <rect x="20" y="22" width="260" height="28" rx="8" fill={SAND} />
      <circle cx="38" cy="36" r="8" fill={tokens.color.gray[300]} />
      <text x="54" y="34" fontSize="11" fontWeight="600" fill={INK}>王先生</text>
      <text x="54" y="46" fontSize="9" fill={MUTED}>7/8–7/9 · 待排房</text>
      <rect x="222" y="28" width="50" height="16" rx="8" fill={tokens.color.status.pendingSoft} />
      <text x="247" y="39" fontSize="9" fill={tokens.color.status.pending} textAnchor="middle">待排房</text>
      {/* 选房间 */}
      <text x="20" y="76" fontSize="10" fill={MUTED}>选一间当天空着的房</text>
      {rooms.map((room, i) => {
        const col = i % 3;
        const row = Math.floor(i / 3);
        const x = 20 + col * 90;
        const y = 84 + row * 54;
        return (
          <g key={room.n}>
            <Highlight show={active === "pick" && !room.busy} x={x} y={y} w={80} h={44} r={9} />
            <rect
              x={x + 3}
              y={y + 3}
              width={74}
              height={38}
              rx={7}
              fill={room.busy ? SAND : "#fff"}
              stroke={room.busy ? BORDER : ACCENT}
              strokeWidth={room.busy ? 1 : 1.5}
            />
            <text x={x + 40} y={y + 20} fontSize="12" fontWeight="600" textAnchor="middle" fill={room.busy ? MUTED : INK}>
              {room.n}
            </text>
            <text x={x + 40} y={y + 33} fontSize="8.5" textAnchor="middle" fill={room.busy ? MUTED : ACCENT}>
              {room.busy ? "已占" : "可选"}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

// —— 收款 界面示意 ——
function PaymentArt({ active }: { active: string }) {
  const methods = ["微信", "支付宝", "现金"];
  return (
    <svg viewBox="0 0 300 210" width="100%" style={{ display: "block" }}>
      <rect x="4" y="4" width="292" height="202" rx="14" fill="#fff" stroke={BORDER} />
      <text x="20" y="30" fontSize="13" fontWeight="600" fill={INK}>收款</text>
      <line x1="16" y1="42" x2="284" y2="42" stroke={BORDER} />
      {/* 金额 */}
      <Highlight show={active === "amount"} x={14} y={54} w={272} h={44} />
      <text x="24" y="70" fontSize="10" fill={MUTED}>金额</text>
      <text x="24" y="92" fontSize="22" fontWeight="700" fill={INK}>¥ 480</text>
      {/* 方式 */}
      <Highlight show={active === "method"} x={14} y={108} w={272} h={40} />
      <text x="24" y="122" fontSize="10" fill={MUTED}>支付方式</text>
      {methods.map((m, i) => {
        const on = i === 0;
        const x = 24 + i * 86;
        return (
          <g key={m}>
            <rect
              x={x}
              y={128}
              width={78}
              height={16}
              rx={8}
              fill={on ? ACCENT_SOFT : "#fff"}
              stroke={on ? ACCENT : BORDER}
            />
            <text x={x + 39} y={139} fontSize="10" textAnchor="middle" fill={on ? INK : SUB}>
              {m}
            </text>
          </g>
        );
      })}
      {/* 确认 */}
      <Highlight show={active === "confirm"} x={196} y={162} w={90} h={32} r={999} />
      <rect x="200" y="166" width="82" height="24" rx="12" fill={active === "confirm" ? ACCENT : INK} />
      <text x="241" y="182" fontSize="11" fill="#fff" textAnchor="middle" fontWeight="600">确认收款</text>
    </svg>
  );
}

type Walk = {
  title: string;
  Art: (p: { active: string }) => JSX.Element;
  steps: { text: string; spot: string }[];
};

const WALKS: Walk[] = [
  {
    title: "新建一个订单",
    Art: NewOrderArt,
    steps: [
      { text: "填客人姓名和手机号", spot: "guest" },
      { text: "选入住 / 退房日期", spot: "date" },
      { text: "选房间（还没定就先留空）", spot: "room" },
      { text: "填价格", spot: "price" },
      { text: "点「保存」，订单就建好了", spot: "save" },
    ],
  },
  {
    title: "给「待排房」的订单排房",
    Art: AssignRoomArt,
    steps: [
      { text: "在订单里找到「待排房」那一单", spot: "find" },
      { text: "从当天空着的房里选一间（灰的是已占）", spot: "pick" },
    ],
  },
  {
    title: "记一笔收款",
    Art: PaymentArt,
    steps: [
      { text: "填这次收多少钱", spot: "amount" },
      { text: "选客人用的支付方式", spot: "method" },
      { text: "点「确认收款」，自动记进账", spot: "confirm" },
    ],
  },
];

function Walkthrough({ walk }: { walk: Walk }) {
  const isMobile = useIsMobile();
  const [step, setStep] = useState(0);
  const { Art } = walk;
  const active = walk.steps[step].spot;
  return (
    <div
      style={{
        background: "#fff",
        border: `1px solid ${BORDER}`,
        borderRadius: 14,
        padding: 16,
        marginBottom: 14,
      }}
    >
      <div style={{ fontSize: 15, fontWeight: tokens.font.weight.semibold, color: INK, marginBottom: 12 }}>
        {walk.title}
      </div>
      <div
        style={{
          display: "flex",
          flexDirection: isMobile ? "column" : "row",
          gap: 16,
          alignItems: isMobile ? "stretch" : "flex-start",
        }}
      >
        {/* 界面示意图 */}
        <div
          style={{
            flex: isMobile ? "none" : "0 0 46%",
            background: SAND,
            borderRadius: 12,
            padding: 12,
          }}
        >
          <Art active={active} />
        </div>
        {/* 步骤 */}
        <ol style={{ flex: 1, margin: 0, padding: 0, listStyle: "none", display: "flex", flexDirection: "column", gap: 8 }}>
          {walk.steps.map((s, i) => {
            const on = i === step;
            return (
              <li key={i}>
                <button
                  onClick={() => setStep(i)}
                  style={{
                    width: "100%",
                    textAlign: "left",
                    cursor: "pointer",
                    display: "flex",
                    alignItems: "flex-start",
                    gap: 10,
                    background: on ? ACCENT_SOFT : "transparent",
                    border: `1px solid ${on ? ACCENT : "transparent"}`,
                    borderRadius: 10,
                    padding: "9px 11px",
                    transition: "all .15s ease",
                  }}
                >
                  <span
                    style={{
                      flex: "0 0 22px",
                      width: 22,
                      height: 22,
                      borderRadius: 999,
                      background: on ? ACCENT : SAND,
                      color: on ? "#fff" : MUTED,
                      fontSize: 12,
                      fontWeight: tokens.font.weight.semibold,
                      display: "inline-flex",
                      alignItems: "center",
                      justifyContent: "center",
                      marginTop: 1,
                    }}
                  >
                    {i + 1}
                  </span>
                  <span style={{ fontSize: 14, color: on ? INK : SUB, lineHeight: 1.5 }}>{s.text}</span>
                </button>
              </li>
            );
          })}
        </ol>
      </div>
    </div>
  );
}

// ————————————————————————————————————————————————
// 第三块：功能模块概览
// ————————————————————————————————————————————————
const MODULES: { icon: JSX.Element; name: string; desc: string; menu: string; adminOnly?: boolean }[] = [
  { icon: <DashboardOutlined />, name: "今日概览", desc: "进系统第一眼看到的地方，汇总今天的入住 / 退房、收入和待办。", menu: "今日概览" },
  { icon: <HomeOutlined />, name: "房态管理", desc: "所有房间的当前状态和日历，用来安排、调整客人住哪间。", menu: "房态管理" },
  { icon: <FileTextOutlined />, name: "订单管理", desc: "从新建、修改到取消、完成，一条订单的整个过程都在这里。", menu: "订单管理" },
  { icon: <TeamOutlined />, name: "客人档案", desc: "回头客的资料和历史订单，方便查老客人住过几次。", menu: "客人档案" },
  { icon: <DollarOutlined />, name: "财务管理", desc: "收款、退款、支出的账本，用来算真实的收入和利润。", menu: "财务管理" },
  { icon: <CheckSquareOutlined />, name: "运营任务", desc: "打扫、补货、维修这些要做的事，派给谁、做没做，一目了然。", menu: "运营任务" },
  { icon: <FileDoneOutlined />, name: "业主结算", desc: "给每位业主出分成报表，算清各自该拿多少。", menu: "业主结算", adminOnly: true },
  { icon: <SettingOutlined />, name: "系统管理", desc: "配置业主分成、管理登录账号、查看操作日志。仅管理员可用。", menu: "系统管理", adminOnly: true },
];

function SectionTitle({ index, text, hint }: { index: string; text: string; hint: string }) {
  return (
    <div style={{ display: "flex", alignItems: "baseline", gap: 10, marginBottom: 14, flexWrap: "wrap" }}>
      <span
        style={{
          flex: "0 0 auto",
          width: 24,
          height: 24,
          borderRadius: 8,
          background: tokens.color.brand.primary,
          color: tokens.color.text.inverse,
          fontSize: 13,
          fontWeight: tokens.font.weight.semibold,
          display: "inline-flex",
          alignItems: "center",
          justifyContent: "center",
          transform: "translateY(3px)",
        }}
      >
        {index}
      </span>
      <span style={{ fontSize: 16, fontWeight: tokens.font.weight.semibold, color: INK }}>{text}</span>
      <span style={{ fontSize: 12, color: MUTED }}>{hint}</span>
    </div>
  );
}

export default function HelpPage() {
  const isMobile = useIsMobile();
  return (
    <div style={{ maxWidth: 960, margin: "0 auto" }}>
      <PageHeader
        title="使用说明"
        subtitle="不用死记——跟着图点一遍就会。先看订单是怎么一步步走完的，再照着常用操作的示意图做，最后是每个菜单的速查。"
      />

      <div style={{ marginBottom: 36 }}>
        <SectionTitle index="1" text="订单是怎么走完的" hint="点每个状态，看含义和下一步" />
        <OrderFlow />
      </div>

      <div style={{ marginBottom: 36 }}>
        <SectionTitle index="2" text="常用操作 · 跟着图做" hint="点左边的步骤，右图会高亮对应位置" />
        {WALKS.map((w) => (
          <Walkthrough key={w.title} walk={w} />
        ))}
      </div>

      <div>
        <SectionTitle index="3" text="功能模块 · 每个菜单是干嘛的" hint="对应左侧（手机在底部）的菜单" />
        <div
          style={{
            display: "grid",
            gridTemplateColumns: isMobile ? "1fr" : "repeat(auto-fit, minmax(280px, 1fr))",
            gap: 12,
          }}
        >
          {MODULES.map((m) => (
            <div
              key={m.name}
              style={{
                background: "#fff",
                border: `1px solid ${BORDER}`,
                borderRadius: 12,
                padding: 16,
                display: "flex",
                gap: 12,
                alignItems: "flex-start",
              }}
            >
              <div
                style={{
                  flex: "0 0 40px",
                  width: 40,
                  height: 40,
                  borderRadius: 10,
                  background: SAND,
                  color: INK,
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  fontSize: 18,
                }}
              >
                {m.icon}
              </div>
              <div style={{ minWidth: 0 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 15, fontWeight: tokens.font.weight.semibold, color: INK }}>
                  {m.name}
                  {m.adminOnly && (
                    <span
                      style={{
                        fontSize: 11,
                        fontWeight: tokens.font.weight.regular,
                        color: MUTED,
                        background: SAND,
                        border: `1px solid ${BORDER}`,
                        borderRadius: 999,
                        padding: "0 6px",
                      }}
                    >
                      管理员
                    </span>
                  )}
                </div>
                <div style={{ marginTop: 4, fontSize: 13, color: SUB, lineHeight: 1.55 }}>{m.desc}</div>
                <div style={{ marginTop: 6, fontSize: 12, color: MUTED }}>菜单：{m.menu}</div>
              </div>
            </div>
          ))}
        </div>
      </div>

      <div
        style={{
          marginTop: 32,
          padding: isMobile ? 16 : 18,
          background: SAND,
          border: `1px solid ${BORDER}`,
          borderRadius: 12,
          fontSize: 13,
          color: SUB,
          lineHeight: 1.6,
        }}
      >
        还是不清楚某个操作？可以先在「今日概览」看当天要处理的事，或直接联系管理员。
        <Link href="/dashboard" style={{ marginLeft: 8, color: INK, fontWeight: tokens.font.weight.medium, textDecoration: "none" }}>
          回到今日概览 <RightOutlined style={{ fontSize: 10 }} />
        </Link>
      </div>
    </div>
  );
}
