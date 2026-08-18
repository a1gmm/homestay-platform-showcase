# 岸屿 ÀN·YŬ 设计系统

民宿/公寓运营系统的视觉语言。三端(业主后台 / 客户端 / 员工端)共享,差异只在**信息密度**。

## 品牌

- 中文名:**岸屿**(海岸 + 岛屿)
- 罗马字:**ÀN · YŬ**
- 语调:克制、留白、东方、静。禁感叹号 / emoji / 营销词

## 调色板(8 + 2,禁扩展)

| Token | 值 | 用途 |
|---|---|---|
| `--shell` | `#FBF8F1` | 主背景(页面 + 卡片) |
| `--sand` | `#F5F1EA` | 次级背景 / 分隔区 / 表头 |
| `--linen` | `#E5DDCB` | 分割线 / 边框 `0.5px` |
| `--driftwood` | `#A89680` | 弱化文字 / 占位 / 禁用 |
| `--stone` | `#6B665B` | 次级文字 / 图标 |
| `--ink` | `#2B2721` | 主色 / 主文字 / 主按钮底 |
| `--ink-hover` | `#1A1612` | Ink 悬停 |
| `--sage` | `#7B8578` | 语义:成功 / 可用 / 已支付 |
| `--clay` | `#8A6E5A` | 语义:警告 / 待处理 |

**禁用**:蓝紫红黄任何品牌彩色、纯黑 `#000`、纯白 `#FFF`、渐变、彩色填充胶囊。

### 功能色例外(data-viz)

上面的禁色只约束**品牌界面**(按钮 / Hero / 营销 / 卡片 / 状态标签胶囊)。**功能性数据可视化**——即必须靠颜色区分**类目**、且类目数超过 sage/clay 两色所能承载的场景——可使用一套独立的、可比品牌色更鲜明的「功能色板」。

- 适用:运营甘特图的**订单状态条**、仪表盘图表的多序列。
- 现状真相源:`components/rooms/GanttView.tsx` / `MobileGantt.tsx` / `components/owner/OwnerGanttView.tsx` 里的 `STATUS_COLOR`(待确认/待排房/预抵/在住/待退房/已完成 + 维修/锁房),三处取值保持一致;图例在 `MobileGantt.tsx`。
- 边界:功能色**只许出现在图表/甘特条本体**,不得外溢到按钮、文字、边框、背景等品牌表面。前台房态甘特以「一眼可辨」为第一优先,显眼度优先于品牌克制。
- 这不是违规,是被本规范正式承认的例外;改这块颜色无须「去蓝紫红黄」,但三处取值要同步。

## 字体

- Sans:`var(--font-inter)` + PingFang SC fallback(`--font-sans`)
- Serif:`Cormorant Garamond` + Noto Serif SC + Songti SC(`--font-serif`)
- 加载方式:`next/font/google`(见 `app/layout.tsx`)

### 排版规则

| 场景 | 字体 | 字号 | 字重 | letter-spacing | line-height |
|---|---|---|---|---|---|
| H1 | serif | 32-52 | 400 | 0 | 1.4 |
| H2 | serif | 22-30 | 400 | 0 | 1.4 |
| 卡片标题 / 房源名 | serif | 17-22 | 400 | 0 | 1.4 |
| 价格 / 数字 / 订单号 | serif | 跟随场景 | 400 | 0 | — |
| 正文 | sans | 12-14 | 400 | 0 | 1.8-2.0 |
| 英文大写小标签 | sans | 10 | 400 | **0.2em** | — |

**铁律**:
- CJK `letter-spacing` 永远 `0`
- 只有英文大写标签用 `0.2em`
- CJK **不用 italic**(宋体无真斜体)
- font-weight **不得 ≥ 600**(token 层已封顶 500)
- 数字 / 百分比 / 房号统一 serif

## 圆角

| Token | 值 | 用途 |
|---|---|---|
| `--radius-0` | `0` | Hero 图 / 表格单元 |
| `--radius-sm` | `8px` | 小图片 / 标签内 / 输入框(暂) |
| `--radius-md` | `12px` | 卡片 / 面板 |
| `--radius-lg` | `16px` | 大卡片 / Hero 区 / Modal |
| `--radius-pill` | `999px` | 按钮 / chip / 头像 |

**禁**:> 16px 的圆角(除 pill);部分圆角(只圆上两角)。

## 动效

- 缓动曲线统一:`var(--ease-breath)` = `cubic-bezier(0.2, 0.8, 0.2, 1)`
- 工具类:`.fade-up / .fade-up-2 / .fade-up-3`(900ms,3 层错位 120ms)
- 卡片 hover:`.card-hoverable` → `translateY(-4px)`
- 按钮 active:`scale(0.97)`(全局 Ant 覆盖)

## 工具类

| 类 | 用途 |
|---|---|
| `.serif` | 宋体/衬线 + CJK 标点压缩 |
| `.en-label` | 英文大写小标签(10px / 0.2em / driftwood) |
| `.fade-up` / `-2` / `-3` | 进场动画 3 层错位 |
| `.card-hoverable` | 卡片 hover `translateY(-4px)` |
| `.status-dot` | 小圆点 + 文字(禁彩色胶囊) |
| `.link-underline` | 下划线 hover 扩展 |

## 组件约定

- **按钮**:一律 pill(`999px`),主按钮 Ink 底 Shell 字,文案"动词 + 箭头"(如"立即预订 →")
- **输入框**:本轮 PR 暂保留方框(8px 圆角 + Ink 激活边),下一 PR 改下划线 wrapper
- **卡片**:`12-16px` 圆角,无阴影,`0.5px` linen 边
- **状态**:小圆点 + 文字(sage/clay/stone),不用胶囊
- **订单号**:`ORD · 20260420 · 01` 格式(中点分隔)
- **价格**:`¥488 / 晚`,serif,不用"488 元/晚"
- **日期**:`04.20 → 04.22`

## 架构

- 令牌定义:`lib/design-tokens.ts`(新 `tokens.anyu.*` 规范化树 + 旧键值重映射兼容层)
- Ant Design 主题:`app/providers.tsx`(ConfigProvider)
- 全局 CSS 变量 + Ant 覆盖 + 工具类:`app/globals.css`
- 字体加载:`app/layout.tsx`(`next/font/google`)

## 迁移状态

### 已完成(基建 PR + 组件层 PR)

- [x] 颜色/圆角/字体/动效 token 重映射(`lib/design-tokens.ts`)
- [x] 字体加载(Inter + Cormorant Garamond + Noto Serif SC via `next/font/google`)
- [x] Ant ConfigProvider 完整主题(pill 按钮 / shell 底 / 无阴影)
- [x] `globals.css` 重写(工具类 + Ant 覆盖 + breath 曲线)
- [x] 4 个 layout shell(`(dashboard)` / `booking` / `staff` / `owner`)去蓝去白
- [x] 原子组件:`EnLabel` / `Serif` / `MobileHero`(`components/ui/`)
- [x] 6 处移动端 hero 蓝紫渐变 → 墨底 + 金沙 en-label
- [x] 所有 `#2E5CFF` 硬编码蓝清除(27 处 → 0,文档引用除外)
- [x] Dashboard 图表调色板 → 岸屿 6 色(ink/stone/sage/clay/driftwood/text-secondary)
- [x] RoomCard `STATUS_ACCENT` → 岸屿语义色
- [x] `rooms/constants.ts` 彩色状态 bg → 米色系
- [x] `EmptyState` SVG 插画 → 墨底米字
- [x] Login 页蓝紫营销面板 → 墨底 serif "岸屿 ÀN·YŬ"
- [x] Owner/Staff/Booking 的 "我的"页、结算页、房间详情页主要白卡 → 沙色 + 0.5px linen 边

### 下一轮候选(未做)

- [ ] `<Button>` wrapper(pill + 箭头图标原子)
- [ ] `<Input variant="underline">` wrapper(下划线输入框)
- [ ] `<Card variant="shell|sand">` wrapper + hover lift
- [ ] `<KpiCard>`(升级现有 `StatCard`)
- [ ] `<AdminTable>`(升级现有 Ant Table 样式)
- [ ] Booking 预订新单页 / 房间详情页 / 三端 login 的白卡清理(约 20 处 `#fff` inline 残留)
- [ ] `Tag color="blue|green|red"` 改成灰调 neutral Tag
- [ ] 微观色值 `#888`/`#999`/`#aaa`/`#666` → 统一到 token `--text-muted`/`--text-secondary`

### 后续阶段

- PR3 C 端客户:Discover → Detail → Checkout → Payment → Orders
- PR4 B 端业主:Overview → Properties → Orders → Staff → Finance → System
- PR5 C 端员工:今日任务 → 任务详情 → 客户资料 → 工单上报
- PR6 打磨:`/design-review` 跑一次 visual QA

## 移动端文本截断（竖排单字根因）

中文在窄屏被压成"一字一行"的竖排单字，根因是 flex/grid 子项默认 `min-width:auto`，不会收缩到内容宽度以下。规范：

- 任何**可能被压缩、需要截断**的文本节点，用 `lib/text.ts` 的工具：
  - `ellipsis` —— 单行截断 + 省略号（标题、名称）
  - `clamp(n)` —— 多行截断（描述、地址）
  - `flexShrinkText` —— 铺在包裹文本的 flex 子项上，允许收缩（`min-width:0`）
- 页面头部一律用 `components/ui/PageHeader`（移动端自动「标题独占行 + 操作下移」，不会竖排）。
- 标题类文本可加 `overflowWrap:"anywhere"` 兜底：超宽时换行而非竖排。

## 移动端令牌（`tokens.layout`）

- `touchTarget: 44` —— 最小触控目标 44×44
- `bottomNavHeight: 64` / `fabSize: 56` —— 底部导航与中央悬浮按钮
- 输入框字号 ≥16px（防 iOS 聚焦缩放）

## 常见错误自查

- [ ] 品牌界面没有蓝 / 紫 / 鲜红 / 鲜黄(甘特状态条 / 图表序列等功能色见「功能色例外」,不在此列)
- [ ] 没有 `box-shadow`,只有 `0.5px` linen border
- [ ] 没有 `font-weight ≥ 600`
- [ ] CJK `letter-spacing: 0`,英文大写标签 `0.2em`
- [ ] CJK 不用 italic
- [ ] 所有按钮是 pill(`999px`)
- [ ] 状态用小圆点 + 文字,不用彩色胶囊
- [ ] 所有可点击元素有 hover 态
- [ ] 价格 / 数字 / 房号用 serif
- [ ] 订单号用 `ORD · 20260420 · 01` 格式
- [ ] 没有"热销""特惠"等营销词
