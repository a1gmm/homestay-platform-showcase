"""
Intelligent room pricing engine.

Multiplier-based algorithm blended with competitor reference prices.
Calculates real occupancy from orders, caches competitor scrapes per city+date,
and provides full factor audit trail.
"""
import httpx
import logging
from datetime import date, timedelta
from app.core.datetime_helpers import today_cn
from decimal import Decimal
import statistics
from typing import List, Optional, Dict, Tuple
from urllib.parse import quote

from sqlalchemy import select, func, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.room import Room
from app.models.order import Order, OrderStatus, BookingType
from app.models.pricing import PricingRecord
from app.models.order_room import OrderRoom
import uuid

logger = logging.getLogger(__name__)


async def compute_listing_base_by_room_type(
    session: AsyncSession,
    cutoff: date,
) -> Dict[str, Tuple[float, int]]:
    """按 room_type 汇总 OTA 搬单挂牌价(list_price)每晚中位 → {room_type: (median, n)}。

    issue #103 Step 2：base 反映真实携程挂牌价量级。挂牌价是上架即定、与是否成行无关，
    故不按已成行过滤；仅排除 cancelled(防错单/测试单)。手填单无 list_price 且非 ota-sync，
    自然不计入。SQLite 测试库 JSONB 查询不可移植，source 过滤在 Python 侧做。
    """
    rows = (await session.execute(
        select(Order, Room.room_type)
        .join(Room, Room.room_id == Order.room_id)
        .where(
            Order.booking_type == BookingType.normal,
            Order.is_deleted == False,
            Order.list_price.isnot(None),
            Order.order_status != OrderStatus.cancelled,
            Order.check_in_date >= cutoff,
        )
    )).all()
    buckets: Dict[str, List[float]] = {}
    for o, rtype in rows:
        if (o.metadata_ or {}).get("source") != "ota-sync":
            continue
        nights = (o.check_out_date - o.check_in_date).days
        if rtype and nights and nights > 0 and o.list_price:
            buckets.setdefault(rtype, []).append(float(o.list_price) / nights)
    return {t: (statistics.median(v), len(v)) for t, v in buckets.items()}


def propose_listing_base(median: float, round_to: int = 10) -> int:
    """挂牌价中位 → 取整到最近的 ¥round_to 的底价(下限 round_to)。

    平局向偶数取整(Python banker's rounding)，对人工复核的定价提示足够。
    """
    return max(round_to, round(median / round_to) * round_to)

# ─── Holiday Calendar ────────────────────────────────────────────────────────
# Exact dates from 国办发明电〔2025〕7号. Multipliers are our pricing policy,
# not government-prescribed rates. Unknown years never reuse lunar dates.
HOLIDAY_CALENDAR_SOURCE = "https://www.gov.cn/zhengce/zhengceku/202511/content_7047091.htm"
HOLIDAY_CALENDAR_YEARS = frozenset({2026})
HOLIDAYS: Dict[str, float] = {}
for _start, _multipliers in (
    (date(2026, 1, 1), (1.3, 1.3, 1.3)),
    (date(2026, 2, 15), (1.8, 2.0, 2.0, 1.8, 1.8, 2.0, 2.0, 1.8, 1.8)),
    (date(2026, 4, 4), (1.3, 1.4, 1.3)),
    (date(2026, 5, 1), (1.4, 1.5, 1.4, 1.3, 1.3)),
    (date(2026, 6, 19), (1.3, 1.3, 1.3)),
    (date(2026, 9, 25), (1.3, 1.3, 1.3)),
    (date(2026, 10, 1), (1.8, 1.8, 1.8, 1.6, 1.5, 1.4, 1.3)),
):
    for _offset, _multiplier in enumerate(_multipliers):
        HOLIDAYS[(_start + timedelta(days=_offset)).isoformat()] = _multiplier

# Local festivals also require an explicit year (YYYY-MM-DD).
LOCATION_HOLIDAYS: Dict[str, float] = {}


# ─── Competitor price cache (per city+date, refreshed daily) ─────────────────
_competitor_cache: Dict[str, Tuple[date, List[dict]]] = {}


def build_ctrip_url(keyword: str, checkin_date: date) -> str:
    """Build Ctrip PC search URL for price traceability."""
    checkout_date = checkin_date + timedelta(days=1)
    safe_kw = quote(keyword)
    return (
        "https://hotels.ctrip.com/hotel/list?countryId=1"
        f"&checkin={checkin_date.isoformat()}"
        f"&checkout={checkout_date.isoformat()}"
        f"&keyword={safe_kw}"
    )


# ─── Multiplier Functions ────────────────────────────────────────────────────

def get_day_of_week_multiplier(d: date) -> float:
    """Mon-Thu cheaper, Fri-Sun premium."""
    dow = d.weekday()
    return {0: 0.88, 1: 0.88, 2: 0.90, 3: 0.92, 4: 1.10, 5: 1.22, 6: 1.12}[dow]


def get_holiday_multiplier(d: date) -> float:
    """Holiday premium based on calendar — national + per-deployment local festivals."""
    key = d.isoformat()
    return max(HOLIDAYS.get(key, 1.0), LOCATION_HOLIDAYS.get(key, 1.0))


def get_lead_time_multiplier(lead_days: int) -> float:
    """
    Dynamic pricing based on booking lead time:
    - Last minute (<=0 days): discount to fill rooms
    - 1-2 days: slight discount
    - 3-7 days: standard
    - 8-21 days: slight premium (demand signal)
    - 22+ days: early bird discount to lock revenue
    """
    if lead_days <= 0:
        return 0.85
    if lead_days <= 2:
        return 0.90
    if lead_days <= 7:
        return 1.00
    if lead_days <= 21:
        return 1.05
    # Early bird: slight discount to incentivize advance booking
    return 0.95


def get_occupancy_multiplier(occ_rate: float) -> float:
    """Higher occupancy = higher price (supply/demand)."""
    if occ_rate >= 0.90:
        return 1.25
    if occ_rate >= 0.75:
        return 1.12
    if occ_rate >= 0.55:
        return 1.00
    if occ_rate >= 0.35:
        return 0.92
    return 0.82


# ─── Real Occupancy Calculation ──────────────────────────────────────────────

async def get_occupancy_for_range(
    session: AsyncSession, start_date: date, days: int, total_rooms: int,
) -> Dict[date, float]:
    """Count distinct active rooms per night with one bounded interval query.

    Room segments are authoritative after a split/transfer. Legacy orders are
    used only when they have no segments at all, including unassigned segments.
    Merge overlapping intervals per room before the difference-array sweep so
    duplicate/continuation rows cannot inflate demand.
    """
    if days <= 0:
        return {}
    if total_rooms <= 0:
        return {start_date + timedelta(days=i): 0.5 for i in range(days)}
    end_date = start_date + timedelta(days=days)
    active = (Order.is_deleted.is_(False), Order.order_status != OrderStatus.cancelled,
              Room.is_deleted.is_not(True))
    segments = (
        select(OrderRoom.room_id, OrderRoom.check_in_date, OrderRoom.check_out_date)
        .join(Order, Order.order_id == OrderRoom.order_id)
        .join(Room, Room.room_id == OrderRoom.room_id)
        .where(*active, OrderRoom.check_in_date < end_date,
               OrderRoom.check_out_date > start_date)
    )
    legacy = (
        select(Order.room_id, Order.check_in_date, Order.check_out_date)
        .join(Room, Room.room_id == Order.room_id)
        .where(*active, Order.check_in_date < end_date, Order.check_out_date > start_date,
               ~select(OrderRoom.order_room_id).where(
                   OrderRoom.order_id == Order.order_id).exists())
    )
    by_room: Dict[str, List[Tuple[int, int]]] = {}
    for room_id, check_in, check_out in (await session.execute(union_all(segments, legacy))).all():
        lo = max(0, (check_in - start_date).days)
        hi = min(days, (check_out - start_date).days)
        if lo < hi:
            by_room.setdefault(room_id, []).append((lo, hi))
    changes = [0] * (days + 1)
    for intervals in by_room.values():
        merged = []
        for lo, hi in sorted(intervals):
            if merged and lo <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
            else:
                merged.append((lo, hi))
        for lo, hi in merged:
            changes[lo] += 1
            changes[hi] -= 1
    occupied = 0
    rates = {}
    for offset in range(days):
        occupied += changes[offset]
        rates[start_date + timedelta(days=offset)] = min(occupied / total_rooms, 1.0)
    return rates


async def get_real_occupancy(
    session: AsyncSession, check_date: date, total_rooms: int,
) -> float:
    """Booked room occupancy for the half-open night [date, date + 1)."""
    return (await get_occupancy_for_range(session, check_date, 1, total_rooms))[check_date]


# ─── Competitor Scraping (with caching) ──────────────────────────────────────

async def scrape_competitor_prices(
    city: str, district: str, check_date: date
) -> List[dict]:
    """
    Scrape Ctrip competitor prices with per-city daily caching.
    Returns: [{"name": str|None, "price": float}, ...]
    """
    # No city configured — skip the scrape entirely. Caller still gets an empty
    # benchmark and falls back to base_price + multipliers, no exception, no log spam.
    if not city:
        return []
    # Check cache first — same city+date only scrapes once
    cache_key = f"{city}:{district}:{check_date.isoformat()}"
    cached = _competitor_cache.get(cache_key)
    if cached and cached[0] == today_cn():
        return cached[1]

    hotels: List[dict] = []
    date_str = check_date.strftime("%Y-%m-%d")
    checkout_str = (check_date + timedelta(days=1)).strftime("%Y-%m-%d")

    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
        "Accept": "application/json",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }

    try:
        async with httpx.AsyncClient(timeout=15, headers=headers) as client:
            url = (
                "https://m.ctrip.com/restapi/soa2/18928/json/searchList"
                f"?city={quote(city)}&checkIn={date_str}&checkOut={checkout_str}"
                "&pageIndex=1&pageSize=20&type=hotel&filterType=ehotel"
            )
            resp = await client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                items = data.get("Response", {}).get("hotelList", [])
                for item in items:
                    price = item.get("lowPrice") or item.get("price")
                    if price and 50 < float(price) < 5000:
                        hotels.append({
                            "name": item.get("hotelName") or item.get("name"),
                            "price": float(price),
                        })
            else:
                logger.warning("携程 API 返回 %d，城市: %s", resp.status_code, city)
    except httpx.TimeoutException:
        logger.warning("携程 API 超时，城市: %s, 日期: %s", city, check_date)
    except Exception as exc:
        logger.warning("携程爬取失败: %s, 城市: %s", type(exc).__name__, city)

    # Cache result (even empty, to avoid repeated failures)
    _competitor_cache[cache_key] = (today_cn(), hotels)
    return hotels


# ─── Core Pricing Algorithm ──────────────────────────────────────────────────

def calculate_recommended_price(
    base_price: float,
    check_date: date,
    occ_rate: float,
    competitor_prices: List[float],
    weekend_markup: float = 0,
    holiday_markup: float = 0,
) -> dict:
    """
    Calculate recommended price using multiplicative model.

    All multipliers are MULTIPLIED together (not max'd):
    final = base × dow × holiday × lead_time × occupancy

    Then blended with competitor reference price.
    """
    today = today_cn()
    lead_days = (check_date - today).days

    dow_m = get_day_of_week_multiplier(check_date)
    holiday_m = get_holiday_multiplier(check_date)
    lead_m = get_lead_time_multiplier(lead_days)
    occ_m = get_occupancy_multiplier(occ_rate)

    # Apply room-level overrides if configured
    if weekend_markup > 0 and check_date.weekday() >= 4:  # Fri-Sun
        dow_m = max(dow_m, 1.0 + weekend_markup)
    if holiday_markup > 0 and holiday_m > 1.0:
        holiday_m = max(holiday_m, 1.0 + holiday_markup)

    # MULTIPLICATIVE combination: all factors compound
    # e.g. holiday weekend: 1.22 × 1.8 × 1.05 × 1.12 = 2.57x
    combined_multiplier = dow_m * holiday_m * lead_m * occ_m
    algo_price = base_price * combined_multiplier

    # Blend with competitor reference (if enough data)
    comp_ref: Optional[float] = None
    comp_avg: Optional[float] = None
    if len(competitor_prices) >= 3:
        sorted_prices = sorted(competitor_prices)
        # Trimmed mean: remove top/bottom 10% outliers
        trim = max(1, len(sorted_prices) // 10)
        trimmed = sorted_prices[trim:-trim] if len(sorted_prices) > 2 * trim else sorted_prices
        comp_avg = statistics.mean(trimmed)
        # Use median instead of P25 for more robust reference
        comp_ref = statistics.median(sorted_prices)
        # Blend: 55% algorithm + 45% competitor median
        blended = algo_price * 0.55 + comp_ref * 0.45
    else:
        blended = algo_price

    # Clamp to min/max bounds
    min_price = base_price * 0.65
    max_price = base_price * 3.5
    final = max(min_price, min(max_price, blended))
    # Round to nearest 5
    final = round(final / 5) * 5

    return {
        "recommended": final,
        "min_price": round(min_price / 5) * 5,
        "max_price": round(max_price / 5) * 5,
        "factors": {
            "base_price": base_price,
            "day_of_week": round(dow_m, 3),
            "holiday": round(holiday_m, 3),
            "holiday_calendar_known": check_date.year in HOLIDAY_CALENDAR_YEARS,
            "lead_time": round(lead_m, 3),
            "occupancy": round(occ_m, 3),
            "occupancy_rate": round(occ_rate, 3),
            "combined": round(combined_multiplier, 3),
        },
        "competitor_ref": round(comp_ref, 2) if comp_ref else None,
        "competitor_avg": round(comp_avg, 2) if comp_avg else None,
    }


# ─── Upsert Pricing Record ──────────────────────────────────────────────────

async def upsert_pricing_record(
    session: AsyncSession,
    room: Room,
    check_date: date,
    result: dict,
    keyword: str,
    *,
    records_by_date: Optional[Dict[date, PricingRecord]] = None,
) -> PricingRecord:
    """Insert or update pricing record for a room+date."""
    if records_by_date is None:
        existing = await session.execute(
            select(PricingRecord).where(
                PricingRecord.room_id == room.room_id,
                PricingRecord.effective_date == check_date,
            )
        )
        record = existing.scalar_one_or_none()
    else:
        record = records_by_date.get(check_date)
    if record is None:
        record = PricingRecord(
            pricing_id="PRC-" + uuid.uuid4().hex[:12].upper(),
            room_id=room.room_id,
            effective_date=check_date,
            price=Decimal(str(result["recommended"])),
        )
        session.add(record)
        if records_by_date is not None:
            records_by_date[check_date] = record

    record.recommended_price = Decimal(str(result["recommended"]))
    record.base_price = room.base_price
    comp_ref = result.get("competitor_ref")
    record.competitor_avg_price = (
        Decimal(str(comp_ref)) if comp_ref is not None else None
    )
    factors = result.get("factors") or {}
    competitors_sample = result.get("competitors_sample")
    if competitors_sample:
        factors["competitors_sample"] = competitors_sample
    record.algorithm_factors = factors
    record.source = "competitor" if comp_ref else "rule_engine"
    record.source_url = build_ctrip_url(keyword, check_date)
    record.price = Decimal(str(result["recommended"]))
    return record


# ─── Main Entry Point ────────────────────────────────────────────────────────

async def calculate_room_pricing_for_range(
    session: AsyncSession,
    room: Room,
    start_date: date,
    days: int,
) -> int:
    """
    Calculate pricing for a room over a date range.
    Returns number of days priced.
    """
    if not room.base_price or days <= 0:
        return 0

    # No default city — if room.city is unset the competitor scraper short-circuits to []
    # so pricing falls back to base_price + multipliers without external benchmark.
    city = room.city or ""
    district = room.district or ""
    keyword_parts = [city] if city else []
    if district:
        keyword_parts.append(district)
    if room.community_name:
        keyword_parts.append(room.community_name)
    search_keyword = " ".join(keyword_parts)

    # Scrape competitors ONCE per city+date (cached)
    try:
        competitor_hotels = await scrape_competitor_prices(
            city, district, start_date + timedelta(days=1)
        )
    except Exception as exc:
        logger.warning("竞品爬取失败 (room %s): %s", room.room_id, exc)
        competitor_hotels = []

    competitor_prices = [h["price"] for h in competitor_hotels]

    # Get total room count for occupancy calculation
    total_rooms = await session.scalar(
        select(func.count()).select_from(Room).where(Room.is_deleted.is_not(True))
    ) or 0
    occupancy = await get_occupancy_for_range(session, start_date, days, total_rooms)
    records_by_date = {record.effective_date: record for record in await session.scalars(
        select(PricingRecord).where(
            PricingRecord.room_id == room.room_id,
            PricingRecord.effective_date >= start_date,
            PricingRecord.effective_date < start_date + timedelta(days=days),
        )
    )}

    # Room-level price rule overrides
    weekend_markup = float(getattr(room, "weekend_markup", 0) or 0)
    holiday_markup = float(getattr(room, "holiday_markup", 0) or 0)

    # base price 直接采用房间挂牌价底价(issue #103 Step 2 起 base 反映真实挂牌价量级)。
    # 动态锚已退休：base 已是 OTA 挂牌价池化值，再锚同源即空转(冗余)。
    effective_base = float(room.base_price)

    priced = 0
    for delta in range(days):
        check_date = start_date + timedelta(days=delta)

        # Real occupancy from orders
        occ_rate = occupancy[check_date]

        result = calculate_recommended_price(
            base_price=effective_base,
            check_date=check_date,
            occ_rate=occ_rate,
            competitor_prices=competitor_prices,
            weekend_markup=weekend_markup,
            holiday_markup=holiday_markup,
        )
        # Attach competitor sample for audit trail (middle range, not just cheapest)
        if competitor_hotels:
            sorted_hotels = sorted(
                [h for h in competitor_hotels if isinstance(h.get("price"), (int, float))],
                key=lambda h: h["price"],
            )
            # Take middle 10 instead of cheapest 10
            mid_start = max(0, len(sorted_hotels) // 4)
            mid_end = min(len(sorted_hotels), mid_start + 10)
            result["competitors_sample"] = [
                {"name": h.get("name"), "price": h["price"]}
                for h in sorted_hotels[mid_start:mid_end]
            ]

        await upsert_pricing_record(
            session, room, check_date, result, search_keyword, records_by_date=records_by_date,
        )
        priced += 1

    return priced
