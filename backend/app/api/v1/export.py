"""Feature A: Data export to Excel — orders, finance summary, settlements."""

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select, extract
from sqlalchemy.orm import selectinload
from datetime import date

from app.core.datetime_helpers import today_cn, to_cn
from decimal import Decimal
from typing import Optional
from io import BytesIO

import openpyxl
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side

from app.core.deps import DBSession, CurrentUser
from app.models.order import Order, OrderStatus, Channel
from app.models.settlement import OwnerSettlement
from app.models.expense import Expense
from app.models.owner import Owner
from app.models.room import Room

router = APIRouter(prefix="/export", tags=["export"])

# ── Helpers ──────────────────────────────────────────────────────────────────

HEADER_FONT = Font(bold=True, size=11, color="FFFFFF")
HEADER_FILL = PatternFill(start_color="1677FF", end_color="1677FF", fill_type="solid")
HEADER_ALIGN = Alignment(horizontal="center", vertical="center")
THIN_BORDER = Border(
    left=Side(style="thin", color="D9D9D9"),
    right=Side(style="thin", color="D9D9D9"),
    top=Side(style="thin", color="D9D9D9"),
    bottom=Side(style="thin", color="D9D9D9"),
)

CHANNEL_LABELS = {
    # 现役渠道（2026-05-23 拍板 + trial_stay）
    "ctrip": "携程", "meituan_hotel": "美团酒店", "meituan_homestay": "美团民宿",
    "douyin": "抖音", "qunar": "去哪儿", "zhixing": "智行", "tongcheng": "同程",
    "self_acquired": "自来客", "offline": "线下渠道", "self_used": "自住",
    "trial_stay": "试住",
    # 历史保留值（前端隐藏，仅供老订单展示）
    "meituan": "美团（旧）", "tujia": "途家", "private": "私域",
    "walk_in": "散客", "direct": "自助",
}

STATUS_LABELS = {
    # 2026-06-05 流程调整：确认收款挪到退房后。
    # pending_checkout=已退房待收款，pending_payment 重定义为后期「待完成」。
    "pending_confirm": "待确认", "pending_payment": "待完成",
    "paid_pending_room": "已付待排房", "roomed_pending_checkin": "待入住",
    "checked_in": "入住中", "pending_checkout": "已退房待收款",
    "completed": "已完成", "cancelled": "已取消",
    "rescheduled": "已改期", "abnormal": "异常",
}


def _style_header(ws, col_count: int):
    for col in range(1, col_count + 1):
        cell = ws.cell(row=1, column=col)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = HEADER_ALIGN
        cell.border = THIN_BORDER


def _to_streaming(wb, filename: str) -> StreamingResponse:
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    # HTTP 头只能 latin-1；中文文件名走 RFC 6266 filename*，并给 ASCII 兜底名。
    from urllib.parse import quote

    ascii_fallback = filename.encode("ascii", "ignore").decode("ascii") or "export.xlsx"
    disposition = (
        f"attachment; filename=\"{ascii_fallback}\"; "
        f"filename*=UTF-8''{quote(filename)}"
    )
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": disposition},
    )


# ── Export Orders ────────────────────────────────────────────────────────────

@router.get("/orders")
async def export_orders(
    db: DBSession,
    current_user: CurrentUser,
    status: Optional[str] = Query(default=None),
    check_in_from: Optional[date] = Query(default=None),
    check_in_to: Optional[date] = Query(default=None),
):
    """Export filtered orders as Excel."""
    if current_user["role"] not in ("admin", "operator", "finance"):
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="无权导出")

    q = select(Order).options(selectinload(Order.rooms)).where(Order.is_deleted == False)
    if status:
        q = q.where(Order.order_status == status)
    if check_in_from:
        q = q.where(Order.check_in_date >= check_in_from)
    if check_in_to:
        q = q.where(Order.check_in_date <= check_in_to)
    q = q.order_by(Order.created_at.desc())

    result = await db.execute(q)
    orders = result.scalars().all()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "订单列表"

    headers = ["订单号", "渠道", "客人姓名", "手机号", "房间", "入住日期", "退房日期",
               "晚数", "实收金额", "佣金", "净收入", "预计收入(业主到手)", "状态", "创建时间"]
    ws.append(headers)
    _style_header(ws, len(headers))

    for o in orders:
        ws.append([
            o.order_id,
            CHANNEL_LABELS.get(o.channel.value, o.channel.value),
            o.guest_name,
            o.guest_phone,
            # 多房订单读 order_rooms（room_id 顶层字段已废弃，多房单恒 NULL → 曾显示"待排房"）
            "、".join(o.room_ids) or "待排房",
            str(o.check_in_date),
            str(o.check_out_date),
            o.nights,
            float(o.actual_price or 0),
            float(o.platform_commission),
            float(o.net_revenue),
            float(o.expected_revenue) if o.expected_revenue is not None else "",
            STATUS_LABELS.get(o.order_status.value, o.order_status.value),
            to_cn(o.created_at).strftime("%Y-%m-%d %H:%M") if o.created_at else "",
        ])

    # Auto-width
    for col in ws.columns:
        max_len = max((len(str(cell.value or "")) for cell in col), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 30)

    today_str = today_cn().strftime("%Y%m%d")
    return _to_streaming(wb, f"orders_{today_str}.xlsx")


# ── Export Finance Report ────────────────────────────────────────────────────

@router.get("/finance")
async def export_finance(
    db: DBSession,
    current_user: CurrentUser,
    year: int = Query(default_factory=lambda: today_cn().year),
    month: int = Query(default_factory=lambda: today_cn().month),
    start_date: Optional[date] = Query(default=None, description="起始日期 YYYY-MM-DD"),
    end_date: Optional[date] = Query(default=None, description="结束日期 YYYY-MM-DD"),
):
    """导出财务报表 Excel。日期范围二选一（start_date+end_date 优先，向后兼容 year+month）。
    收入按订单离店日、支出按发生日落区间，与月度汇总/结算口径一致。"""
    if current_user["role"] not in ("admin", "finance"):
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="无权导出财务数据")

    use_range = bool(start_date and end_date)
    if use_range and end_date < start_date:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="end_date 必须 >= start_date")

    # 订单：区间按离店日 between，否则按 year/month（离店月归属，2026-07-14 拍板）
    order_date_filter = (
        [Order.check_out_date >= start_date, Order.check_out_date <= end_date]
        if use_range
        else [extract("year", Order.check_out_date) == year,
              extract("month", Order.check_out_date) == month]
    )
    orders_result = await db.execute(
        select(Order).options(selectinload(Order.rooms)).where(
            Order.is_deleted == False,
            Order.order_status.not_in([OrderStatus.cancelled]),
            *order_date_filter,
        ).order_by(Order.check_out_date)
    )
    orders = orders_result.scalars().all()

    # 支出：区间按发生日 between，否则按 year/month
    expense_date_filter = (
        [Expense.expense_date >= start_date, Expense.expense_date <= end_date]
        if use_range
        else [extract("year", Expense.expense_date) == year,
              extract("month", Expense.expense_date) == month]
    )
    expenses_result = await db.execute(
        select(Expense).where(
            *expense_date_filter,
            Expense.is_deleted == False,
        ).order_by(Expense.expense_date)
    )
    expenses = expenses_result.scalars().all()

    wb = openpyxl.Workbook()

    # Sheet 1: Revenue summary
    ws1 = wb.active
    ws1.title = "收入明细"
    h1 = ["订单号", "渠道", "客人", "房间", "入住", "退房", "晚数", "实收", "佣金", "净收入", "预计收入(业主到手)"]
    ws1.append(h1)
    _style_header(ws1, len(h1))

    total_actual = Decimal("0")
    total_commission = Decimal("0")
    total_net = Decimal("0")
    for o in orders:
        ws1.append([
            o.order_id,
            CHANNEL_LABELS.get(o.channel.value, o.channel.value),
            o.guest_name,
            "、".join(o.room_ids) or "",
            str(o.check_in_date),
            str(o.check_out_date),
            o.nights,
            float(o.actual_price or 0),
            float(o.platform_commission),
            float(o.net_revenue),
            float(o.expected_revenue) if o.expected_revenue is not None else "",
        ])
        total_actual += o.actual_price or Decimal("0")
        total_commission += o.platform_commission
        total_net += o.net_revenue

    # Summary row
    ws1.append([])
    ws1.append(["合计", "", "", "", "", "", "", float(total_actual), float(total_commission), float(total_net), ""])

    for col in ws1.columns:
        max_len = max((len(str(cell.value or "")) for cell in col), default=10)
        ws1.column_dimensions[col[0].column_letter].width = min(max_len + 4, 30)

    # Sheet 2: Expenses
    ws2 = wb.create_sheet("支出明细")
    h2 = ["日期", "类别", "描述", "房间", "金额"]
    ws2.append(h2)
    _style_header(ws2, len(h2))

    total_expense = Decimal("0")
    for e in expenses:
        ws2.append([
            str(e.expense_date) if e.expense_date else "",
            e.category.value if hasattr(e.category, "value") else str(e.category),
            e.description,
            e.room_id or "",
            float(e.amount),
        ])
        total_expense += e.amount

    ws2.append([])
    ws2.append(["合计", "", "", "", float(total_expense)])

    for col in ws2.columns:
        max_len = max((len(str(cell.value or "")) for cell in col), default=10)
        ws2.column_dimensions[col[0].column_letter].width = min(max_len + 4, 30)

    fname = (
        f"finance_{start_date.strftime('%Y%m%d')}-{end_date.strftime('%Y%m%d')}.xlsx"
        if use_range
        else f"finance_{year}{str(month).zfill(2)}.xlsx"
    )
    return _to_streaming(wb, fname)


# ── Export Settlements ───────────────────────────────────────────────────────

@router.get("/settlements")
async def export_settlements(
    db: DBSession,
    current_user: CurrentUser,
    billing_month: Optional[str] = Query(default=None),
):
    """Export owner settlements as Excel."""
    if current_user["role"] not in ("admin", "finance"):
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="无权导出结算数据")

    q = select(OwnerSettlement).options(selectinload(OwnerSettlement.items))
    if billing_month:
        q = q.where(OwnerSettlement.billing_month == billing_month)
    q = q.order_by(OwnerSettlement.billing_month.desc())

    result = await db.execute(q)
    settlements = result.scalars().all()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "房东结算"

    # 房东分成/实际结算导出到厘（与页面一致）；净收入/扣除支出到分。
    from app.services.owner_settlement import settlement_precise_amounts
    headers = ["结算ID", "房东ID", "结算月", "净收入", "房东分成", "扣除支出", "实际结算", "状态", "备注"]
    ws.append(headers)
    _style_header(ws, len(headers))

    status_labels = {"pending": "待确认", "confirmed": "已确认", "paid": "已打款", "disputed": "有争议"}

    for s in settlements:
        owner_amount_p, actual_p = settlement_precise_amounts(s.items)
        # 无明细的旧版结算 precise 为 None → 回退到分（存库值），不显示 0
        owner_amount_out = float(owner_amount_p if owner_amount_p is not None else s.owner_amount)
        actual_out = float(actual_p if actual_p is not None else s.actual_owner_amount)
        ws.append([
            s.settlement_id,
            s.owner_id,
            s.billing_month,
            float(s.total_net_revenue),
            owner_amount_out,
            float(s.deducted_expenses),
            actual_out,
            status_labels.get(s.status.value if hasattr(s.status, "value") else s.status, str(s.status)),
            s.notes or "",
        ])
        # 显式数字格式：分成/实际结算(E/G 列)到厘，净收入/扣除支出(D/F 列)到分。
        row = ws.max_row
        ws.cell(row=row, column=4).number_format = "0.00"
        ws.cell(row=row, column=5).number_format = "0.000"
        ws.cell(row=row, column=6).number_format = "0.00"
        ws.cell(row=row, column=7).number_format = "0.000"

    # 表尾说明：与页面小字一致，避免业主拿导出对不上到账金额。
    ws.append([])
    ws.append(["说明：房东分成 / 实际结算精确至厘（元），实际打款按分四舍五入结算。"])

    for col in ws.columns:
        max_len = max((len(str(cell.value or "")) for cell in col), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 30)

    month_str = billing_month or today_cn().strftime("%Y-%m")
    return _to_streaming(wb, f"settlements_{month_str}.xlsx")


# ── Export Single Owner Settlement Statement (每行「导出明细」) ────────────────
#
# 单个业主结算单 → 『业主分成明细 + 垫付费用明细』两段式 Excel，可直接发给业主。
# 规格（王总 2026-07-19，精简版）：模式写「业主X%」；垫付明细只填金额、数量/备注留空
# 手填；无高亮、无活公式，数值直接算好。关键不变量：主表每行
#   业主分成 + Σ各支出列(负) + 运营补贴 == 净利润，且 Σ各支出列 == 业主总支出。

# 主表支出列顺序（有数据源的固定列 + 「其他」兜底，保证 Σ列 == 业主总支出，不漏账）
_STATEMENT_EXPENSE_COLUMNS = [
    "日耗", "保洁", "续住保洁", "洗涤", "物业费", "水费", "公摊水费",
    "电费", "新布草过水费", "维修费", "燃气费", "采购费", "其他",
]
# 覆盖 v2#7 全部在用类目（对齐王总《业主分成明细》），未命中才落「其他」。
# 改支出类目枚举时必须同步这里，否则新类目会全堆进「其他」。见 expense.py ExpenseCategory。
_CATEGORY_TO_COLUMN = {
    "daily_supplies": "日耗",
    "laundry": "洗涤",
    "property_fee": "物业费",
    "water": "水费",
    "cold_water": "水费", "hot_water": "水费",  # v2#7 前旧数据
    "public_utilities": "公摊水费",
    "electricity": "电费",
    "new_linen_prewash": "新布草过水费",
    "maintenance": "维修费",
    "gas": "燃气费",
    "supplies": "采购费",
    "kitchen_cleaning": "保洁",          # 厨房保洁归保洁
    "property_guidance_fee": "物业费",   # 物业引导费归物业
    # broadband / utilities(旧水电统称) / other → 「其他」兜底
}
_STATEMENT_COL_WIDTH = {
    "序号": 5, "模式": 9, "户型": 24, "小区名字": 18, "房号": 14,
    "房费": 11, "业主分成": 11, "新布草过水费": 12, "运营补贴": 10,
    "业主总支出": 12, "净利润": 12, "收款账号": 34,
}


def _statement_column_for(category: str, description: str) -> str:
    """把某笔业主支出归到主表哪一列。cleaning 按描述含「续住」拆保洁/续住保洁；
    BookingType 无 continuation 值，只能靠描述兜底（无「续住」字样默认落保洁，仍对账）。
    未识别类目一律落「其他」，保证 Σ各列 == 业主总支出。"""
    if category == "cleaning":
        return "续住保洁" if "续住" in (description or "") else "保洁"
    return _CATEGORY_TO_COLUMN.get(category, "其他")


def _stmt_round(x) -> Decimal:
    return Decimal(str(x or 0)).quantize(Decimal("0.01"))


def _build_statement_wb(settlement, owner, rooms_map: dict, desc_map: dict):
    """rooms_map: room_id -> (room_id, room_name, room_type, community_name)
    desc_map:  expense_id -> description（拆分保洁/续住保洁用）"""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "分成明细"

    header = (
        ["序号", "模式", "户型", "小区名字", "房号", "房费", "业主分成"]
        + _STATEMENT_EXPENSE_COLUMNS
        + ["运营补贴", "业主总支出", "净利润", "收款账号"]
    )
    ncol = len(header)

    # 标题行 + 表头行
    ws.append([f"{settlement.billing_month} 业主分成明细"])
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncol)
    tcell = ws.cell(row=1, column=1)
    tcell.font = Font(bold=True, size=13)
    tcell.alignment = Alignment(horizontal="center", vertical="center")

    ws.append(header)
    for c in range(1, ncol + 1):
        cell = ws.cell(row=2, column=c)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = HEADER_ALIGN
        cell.border = THIN_BORDER

    # 收款账号（户名/账号/开户行）
    payee = ""
    if owner:
        parts = [f"户名：{owner.name}"]
        if owner.bank_account:
            parts.append(f"账号：{owner.bank_account}")
        if owner.bank_name:
            parts.append(f"开户行：{owner.bank_name}")
        payee = "\n".join(parts)

    # 整个业主折成一行（王总 7-19：跟样表一样，一个业主一行合计）。
    # 逐 item 汇总金额 + 收集元数据（户型/小区去重、房号取范围、模式取综合比例）。
    import re

    totals = {k: Decimal("0") for k in _STATEMENT_EXPENSE_COLUMNS}
    category_totals: dict = {}  # 垫付明细用：column -> owner_amount 累计（正数）
    tot_fee = tot_share = tot_subsidy = tot_expense = tot_net = Decimal("0")
    room_types: list = []
    communities: list = []
    room_nums: list = []
    room_names_nonnum: list = []

    for i in settlement.items:
        room = rooms_map.get(i.room_id)
        rt = (room[2] if room else None) or ""
        cm = (room[3] if room else None) or ""
        if rt and rt not in room_types:
            room_types.append(rt)
        if cm and cm not in communities:
            communities.append(cm)
        if i.room_id:  # 业主级支出行（room_id 空）不进房号
            nm = (room[1] if room else None) or i.room_id or ""
            m = re.search(r"\d+", nm)
            if m:
                room_nums.append(int(m.group()))
            elif nm:
                room_names_nonnum.append(nm)

        tot_fee += _stmt_round(i.net_revenue)
        tot_expense += _stmt_round(i.owner_expenses)
        tot_net += _stmt_round(i.owner_net_amount)
        tot_share += _stmt_round(_stmt_round(i.owner_net_amount) + _stmt_round(i.owner_expenses))

        for e in (i.cost_share_breakdown or []):
            if e.get("category") == "平台补贴" or e.get("type") == "platform_subsidy":
                tot_subsidy += _stmt_round(e.get("amount"))
                continue
            oa = _stmt_round(e.get("owner_amount"))
            col = _statement_column_for(
                e.get("category", ""), desc_map.get(e.get("expense_id"), "")
            )
            totals[col] += oa
            if oa:
                category_totals[col] = category_totals.get(col, Decimal("0")) + oa

    # 模式：所有房同比例 → 「业主X%」；否则按综合比例（分成/房费）反推
    ratios = {Decimal(str(i.share_ratio_snapshot or 0)) for i in settlement.items if i.room_id}
    if len(ratios) == 1:
        mode = f"业主{int(next(iter(ratios)) * 100)}%"
    elif tot_fee > 0:
        mode = f"业主{int((tot_share / tot_fee * 100).to_integral_value())}%"
    else:
        mode = ""

    # 房号范围：数字房号取 min-max；有非数字（业主级 label 已排除）则补「等」
    if room_nums:
        lo, hi = min(room_nums), max(room_nums)
        room_cell = f"{lo}-{hi}" if hi > lo else str(lo)
        if room_names_nonnum:
            room_cell += "等"
    else:
        room_cell = "、".join(room_names_nonnum)

    row = [1, mode, "、".join(room_types), "、".join(communities), room_cell,
           float(tot_fee), float(tot_share)]
    for k in _STATEMENT_EXPENSE_COLUMNS:
        v = totals[k]
        row.append(float(-v) if v else "")   # 支出列显示负数；0 留空
    row.append(float(tot_subsidy) if tot_subsidy else "")
    row.append(float(-tot_expense))
    row.append(float(tot_net))
    row.append(payee)
    ws.append(row)

    # 数据行：垂直居中，长文本列自动换行，行高给足（收款账号多行）
    data_row = ws.max_row
    for c in range(1, ncol + 1):
        wrap = header[c - 1] in ("户型", "小区名字", "收款账号")
        ws.cell(row=data_row, column=c).alignment = Alignment(vertical="center", wrap_text=wrap)
    ws.row_dimensions[data_row].height = 46

    # 列宽（中文列加宽，避免截断）
    for idx, name in enumerate(header, start=1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(idx)].width = (
            _STATEMENT_COL_WIDTH.get(name, 9)
        )

    # ── 垫付费用明细：接在主表下方（同一 sheet，对齐样表上下堆叠）──
    r0 = data_row + 2  # 主表下空一行再起
    tcell = ws.cell(row=r0, column=1, value="垫付费用明细")
    ws.merge_cells(start_row=r0, start_column=1, end_row=r0, end_column=5)
    tcell.font = Font(bold=True, size=13)
    tcell.alignment = Alignment(horizontal="center", vertical="center")

    detail_header = ["序号", "费用名称", "费用数量", "金额", "备注"]
    for c, name in enumerate(detail_header, start=1):
        cell = ws.cell(row=r0 + 1, column=c, value=name)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = HEADER_ALIGN
        cell.border = THIN_BORDER

    rr = r0 + 2
    dseq = 0
    for k in _STATEMENT_EXPENSE_COLUMNS:  # 按主表列顺序，只列有金额的类目
        amt = category_totals.get(k)
        if not amt:
            continue
        dseq += 1
        ws.cell(row=rr, column=1, value=dseq)
        ws.cell(row=rr, column=2, value=k)
        # 费用数量(C)/备注(E) 留空手填（王总口径）；金额(D)
        ws.cell(row=rr, column=4, value=float(amt))
        rr += 1
    if dseq == 0:
        ws.cell(row=rr, column=1, value=1)
        ws.cell(row=rr, column=2, value="（本月无垫付费用）")

    return wb


@router.get("/settlements/{settlement_id}/statement")
async def export_settlement_statement(
    settlement_id: str,
    db: DBSession,
    current_user: CurrentUser,
):
    """单个业主结算 → 『业主分成明细 + 垫付费用明细』Excel（可直接发给业主）。"""
    from fastapi import HTTPException

    if current_user["role"] not in ("admin", "finance"):
        raise HTTPException(status_code=403, detail="无权导出结算明细")

    result = await db.execute(
        select(OwnerSettlement)
        .options(selectinload(OwnerSettlement.items))
        .where(OwnerSettlement.settlement_id == settlement_id)
    )
    settlement = result.scalar_one_or_none()
    if not settlement:
        raise HTTPException(status_code=404, detail="结算记录不存在")

    owner = (await db.execute(
        select(Owner).where(Owner.owner_id == settlement.owner_id)
    )).scalar_one_or_none()

    room_ids = [i.room_id for i in settlement.items if i.room_id]
    rooms_map: dict = {}
    if room_ids:
        rr = await db.execute(
            select(Room.room_id, Room.room_name, Room.room_type, Room.community_name)
            .where(Room.room_id.in_(room_ids))
        )
        rooms_map = {row[0]: row for row in rr.all()}

    expense_ids = {
        e.get("expense_id")
        for i in settlement.items
        for e in (i.cost_share_breakdown or [])
        if e.get("expense_id")
    }
    desc_map: dict = {}
    if expense_ids:
        er = await db.execute(
            select(Expense.expense_id, Expense.description)
            .where(Expense.expense_id.in_(expense_ids))
        )
        desc_map = {row[0]: row[1] for row in er.all()}

    wb = _build_statement_wb(settlement, owner, rooms_map, desc_map)
    owner_name = (owner.name if owner else None) or settlement.owner_id
    return _to_streaming(wb, f"业主分成明细_{owner_name}_{settlement.billing_month}.xlsx")
