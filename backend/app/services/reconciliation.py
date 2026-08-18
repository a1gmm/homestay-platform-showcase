"""对账比对核心（纯逻辑，无 IO，便于单测）。

按**客人整体**对账（2026-07-02）：一个客人在我们系统所有单的实收合计（不含取消单），
对比他在 bypms 所有单的房费合计（不含取消态）。相等=账平（自动消掉「续住被 bypms
按日期拆段」「一单多房」等假警报）；对不上=真差异，并打**原因标签**便于分诊。

匹配键（先 pid 后名字）：bypms 行的 pid 若在我们某单上，归到那个客人（修
「张/馨月, LEE/SIU LAN」vs「LEE/SIU LAN」这类两侧登记名不同的误报）；否则按
归一化客人名（统一全/半角逗号、去空格）。

输出：
  guest_diff：只看 bypms 里出现过的客人（OTA 客人）；我们合计 ≠ bypms 合计（容差 1 元）。
    每条带 reason：
      cancelled_stale — 我们已取消、bypms 仍挂着（bypms 取消同步未做，多为可略过）
      missing         — 完全漏录（我们一单都没有）
      short           — 我们偏少（疑漏了几间房/金额偏低）
      over            — 我们偏多（疑多录/金额偏高）
    并带两侧明细 our_orders（含取消单+状态）/ bypms_orders（matched=我们已有对应单；
    未 matched 的就是缺的，前端给「补建」按钮）。
    （只在我们系统、不在 bypms 的散客/自来客不参与，避免误报。）
  duplicates：同一 platform_order_id 对应我们 ≥2 条订单（真重复录入）。
"""
import re
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP

from app.services.order_pricing import compute_daily_prices, derive_ota_commission_rate

GUEST_TOL = Decimal("1.0")
CANCELLED_STATES = {"D"}  # bypms 取消态
_TWO = Decimal("0.01")


def _dec(v):
    return Decimal(str(v)) if v is not None else None


def _norm_name(g: str | None) -> str:
    """客人名归一化用于两侧匹配：统一全角/半角逗号、去空格。
    （多客连写如「成华，袁陵」bypms 用半角、我们用全角，否则会误判漏录。）"""
    return (g or "").replace("，", ",").replace("、", ",").replace(" ", "").strip()


def _our_default():
    return {"total": Decimal("0"), "order_ids": [], "locked": False,
            "has_cancelled": False, "all_pids": set(), "display": "",
            "orders": []}


def classify_reconciliation(orders: list[dict], staging: list[dict]) -> dict:
    # ── 重复单：同 pid 多条 order ──
    orders_by_pid: dict[str, list[dict]] = defaultdict(list)
    for o in orders:
        if o.get("platform_order_id"):
            orders_by_pid[o["platform_order_id"]].append(o)
    duplicates = [
        {
            "platform_order_id": pid,
            "order_ids": [o["order_id"] for o in olist],
            "guest_name": olist[0].get("guest_name"),
        }
        for pid, olist in orders_by_pid.items() if len(olist) > 1
    ]

    # ── 我们侧：按客人汇总（实收不含取消单；all_pids/has_cancelled/orders 记全部）──
    our: dict[str, dict] = defaultdict(_our_default)
    pid_to_key: dict[str, str] = {}
    # 认领候选索引：无平台单号、非取消的手录单，按 (客人, 入住日) 索引 —— bypms 未匹配行
    # 若有唯一候选，动作应是「关联」而非「补建」（防重复建单）。
    link_cands: dict[tuple, list[str]] = defaultdict(list)
    for o in orders:
        g = o.get("guest_name")
        if not g:
            continue
        k = _norm_name(g)
        our[k]["display"] = our[k]["display"] or g
        pid = o.get("platform_order_id")
        if pid:
            our[k]["all_pids"].add(pid)
            pid_to_key.setdefault(pid, k)
        elif o.get("order_status") != "cancelled":
            link_cands[(k, o.get("check_in"))].append(o["order_id"])
        our[k]["orders"].append({
            "order_id": o["order_id"],
            "actual_price": _dec(o.get("actual_price")),
            "check_in": o.get("check_in"),
            "order_status": o.get("order_status"),
            "price_locked": bool(o.get("price_locked")),
        })
        if o.get("order_status") == "cancelled":
            our[k]["has_cancelled"] = True
            continue
        ap = _dec(o.get("actual_price"))
        if ap is None:
            continue
        our[k]["total"] += ap
        our[k]["order_ids"].append(o["order_id"])
        if o.get("price_locked"):
            our[k]["locked"] = True

    all_our_pids = set(pid_to_key.keys())

    # ── bypms 侧：按客人汇总房费（排除取消态 + 已忽略[记性]）；pid 优先归队 ──
    byp: dict[str, dict] = defaultdict(
        lambda: {"total": Decimal("0"), "pids": [], "display": "", "rows": []})
    ignored_count = 0
    ghost_count = 0
    for s in staging:
        if (s.get("state") or "").upper() in CANCELLED_STATES:
            continue
        if s.get("ghost"):
            # 幽灵单：bypms 已删除（同步长时间没再抓到，如改单/重新下单后的旧单），
            # 仍挂 state=P 但不该参与比对（实证 2026-07-03 魏珊珊 1233+幽灵1278 被误报偏少）
            ghost_count += 1
            continue
        if s.get("reconcile_status") == "ignored":
            ignored_count += 1
            continue
        g = s.get("guest_name")
        raw = s.get("raw") or {}
        fang = _dec(raw.get("priceFang"))
        if not g or fang is None:
            continue
        pid = s.get("platform_order_id")
        k = pid_to_key.get(pid) or _norm_name(g)  # pid 优先归队
        matched = pid in all_our_pids
        cand_list = [] if matched else link_cands.get((k, s.get("check_in")), [])
        byp[k]["display"] = byp[k]["display"] or g
        byp[k]["total"] += fang
        byp[k]["pids"].append(pid)
        byp[k]["rows"].append({
            "platform_order_id": pid,
            "bypms_fang": fang,
            "check_in": s.get("check_in"),
            "room_type": s.get("room_type"),
            "channel_chs": raw.get("channelChs"),
            "matched": matched,
            # 唯一候选才给（≥2 个候选歧义，留人工）
            "link_candidate": cand_list[0] if len(cand_list) == 1 else None,
            "suggested_action": None,  # 定 reason 后回填
        })

    # ── 只对 bypms 里出现过的客人比对（散客不参与）──
    guest_diff = []
    for k, b in byp.items():
        o = our.get(k) or _our_default()
        our_total = o["total"]
        diff = b["total"] - our_total
        if abs(diff) < GUEST_TOL:
            continue
        matched = bool(set(b["pids"]) & o["all_pids"])
        if our_total == 0 and o["has_cancelled"] and matched:
            reason = "cancelled_stale"
        elif our_total == 0:
            reason = "missing"
        elif diff > 0:
            reason = "short"
        else:
            reason = "over"
        # 回填每条 bypms 行的建议动作：未匹配→有唯一候选关联，否则补建。
        # cancelled_stale **不给自动动作**（2026-07-04 孙晶案：我们取消错了、bypms 单
        # 真实有效 ¥766 在漏——自动忽略会把真损失永久藏掉；必须人工判断后手动忽略/恢复）。
        if reason != "cancelled_stale":
            for r in b["rows"]:
                if not r["matched"]:
                    r["suggested_action"] = "link" if r["link_candidate"] else "create"
        guest_diff.append({
            "guest_name": b["display"] or o["display"],
            "our_total": our_total,
            "bypms_total": b["total"],
            "diff": diff,
            "our_order_count": len(o["order_ids"]),
            "bypms_count": len(b["pids"]),
            "price_locked": o["locked"],
            "reason": reason,
            # 两侧都按入住日排序，展开时左右行能对应上（用户反馈 2026-07-03）
            "our_orders": sorted(o["orders"], key=lambda x: str(x.get("check_in") or "")),
            "bypms_orders": sorted(b["rows"], key=lambda x: str(x.get("check_in") or "")),
        })
    guest_diff.sort(key=lambda r: -abs(r["diff"]))

    return {"guest_diff": guest_diff, "duplicates": duplicates,
            "ignored_count": ignored_count, "ghost_count": ghost_count}


# ── 内部自查检测器（不比 bypms，只查我们自己订单的自相矛盾）──────────────
# 对账工具（classify_reconciliation）只比「我们 vs bypms 总额」，有结构性死角：
#   ① bypms 上门单 priceFang 恒为 0 → 我们记 ¥0，两侧都 0「完美匹配」永不报（周浩/韩毅案）
#   ② 内部金额自相矛盾（订单总额≠房间和、房间价≠日价和）——孙晶案那类，bypms 比不出
#   ③ 佣金率被反推成 100% 把房东分成清零（月底分账地雷）
#   ④ 同房日期重叠双订（换房残留/误录）
# 这些改由 detect_anomalies 直接扫我们订单，页面自动亮红，不再靠人肉查。

_OTA_CHANNELS = {"ctrip", "qunar", "zhixing", "tongcheng", "meituan",
                 "meituan_hotel", "fliggy", "douyin"}
_FREE_MARKERS = ("免", "刷单", "内部", "测试", "自用")   # 免房/刷单等业主本就分 0，不报
_EARNED_STATUSES = {"checked_in", "pending_checkout", "completed"}  # 钱已挣，记 0 = 铁定漏
_COMM_ZERO = Decimal("0.99")  # 佣金率≥此即视作把房东清零


def _iso(v) -> str:
    """日期归一成 ISO 串（date 对象或字符串皆可），ISO 串按字典序即时序。"""
    if isinstance(v, (date, datetime)):
        return v.isoformat()[:10]
    return str(v)[:10] if v else ""


def _is_free_order(name: str | None) -> bool:
    return any(m in (name or "") for m in _FREE_MARKERS)


def detect_anomalies(orders: list[dict]) -> list[dict]:
    """扫我们自己的订单找自相矛盾（纯函数）。每条 order 需带 rooms=[{room_id,check_in,
    check_out,actual_price,daily_prices}]、commission_rate、metadata。返回异常列表，
    按严重度 high>medium>low 排。类型：zero_price / commission_zeroed /
    internal_mismatch / room_overlap。"""
    anomalies: list[dict] = []
    room_segs: dict[str, list] = defaultdict(list)

    for o in orders:
        status = o.get("order_status")
        if status == "cancelled":
            continue
        oid = o["order_id"]
        name = o.get("guest_name")
        ap = _dec(o.get("actual_price"))
        meta = o.get("metadata") or {}
        price_pending = str(meta.get("price_pending")).lower() == "true"
        rate = _dec(o.get("commission_rate"))
        rooms = o.get("rooms") or []

        # ① 零价活跃单（排除 OTA「价格同步中」占位）
        if not price_pending and (ap is None or ap == 0):
            earned = status in _EARNED_STATUSES
            anomalies.append({
                "type": "zero_price", "severity": "high" if earned else "low",
                "order_id": oid, "guest_name": name, "channel": o.get("channel"),
                "order_status": status, "check_in": o.get("check_in"),
                "amount": None,
                "detail": ("已完成/在住却记 ¥0，疑漏录价格" if earned
                           else "实收 ¥0，可能尚未录价"),
            })

        # ③ 佣金率把房东清零（可能是活动/免房单，只报不自动改）
        if (ap is not None and ap > 0 and rate is not None
                and rate >= _COMM_ZERO and not _is_free_order(name)):
            anomalies.append({
                "type": "commission_zeroed", "severity": "medium",
                "order_id": oid, "guest_name": name, "channel": o.get("channel"),
                "check_in": o.get("check_in"), "amount": ap,
                "detail": "佣金率 100%，房东分成 ¥0（若非活动单需修正佣金率）",
            })

        # ② 内部金额不平：订单总额 vs 房间和；房间价 vs 日价和
        if rooms:
            rsum = sum((_dec(r.get("actual_price")) or Decimal("0")) for r in rooms)
            if ap is not None and abs(ap - rsum) > _TWO:
                anomalies.append({
                    "type": "internal_mismatch", "severity": "high",
                    "order_id": oid, "guest_name": name, "check_in": o.get("check_in"),
                    "amount": ap,
                    "detail": f"订单总额与各房间之和对不上（差 {abs(ap - rsum)}）",
                })
            for r in rooms:
                dp = r.get("daily_prices") or {}
                dsum = sum((_dec(v) or Decimal("0")) for v in dp.values())
                rap = _dec(r.get("actual_price"))
                if rap is not None and dp and abs(rap - dsum) > _TWO:
                    anomalies.append({
                        "type": "internal_mismatch", "severity": "high",
                        "order_id": oid, "guest_name": name, "check_in": o.get("check_in"),
                        "amount": rap,
                        "detail": f"房间 {r.get('room_id')} 房价与每日价之和对不上",
                    })

        for r in rooms:
            rid = r.get("room_id")
            ci, co = _iso(r.get("check_in")), _iso(r.get("check_out"))
            if rid and ci and co:
                room_segs[rid].append((ci, co, oid, name))

    # ④ 同房重叠双订（排他语义：同日退+入不算重叠）
    for rid, segs in room_segs.items():
        segs.sort(key=lambda x: x[0])
        for i in range(len(segs)):
            for j in range(i + 1, len(segs)):
                a, b = segs[i], segs[j]
                if b[0] < a[1]:            # b.ci < a.co → 重叠
                    if a[2] != b[2]:       # 不同订单
                        anomalies.append({
                            "type": "room_overlap", "severity": "high",
                            "order_id": a[2], "guest_name": a[3], "room_id": rid,
                            "check_in": a[0], "amount": None,
                            "detail": (f"房间 {rid} 与 {b[3]}（{b[2]}）"
                                       f"日期重叠 {b[0]}~{b[1]}"),
                            "other_order_id": b[2],
                        })
                else:
                    break

    _rank = {"high": 0, "medium": 1, "low": 2}
    anomalies.sort(key=lambda x: (_rank.get(x["severity"], 3), str(x.get("check_in") or "")))
    return anomalies


def build_auto_fix_plan(guest_diff: list[dict]) -> dict:
    """自动修复计划（纯函数）：把 guest_diff 里带建议动作的行归成三桶。
    creates=无候选的缺单补建；links=唯一候选关联。
    over（我们偏多）与 cancelled_stale（我们取消了但 bypms 还有效——可能取消错了在漏钱，
    孙晶案 2026-07-04）都没有安全的自动动作，留人工；ignores 恒为空（保留键做 API 兼容）。"""
    creates, links = [], []
    for row in guest_diff:
        for b in row["bypms_orders"]:
            act = b.get("suggested_action")
            if act == "create":
                creates.append({"platform_order_id": b["platform_order_id"],
                                "guest_name": row["guest_name"],
                                "bypms_fang": b["bypms_fang"],
                                "check_in": b["check_in"],
                                "room_type": b["room_type"]})
            elif act == "link":
                links.append({"platform_order_id": b["platform_order_id"],
                              "order_id": b["link_candidate"],
                              "guest_name": row["guest_name"]})
    return {"creates": creates, "links": links, "ignores": []}


# ── 补建缺失单：从 bypms staging 原始构建订单载荷（口径与 ota-sync bypms_import 一致）──

# bypms 渠道中文 → Channel 枚举。子串匹配；channelChs 判成携程时再用备注「订单来源」纠正
# 子渠道（与 ota-sync resolve_channel 同口径，见 memory bypms-channel-umbrella）。
_CHANNEL_MARKERS = [
    ("携程", "ctrip"), ("飞猪", "fliggy"), ("抖音", "douyin"), ("去哪", "qunar"),
    ("智行", "zhixing"), ("同程", "tongcheng"), ("美团", "meituan_hotel"),
    ("自来客", "self_acquired"), ("上门客", "offline"), ("散客", "offline"),
]
_ORDER_SOURCE_RE = re.compile(r"订单来源[:：]\s*([^\s，,。;；]+)")
_STATE_STATUS = {"P": "pending_confirm", "E": "completed"}


def _match_channel(s: str | None) -> str | None:
    for marker, code in _CHANNEL_MARKERS:
        if s and marker in s:
            return code
    return None


def _resolve_channel(channel_chs: str | None, remark: str | None) -> str | None:
    base = _match_channel(channel_chs)
    if base == "ctrip" and remark:
        m = _ORDER_SOURCE_RE.search(remark)
        if m:
            sub = _match_channel(m.group(1))
            if sub:
                return sub
    return base


def _parse_date(v) -> date | None:
    if isinstance(v, date):
        return v
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def build_missing_order_payload(raw: dict) -> dict:
    """bypms staging raw_payload → 建单载荷（纯函数，便于单测）。

    口径与 ota-sync build_new_order 一致：actual=priceFang(房费=开票金额)、
    到手=priceClean|pricePaid、佣金率反推、渠道=channelChs+备注订单来源纠正、
    未识别渠道兜底 offline 并把原渠道写进备注；amount>1 拆 N 间均摊(首间吸余数)、
    room 不排(待排房)。
    """
    fang = _dec(raw.get("priceFang"))
    clean = _dec(raw.get("priceClean"))
    if clean is None:
        clean = _dec(raw.get("pricePaid"))
    rate = derive_ota_commission_rate(fang, clean) or Decimal("0")

    raw_channel = raw.get("channelChs")
    channel = _resolve_channel(raw_channel, raw.get("infoRemark"))
    channel_fallback = channel is None
    if channel_fallback:
        channel = "offline"

    ci, co = _parse_date(raw.get("checkIn")), _parse_date(raw.get("checkOut"))
    try:
        room_count = max(1, int(float(raw.get("amount") or 1)))
    except (TypeError, ValueError):
        room_count = 1

    total = (fang or Decimal("0")).quantize(_TWO, rounding=ROUND_HALF_UP)
    per = (total / room_count).quantize(_TWO, rounding=ROUND_HALF_UP)
    per_room = [total - per * (room_count - 1)] + [per] * (room_count - 1)
    rooms = [{
        "actual_price": rp,
        "daily_prices": compute_daily_prices(ci, co, rp) if ci and co else {},
    } for rp in per_room]

    pid = str(raw.get("channelOrderId") or "").strip()
    phone = raw.get("infoPhone")
    note_parts = ["对账补建（bypms 原始）。"]
    if channel_fallback and raw_channel:
        note_parts.append(f"原渠道：{raw_channel}（未识别，暂记线下）。")
    note_parts.append(f"客人联系方式：{phone or '未提供'}。")
    if raw.get("infoRemark"):
        note_parts.append(f"备注：{raw['infoRemark']}。")
    note_parts.append(f"平台单号：{pid}。")

    from app.core.free_room import is_free_room_type
    ota_room_type = raw.get("unitName") or raw.get("room_type")

    return {
        "platform_order_id": pid or None,
        "guest_name": raw.get("infoRealname") or "客人",
        "guest_phone": phone or None,
        "channel": channel,
        "order_status": _STATE_STATUS.get((raw.get("state") or "").strip(), "pending_confirm"),
        "check_in": ci,
        "check_out": co,
        "actual_price": fang,
        "commission_rate": rate,
        "notes": "".join(note_parts),
        "rooms": rooms,
        "metadata": {
            "source": "reconciliation-manual",
            "ota_platform": "bypms",
            "ota_order_id": pid or None,
            "ota_owner_revenue": str(clean) if clean is not None else None,
            "bypms_state": raw.get("state"),
            "ota_room_type": ota_room_type,
            "ota_free_room": is_free_room_type(ota_room_type),
        },
    }
