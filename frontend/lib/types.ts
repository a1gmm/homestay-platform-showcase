// ─── Enums (mirror backend Python enums) ─────────────────────────────────────

// 12 个 active 渠道（客户拍板，新订单只能选这些）
export type ActiveChannel =
  | "ctrip"
  | "meituan_hotel"
  | "meituan_homestay"
  | "douyin"
  | "qunar"
  | "zhixing"
  | "tongcheng"
  | "self_acquired"
  | "offline"
  | "self_used"
  | "trial_stay"
  | "fliggy";

// 历史 legacy 渠道（前端下拉隐藏，只为兼容老订单显示）
export type LegacyChannel = "meituan" | "tujia" | "private" | "walk_in" | "direct";

export type Channel = ActiveChannel | LegacyChannel;

export type OrderStatus =
  | "pending_confirm"
  | "pending_payment"
  | "paid_pending_room"
  | "roomed_pending_checkin"
  | "checked_in"
  | "pending_checkout"
  | "completed"
  | "rescheduled"
  | "abnormal"
  | "cancelled";

export type PaymentStatus =
  | "unpaid"
  | "partial"
  | "paid"
  | "partial_refund"
  | "full_refund";

export type DepositStatus =
  | "not_collected"
  | "collected"
  | "returned"
  | "withheld";

export type CleaningStatus =
  | "not_assigned"
  | "assigned"
  | "in_progress"
  | "done"
  | "inspected";

export type RoomStatus =
  | "available"
  | "occupied"
  | "pending_clean"
  | "cleaning"
  | "maintenance"
  | "locked"
  | "reserved";

export type PaymentMethod =
  | "wechat"
  | "alipay"
  | "cash"
  | "bank_transfer"
  | "platform"
  | "other";

export type RefundReason =
  | "guest_cancel"
  | "platform_cancel"
  | "host_cancel"
  | "overcharge"
  | "complaint"
  | "other";

export type ExpenseCategory =
  | "cleaning"
  | "maintenance"
  | "utilities"
  | "supplies"
  | "platform_fee"
  | "tax"
  | "other"
  | "public_utilities"
  | "water"
  | "cold_water"
  | "broadband"
  | "daily_supplies"
  | "laundry"
  | "hot_water"
  | "gas"
  | "property_fee"
  | "electricity"
  | "kitchen_cleaning"
  | "property_guidance_fee"
  | "new_linen_prewash";

export type ExpensePayer = "company" | "owner";

export type TaskType =
  | "collect_deposit"
  | "cleaning"
  | "checkout_inspection"
  | "return_deposit"
  | "custom";

export type TaskStatus = "pending" | "in_progress" | "done";

export type TaskPriority = "low" | "medium" | "high" | "urgent";

export type UserRole = "admin" | "operator" | "finance" | "cleaner" | "owner";

export type StaySettlementKind = "free_room" | "company_sponsored";

export type ManualOverrideField =
  | "guest_name"
  | "guest_profile"
  | "check_in_date"
  | "check_out_date"
  | "room_assignment"
  | "stay_structure"
  | "actual_price"
  | "daily_prices"
  | "ota_owner_revenue"
  | "channel"
  | "note"
  | "order_status";

// ─── User ────────────────────────────────────────────────────────────────────

export interface User {
  user_id: string;
  username: string;
  display_name: string;
  role: UserRole;
  phone?: string | null;
  is_active: boolean;
}

// ─── Order ───────────────────────────────────────────────────────────────────

// issue#6: 订单类型 — 普通 / 试住（业主每月免费一晚）/ 自住（业主自住）
export type BookingType = "normal" | "trial" | "owner_self";

export const BOOKING_TYPE_LABEL: Record<BookingType, string> = {
  normal: "普通订单",
  trial: "试住单",
  owner_self: "自住单",
};

export const BOOKING_TYPE_TAG_COLOR: Record<BookingType, string> = {
  normal: "default",
  trial: "purple",
  owner_self: "cyan",
};

// issue#103 Step 3: 试住 / 自住单业主出钱，豁免「手机号 + 实收必填」校验
//（客人实付 list_price 已全局选填，不再进豁免清单）。
// 抽成单一真相，避免两个录单表单（orders/new、QuickCreateOrderModal）+ 编辑弹窗
// 各写一份 `bt === "trial" || bt === "owner_self"`，将来新增豁免类型时漏改导致规则不一致。
export const BOOKING_TYPE_EXEMPT: ReadonlySet<BookingType> = new Set<BookingType>([
  "trial",
  "owner_self",
]);

export function isPhonePriceExempt(
  bt: BookingType | string | null | undefined
): boolean {
  return bt != null && BOOKING_TYPE_EXEMPT.has(bt as BookingType);
}

// ─── Multi-room (多房订单) ──────────────────────────────────────────────────

export interface OrderRoomCreate {
  room_id?: string | null;  // null = 待排房
  check_in_date: string;
  check_out_date: string;
  list_price?: number | null;
  discount_amount?: number;
  actual_price?: number | null;
  guests_count?: number;
  position?: number;
  // 每房净房费（B 方案 2026-07-04）：多房平台单手填，落 OrderRoom.metadata。
  // 铁律：任一行填 ⇒ 全填 + Σ = 整单净房费（后端 422 兜底）。
  ota_owner_revenue?: number | null;
}

export interface OrderRoomOut {
  order_room_id: string;
  room_id: string | null;
  check_in_date: string;
  check_out_date: string;
  nights: number;
  list_price: number | null;
  discount_amount: number;
  actual_price: number | null;
  guests_count: number;
  position: number;
  // 单间入住/退房标记（per-room）：NULL=未入住/未退。前端据此逐间办理入住/退房、判整单收尾。
  checked_in_at?: string | null;
  checked_out_at?: string | null;
  // 每房净房费（手填才有值；Pydantic Decimal → string）
  ota_owner_revenue?: string | null;
  // 每日房价快照 { "YYYY-MM-DD": "253.38" }。Pydantic 用 string 输出 Decimal。
  daily_prices?: Record<string, string>;
  created_at: string;
  updated_at: string;
}

export interface OrderCreate {
  channel: Channel;
  platform_order_id?: string;
  guest_name: string;
  guest_phone?: string;  // issue#3: 选填
  booking_type?: BookingType;  // issue#6: 默认 "normal"
  // Multi-room: 新代码用 rooms 数组。空 → 后端按下方旧字段合成 rooms[0]
  rooms?: OrderRoomCreate[];
  // DEPRECATED: 老 payload 兼容字段
  room_id?: string | null;
  check_in_date?: string;
  check_out_date?: string;
  list_price?: number | null;
  discount_amount?: number;
  actual_price?: number | null;
  deposit?: number;
  platform_commission_rate?: number;
  // 平台单「到手价(净房费=平台结给我们)」。填此值 → 后端存 metadata.ota_owner_revenue
  // 并自动倒推佣金率,取代手填佣金率。非平台/私单留空。
  ota_owner_revenue?: number | null;
  notes?: string;
  // 同客同日期重复单拦截（409 duplicate_order）的覆盖开关：
  // 用户确认「同名不同客」后带 true 重发。
  allow_duplicate?: boolean;
}

export interface OrderOut {
  order_id: string;
  channel: Channel;
  platform_order_id?: string | null;
  guest_name: string;
  guest_phone?: string | null;  // issue#3
  booking_type: BookingType;  // issue#6
  // DEPRECATED 单房语义字段（多房订单为首房快照 / min-max 跨度）
  room_id?: string | null;
  check_in_date: string;
  check_out_date: string;
  nights: number;
  list_price?: number | null;
  discount_amount: number;
  actual_price?: number | null;
  deposit: number;
  deposit_status: DepositStatus;
  deposit_returned?: number | null;
  payment_status: PaymentStatus;
  order_status: OrderStatus;
  cleaning_status: CleaningStatus;
  platform_commission_rate: number;
  platform_commission: number;
  net_revenue: number;
  expected_revenue?: number | null; // 携程预计收入(业主到手),仅OTA搬单单非空
  price_pending?: boolean; // OTA占位价待回填,显示"价格同步中"而非¥0
  is_ota_free_room?: boolean;
  stay_settlement_kind?: StaySettlementKind | null;
  manual_override_fields?: ManualOverrideField[];
  is_manually_managed?: boolean;
  company_sponsorship?: CompanySponsoredStay | null;
  stay_group_id?: string | null; // 续住关联组号；同组多张单为一段连续入住
  notes?: string | null;
  created_by?: string | null;
  created_at: string;
  updated_at: string;
  // Multi-room: 嵌套房间数组（按 position 升序）
  rooms: OrderRoomOut[];
}

export interface OrderListItem {
  order_id: string;
  channel: Channel;
  guest_name: string;
  guest_phone?: string | null;  // issue#3
  booking_type: BookingType;  // issue#6
  room_id?: string | null;  // DEPRECATED — 多房订单用 room_ids
  check_in_date: string;
  check_out_date: string;
  nights: number;
  actual_price?: number | null;
  expected_revenue?: number | null; // 携程预计收入(业主到手),仅OTA搬单单非空
  price_pending?: boolean; // OTA占位价待回填,显示"价格同步中"而非¥0
  is_ota_free_room?: boolean;
  stay_settlement_kind?: StaySettlementKind | null;
  order_status: OrderStatus;
  payment_status: PaymentStatus;
  cleaning_status: CleaningStatus;
  created_at: string;
  // Multi-room: 该订单所有房间号（不含待排房 NULL）
  room_ids: string[];
}

// ─── Stay group（续住段）─────────────────────────────────────────────────────

// 续住组整段视图：点组内任意一段，后端都返回同一份（GET /orders/{id}/stay-group）。
// 每段保留自己的渠道/佣金/平台单号——分账与 OTA 对账各段独立，前端只做展示合并。
//
// 后端 segments 实际吐的是完整 OrderOut（schemas/order.py:618），这里只声明展示用得到的键。
// 金额是字符串：后端是 Decimal，Pydantic v2 在 JSON 里序列化成 "693.28" 而非数字。
export interface StaySegment {
  order_id: string;
  channel: string;
  platform_order_id?: string | null;
  guest_name?: string;
  guest_phone?: string | null;
  check_in_date: string;
  check_out_date: string;
  nights: number;
  actual_price?: string | null;
  expected_revenue?: string | null; // OTA 到手价，按单口径；组级没有这个数，别在前端累加
  price_pending?: boolean;
  is_ota_free_room?: boolean;
  stay_settlement_kind?: StaySettlementKind | null;
  manual_override_fields?: ManualOverrideField[];
  is_manually_managed?: boolean;
  company_sponsorship?: CompanySponsoredStay | null;
  order_status: string;
  rooms?: Array<{ order_room_id: string; room_id?: string | null }>;
  // 段明细展示用（后端将补的字段）。**防御式消费**：旧 API 缺失时不渲染对应列。
  room_ids?: string[]; // 该段的房号（跨房续住组各段不同）
  deposit?: string | null; // 该段登记的押金（组押金实收在哪段就显示在哪段）
}

// ⚠️ 与后端 LinkCandidateOut 逐键对齐（checkout_continuation.py 只给这四个键）。
// **没有 check_out_date** —— TS 不校验运行时，凭空加一个会编译照过、页面渲染出 undefined。
export interface LinkCandidate {
  room_id: string;
  next_order_id: string;
  guest_name: string;
  check_in_date: string;
}

export interface StayGroup {
  stay_group_id?: string | null; // null = 非续住单的退化单段组
  group_kind?: "managed_split" | null;
  anchor_order_id: string; // 首段：持门锁码
  last_order_id: string; // 末段：退房只在这段办
  check_in_date: string;
  check_out_date: string;
  nights: number;
  total_amount: string;
  group_status: OrderStatus;
  channels: string[];
  rooms: string[]; // 按夜去重的房间序列；跨房续住会多于一个
  free_room_kind?: "none" | "all" | "mixed";
  deposit: string; // 组内实收那份（非锚单口径，见后端 group_view）
  deposit_status: DepositStatus;
  deposit_order_id?: string | null;
  // 组押金实退（后端将随 StayGroupOut 补的字段）。**防御式消费**：旧 API 没有它时
  // 前端隐藏「实退/扣款」行——绝不能退回去读被点段自己的 order.deposit_returned 凑数。
  deposit_returned?: string | null;
  segments: StaySegment[];
  link_candidates: LinkCandidate[]; // 仅无组且认出续住时非空
}

export interface CompanySponsorshipAdjustment {
  adjustment_id: string;
  correction_of_id: string;
  delta: string;
  reason: string;
  created_at: string;
}

export interface CompanySponsoredStay {
  sponsored_stay_id: string;
  source_order_id: string;
  segment_order_id: string;
  source_price_snapshot_id: string;
  calculation_base: string;
  settlement_ratio: string;
  amount: string;
  effective_amount: string;
  version: number;
  payment_responsibility: "company_payable" | "channel_settled";
  status: "confirmed" | "settled" | "voided";
  adjustments: CompanySponsorshipAdjustment[];
}

export interface SourcePriceSnapshot {
  source_price_snapshot_id: string;
  version: number;
  channel: Channel;
  check_in_date: string;
  check_out_date: string;
  nightly_bases: Record<string, string>;
  total: string;
  origin:
    | "bypms_import"
    | "bypms_adoption"
    | "administrator_fallback"
    | "administrator_override";
}

export interface ManualControlLockedField {
  field: ManualOverrideField;
  current_value: unknown;
}

export interface OrderSyncConflict {
  conflict_id: string;
  field: ManualOverrideField;
  local_value: unknown;
  upstream_value: unknown;
  upstream_version: string;
  first_seen_at: string;
  last_seen_at: string;
  can_restore_following: boolean;
}

export interface OrderManualControl {
  source_order_id: string;
  is_relevant: boolean;
  split: {
    enabled: boolean;
    visible: boolean;
    eligible: boolean;
    blocker_code?: string | null;
    blocker_message?: string | null;
    group_version: number;
  };
  source_price_snapshot?: SourcePriceSnapshot | null;
  locked_fields: ManualControlLockedField[];
  open_conflicts: OrderSyncConflict[];
  can_administer: boolean;
  can_write: boolean;
}

export interface SplitStaySegmentDraft {
  check_in_date: string;
  check_out_date: string;
  room_id: string;
  settlement_kind: StaySettlementKind;
}

export interface ZeroFeeSplitPayload {
  expected_group_version: number;
  price_snapshot_id: string;
  segments: SplitStaySegmentDraft[];
}

export interface ZeroFeeSplitCompanySponsored {
  calculation_base: string;
  settlement_ratio: string;
  amount: string;
}

export interface ZeroFeeSplitSegment extends SplitStaySegmentDraft {
  order_id?: string | null;
  company_sponsored?: ZeroFeeSplitCompanySponsored | null;
}

export interface ZeroFeeSplitResult {
  stay_group_id?: string | null;
  group_version: number;
  segments: ZeroFeeSplitSegment[];
}

export interface SourcePriceSnapshotOverridePayload {
  based_on_snapshot_id?: string | null;
  nightly_prices?: Record<string, string>;
  total?: string;
  reason: string;
}

// 订单列表按段分页的响应（GET /orders/by-segment）。一行一段，每行就是上面的 StayGroup。
// **注意 items 不是订单**——旧端点 GET /orders 的 PaginatedOrderList 才是按单的，两者不通用。
// 合计/晚数/房间序列后端已算好，前端不做归并。
export interface PaginatedSegmentList {
  items: StayGroup[];
  total: number; // 段数：续住组算 1，无组单也算 1
  page: number;
  page_size: number;
}

// ─── Room ────────────────────────────────────────────────────────────────────

export interface RoomOut {
  room_id: string;
  room_name: string;
  room_type?: string | null;  // 房间分组（自由文本）
  floor?: number | null;
  beds?: number;  // 床位数（洗涤费=单价×床位数）
  owner_id?: string | null;
  owner_share_ratio: number;
  // issue#6: 试住/自住单分成比例（默认 1.0 = 业主全担）
  share_ratio_trial: number;
  share_ratio_owner_self: number;
  room_status: RoomStatus;
  effective_status?: RoomStatus | null;  // 服务端按今日计算的有效房态(统一显示用)
  // 上一个状态（进入维修/锁房前），用于"一键恢复"。服务端维护。
  previous_status?: RoomStatus | null;
  base_price?: number | null;
  min_stay_nights: number;
  weekend_markup: number;
  holiday_markup: number;
  channel_availability: Record<string, boolean>;
  province?: string | null;
  city?: string | null;
  district?: string | null;
  community_name?: string | null;
  building_no?: string | null;
  unit_no?: string | null;
  contract_signed_date?: string | null;  // YYYY-MM-DD
  sale_date?: string | null;             // YYYY-MM-DD
  remarks?: string | null;
}

export interface RoomCreate {
  room_id: string;
  room_name: string;
  room_type?: string | null;
  floor?: number | null;
  beds?: number;  // 床位数（洗涤费=单价×床位数）
  owner_id?: string | null;
  owner_share_ratio?: number;
  share_ratio_trial?: number;
  share_ratio_owner_self?: number;
  base_price?: number | null;
  province?: string | null;
  city?: string | null;
  district?: string | null;
  community_name?: string | null;
  building_no?: string | null;
  unit_no?: string | null;
  contract_signed_date?: string | null;
  sale_date?: string | null;
  remarks?: string | null;
}

export interface RoomUpdate {
  room_name?: string;
  room_type?: string | null;
  floor?: number | null;
  beds?: number;  // 床位数（洗涤费=单价×床位数）
  room_status?: RoomStatus;
  base_price?: number | null;
  owner_share_ratio?: number;
  share_ratio_trial?: number;
  share_ratio_owner_self?: number;
  province?: string | null;
  city?: string | null;
  district?: string | null;
  community_name?: string | null;
  building_no?: string | null;
  unit_no?: string | null;
  min_stay_nights?: number;
  weekend_markup?: number;
  holiday_markup?: number;
  owner_id?: string | null;
  contract_signed_date?: string | null;
  sale_date?: string | null;
  remarks?: string | null;
}

// ─── Finance ─────────────────────────────────────────────────────────────────

export interface PaymentCreate {
  order_id: string;
  amount: number;
  method: PaymentMethod;
  paid_at: string;
  is_deposit?: boolean;
  notes?: string;
}

export interface PaymentOut {
  payment_id: string;
  order_id: string;
  amount: number;
  method: PaymentMethod;
  paid_at: string;
  is_deposit: boolean;
  notes?: string | null;
  created_at: string;
}

export interface RefundCreate {
  order_id: string;
  amount: number;
  reason: RefundReason;
  refunded_at: string;
  notes?: string;
}

export interface RefundOut {
  refund_id: string;
  order_id: string;
  amount: number;
  reason: RefundReason;
  refunded_at: string;
  notes?: string | null;
  created_at: string;
}

export interface ExpenseCreate {
  category: ExpenseCategory;
  amount: number;
  description: string;
  expense_date: string;
  room_id?: string | null;
  order_id?: string | null;
  payer?: ExpensePayer;
  owner_id?: string | null;
  // 整层录入：传楼层号，后端解析到该层唯一业主，落库 owner_id、room_id 留空。
  floor?: number | null;
  notes?: string;
}

export interface ExpenseOut {
  expense_id: string;
  category: ExpenseCategory;
  amount: number;
  description: string;
  expense_date: string;
  room_id?: string | null;
  order_id?: string | null;
  payer: ExpensePayer;
  owner_id?: string | null;
  notes?: string | null;
  created_at: string;
}

export interface ByRoomSummaryItem {
  room_id: string;
  room_name: string;
  owner_id?: string | null;
  owner_share_ratio: number;
  order_count: number;
  revenue: number;
  commission: number;
  net_revenue: number;
  expense_company: number;
  expense_owner: number;
  owner_net_share: number;
  company_net: number;
}

// 单房明细「区间订单（收入来源）」的段级行（按 OrderRoom，含多房单里该房那一段）
export interface RoomRevenueSegment {
  order_id: string;
  order_room_id: string;
  room_id: string;
  guest_name: string;
  channel: string;
  check_in_date: string;
  check_out_date: string;
  nights: number;
  actual_price: number | null;
}

// ─── Owner ────────────────────────────────────────────────────────────────────

export interface OwnerRoomItem {
  room_id: string;
  room_name: string;
  owner_share_ratio: number;
  owner_deduction_rules: string[];
  owner_ignored_categories?: string[];
}

export interface OwnerOut {
  owner_id: string;
  name: string;
  username?: string | null;
  phone?: string | null;
  id_card?: string | null;
  bank_account?: string | null;
  bank_name?: string | null;
  notes?: string | null;
  created_at: string;
  rooms: OwnerRoomItem[];
  room_count: number;
  parent_owner_id?: string | null;
  is_master?: boolean;
  sub_owners?: { owner_id: string; name: string }[];
}

export interface OwnerCreate {
  owner_id?: string;
  name: string;
  username?: string | null;
  phone?: string | null;
  id_card?: string | null;
  bank_account?: string | null;
  bank_name?: string | null;
  notes?: string | null;
  parent_owner_id?: string | null;
}

export interface SettlementItem {
  item_id: string;
  // 业主级支出行（整层/某业主电费）room_id 为空，用 label 展示；逐房行仍有 room_id。
  room_id?: string | null;
  room_name?: string | null;
  label?: string | null;
  order_count: number;
  revenue: number;
  commission: number;
  net_revenue: number;
  owner_expenses: number;
  share_ratio_snapshot: number;
  owner_net_amount: number;
  // 账面「到厘」展示值（打款仍用 owner_net_amount 到分值）；旧结算可能缺省
  owner_net_amount_precise?: number | null;
  // v2#4: 每笔费用按 RoomCostShareRule 加权后的明细
  cost_share_breakdown?: Array<{
    expense_id: string;
    category: string;
    booking_type: string;
    amount: string;
    share_percent: string;
    owner_amount: string;
    rule_id?: string | null;
  }>;
}

export interface SettlementDetail extends OwnerSettlementOut {
  items: SettlementItem[];
}

// ─── Task ────────────────────────────────────────────────────────────────────

export interface TaskOut {
  task_id: string;
  task_type: TaskType;
  title: string;
  description?: string | null;
  order_id?: string | null;
  room_id?: string | null;
  assignee_id?: string | null;
  status: TaskStatus;
  priority: TaskPriority;
  deadline?: string | null;
  completed_at?: string | null;
  notes?: string | null;
  submitted_at?: string | null;
  review_status?: string | null;
  reviewer_id?: string | null;
  reviewed_at?: string | null;
  rejection_reason?: string | null;
  created_at: string;
}

export interface TaskCreate {
  task_type?: TaskType;
  title: string;
  description?: string;
  order_id?: string;
  room_id?: string;
  assignee_id?: string;
  priority?: TaskPriority;
  deadline?: string;
  notes?: string;
}

export interface TaskUpdate {
  title?: string;
  description?: string;
  assignee_id?: string;
  status?: TaskStatus;
  priority?: TaskPriority;
  deadline?: string;
  notes?: string;
}

// ─── Dashboard ───────────────────────────────────────────────────────────────

export interface PaginatedOrderList {
  items: OrderListItem[];
  total: number;
  page: number;
  page_size: number;
}

export interface DashboardOverview {
  today: DashboardToday;
  monthly: DashboardMonthly | null;
  trend: RevenueTrendItem[] | null;
  channel: ChannelAnalysisItem[] | null;
  comparison: ComparisonReport | null;
}

export interface DashboardToday {
  checkin_today: number;
  checkout_today: number;
  cleaning_needed: number;
  overdue_tasks: number;
  checked_in: number;
  total_rooms: number;
  occupancy_rate: number;
  // 具体房号（room_name），前台卡片直显；待排房不计入故可能少于计数
  checkin_rooms: string[];
  checkout_rooms: string[];
  cleaning_rooms: string[];
  /**
   * 待保洁明细行：`cleaning_needed` 就是它的长度，抽屉直接渲染它。
   * 数字与名单同一份数据，结构上不可能对不上（旧实现数字读 orders.cleaning_status、
   * 抽屉读 tasks 表，是两个真相源）。
   */
  cleaning_list: DashboardCleaningRow[];
}

export interface DashboardCleaningRow {
  room_id: string;
  room_name: string;
  order_id: string;
  guest_name: string;
  check_in_date: string;
  check_out_date: string;
  /** 试运营房型（极海）角标，与房态页/飞书打扫卡同源；无标时为 null */
  trial_tag: string | null;
}

export interface DashboardMonthly {
  // 整月汇总带 year/month；任意区间汇总(/dashboard/period-summary)带 start_date/end_date。
  year?: number;
  month?: number;
  start_date?: string;
  end_date?: string;
  order_count: number;
  total_nights: number;
  total_actual_price: number;
  total_commission: number;
  total_net_revenue: number;
  total_expenses: number;
  gross_profit: number;
  occ: number;
  adr: number;
  revpar: number;
  channel_breakdown: Record<string, number>;
}

export interface RevenueTrendItem {
  month: string;
  revenue: number;
}

// ─── Calendar ────────────────────────────────────────────────────────────────

export interface CalendarDay {
  order_id: string | null;        // 房间屏蔽 (RoomBlock) 时为 null
  order_room_id?: string;         // Multi-room: 该格属于哪一行 OrderRoom（拖拽换房用）
  stay_group_id?: string | null;  // 续住关联组号：同组相邻同房格渲染成一条连续横条
  guest_name: string;
  status: OrderStatus | string;   // 屏蔽时形如 "block:maintenance"
  channel?: Channel;              // Phase 3 新增；屏蔽行无值
  payment_status?: PaymentStatus; // Phase 3 新增；屏蔽行无值
  price?: number | null;          // D2：该晚房价（daily_prices 优先，否则均摊）；屏蔽行无值
  trial_tag?: string | null;      // 试运营房型角标（备注命中关键词，当前=「极海」）；无则 null
  free_room_kind?: "none" | "all" | "mixed";
  stay_settlement_kind?: StaySettlementKind | null;
  is_manually_managed?: boolean;
  // 时间段锁房 (RoomBlock) 专用：让甘特图能直接点灰条「解除锁房」
  block_id?: string;
  block_reason?: string | null;
  block_start?: string;           // YYYY-MM-DD
  block_end?: string;             // YYYY-MM-DD（半开区间，不含当天）
}

export interface CalendarRoom {
  room_id: string;
  room_name: string;
  room_status: RoomStatus;
  days: Record<string, CalendarDay>;
}

// ─── Auth ────────────────────────────────────────────────────────────────────

export interface LoginResponse {
  access_token: string;
  refresh_token: string;
  token_type: string;
  user_id: string;
  role: string;
  display_name: string;
}

export interface UserCreate {
  username: string;
  display_name: string;
  phone?: string;
  role: UserRole;
  password: string;
}

// ─── Guest ───────────────────────────────────────────────────────────────

export interface GuestOut {
  guest_id: string;
  name: string;
  phone: string;
  id_number?: string | null;
  wechat?: string | null;
  notes?: string | null;
  visit_count: number;
  total_spent: number;
  total_nights: number;
  last_check_in?: string | null;
  preferred_room?: string | null;
  tags?: string | null;
  created_at: string;
  updated_at: string;
}

export interface GuestUpdate {
  name?: string;
  id_number?: string;
  wechat?: string;
  notes?: string;
  tags?: string;
}

export interface GuestOrderHistory {
  order_id: string;
  room_id?: string | null;
  check_in_date: string;
  check_out_date: string;
  nights: number;
  actual_price: number;
  order_status: OrderStatus;
  channel: Channel;
  created_at?: string | null;
}

export interface GuestSummary {
  total_guests: number;
  repeat_guests: number;
  repeat_rate: number;
}

// ─── Room Block ──────────────────────────────────────────────────────────

export type BlockType = "owner_use" | "maintenance" | "reserved" | "other";

export interface RoomBlockOut {
  block_id: string;
  room_id: string;
  block_type: BlockType;
  start_date: string;
  end_date: string;
  reason?: string | null;
  created_by?: string | null;
  created_at: string;
}

export interface RoomBlockCreate {
  room_id: string;
  block_type: BlockType;
  start_date: string;
  end_date: string;
  reason?: string;
}

// ─── Owner Settlement ────────────────────────────────────────────────────

export type SettlementStatus = "pending" | "confirmed" | "paid" | "disputed";

export interface OwnerSettlementOut {
  settlement_id: string;
  owner_id: string;
  billing_month: string;
  total_net_revenue: number;
  owner_amount: number;          // 分成前业主应得（旧版用）；新版等于 actual_owner_amount
  deducted_expenses: number;
  actual_owner_amount: number;   // 最终到账金额
  // 账面「到厘」展示值（打款仍用上面到分的 owner_amount/actual_owner_amount）；旧结算可能缺省
  owner_amount_precise?: number | null;
  actual_owner_amount_precise?: number | null;
  status: SettlementStatus;
  notes?: string | null;
  created_at: string;
  // legacy, may be undefined on new records
  total_revenue?: number;
  total_commission?: number;
  owner_share_ratio?: number;
}

// ─── Pricing ─────────────────────────────────────────────────────────────────

export interface RoomPricingPoint {
  date: string;
  recommended_price?: number | null;
  base_price?: number | null;
  competitor_avg_price?: number | null;
  source?: string | null;
  source_url?: string | null;
}

// ─── Task Review (Feature 4) ────────────────────────────────────────────

export type ReviewStatus = "pending_review" | "approved" | "rejected";

// ─── Notification (Features 3 & 10) ─────────────────────────────────────

export type NotificationType = "checkin_guide" | "system" | "task" | "order" | "settlement" | "alert";

export interface NotificationOut {
  notification_id: string;
  user_id?: string | null;
  title: string;
  content?: string | null;
  type: string;
  is_read: boolean;
  created_at: string;
}

export interface NotificationTemplateOut {
  name: string;
  description: string;
  channels: string[];
  variables: string[];
}

export interface CheckinGuideRequest {
  order_id: string;
  door_code?: string;
  wifi_name?: string;
  wifi_password?: string;
  address?: string;
  extra_notes?: string;
}

export interface CheckinGuideResponse {
  success: boolean;
  log_id: string;
  message: string;
}

// ─── Channel Analysis (Feature 6) ───────────────────────────────────────

export interface ChannelAnalysisItem {
  channel: string;
  order_count: number;
  total_revenue: number;
  total_commission: number;
  net_revenue: number;
  avg_nights: number;
  avg_price: number;
}

// ─── Comparison Report (Feature 7) ──────────────────────────────────────

export interface ComparisonMetrics {
  year: number;
  month: number;
  revenue: number;
  order_count: number;
  occupancy: number;
  adr: number;
}

export interface ComparisonChange {
  revenue: number | null;
  order_count: number | null;
  occupancy: number | null;
  adr: number | null;
}

export interface ComparisonReport {
  current: ComparisonMetrics;
  last_month: ComparisonMetrics;
  same_month_last_year: ComparisonMetrics;
  mom_change: ComparisonChange;
  yoy_change: ComparisonChange;
}

// ─── Deposit (Feature 9) ────────────────────────────────────────────────

export interface DepositActionResponse {
  message: string;
  deposit_status: DepositStatus;
  deposit_returned?: string;
}

// ─── Search (Feature C) ─────────────────────────────────────────────────────

export interface SearchOrderItem {
  order_id: string;
  guest_name: string;
  guest_phone: string;
  room_id: string | null;
  check_in_date: string;
  check_out_date: string;
  order_status: string;
}

export interface SearchGuestItem {
  guest_id: string;
  name: string;
  phone: string;
  visit_count: number;
}

export interface SearchRoomItem {
  room_id: string;
  room_name: string;
  room_status: string;
}

export interface SearchResult {
  orders: SearchOrderItem[];
  guests: SearchGuestItem[];
  rooms: SearchRoomItem[];
}

// ─── 业主托管留资（tuoguan 落地页，公开接口） ────────────────────────────

export interface HostingLeadCreate {
  name: string;
  phone: string;
  property_location: string;
}

export interface HostingLeadOut {
  lead_id: string;
  status: "ok" | "already_registered";
}
