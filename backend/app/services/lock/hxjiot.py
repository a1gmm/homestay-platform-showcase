"""HxjiotProvider —— 慧享家「公寓 APP 开放接口」实现（见 plan §6）。

协议：HTTP POST 单端点，请求体 {msgId, method, tokenId, data}，
响应体 {msgId, method, resultCode, reason, data}，resultCode=0 成功。
password 字段走 AES（见 aes.py），鉴权 tokenId 7 天有效需缓存（剩 2 天刷新）。

依赖注入（为可测 + 可替换）：
  transport      async(url, headers, payload) -> dict   真实现走 httpx；测试注入假的
  msgid_factory  () -> int                              我方生成（默认毫秒时间戳）
  clock          () -> float                            epoch 秒，token 过期判断用（默认 time.time）
  token_provider async() -> str | None                 注入则直接用之（测试/特殊场景）；
                                                        默认 None → 走内部 _login + 缓存
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import datetime, timezone
from typing import Awaitable, Callable

import httpx

from app.services.lock.aes import aes_encrypt
from app.services.lock.types import IssuedCode, IssueOutcome, VendorKey, VendorRoom
from app.services.lock.util import gen_numeric_password

_LOCK_STATE_ONLINE = 3          # lockState: 3 在线 / 4 离线
_ROOM_LIST_PAGE_SIZE = 50       # apartmentRoomList 单页上限

# Content-Version 必须 1.1：apartmentAddPasswordKey/TenantRenewRoom/CallBackUrlSet 等
# 是 V1.1 接口，1.0 下会报「服务能力xxx找不到」（2026-06-14 实测+慧享家确认）。
_HEADERS = {
    "Content-Type": "text/json;charset=utf-8",
    "Content-Version": "1.1",
}

# tokenId 7 天有效；剩这么多秒内就提前刷新（文档建议 5 天换一次，留足余量）
_REFRESH_MARGIN_SECONDS = 2 * 24 * 3600

# 慧享家错误码（见 plan §6.4）
_ERR_LOCK_OFFLINE = 500010      # 门锁未联网 → 受理待重试
_ERR_VENDOR_TIMEOUT = 406       # 服务端往网关/锁推码超时（实测瞬时，重试常成功）→ 受理待重试
_ERR_PASSWORD_TAKEN = 500009    # 密码已被添加（不同手机号不能同密码）→ 换码重试
_ERR_TOKEN_EXPIRED = 304        # 登录信息过期（同账号被别处登录顶掉）→ 清缓存重登重试一次
                                # 见 lock_code_304_token_expired：master_code/注册等 worker 用同一
                                # HXJIOT_ACCOUNT_NAME 登录会顶掉 app worker 缓存的 tokenId。

_PASSWORD_COLLISION_RETRIES = 5


class HxjiotError(Exception):
    """慧享家返回 resultCode != 0 时抛出。携带原始错误码与 reason。"""

    def __init__(self, code: int, reason: str, method: str | None = None):
        self.code = code
        self.reason = reason
        self.method = method
        super().__init__(f"[{code}] {reason}" + (f" (method={method})" if method else ""))


class HxjiotProvider:
    def __init__(
        self,
        base_url: str,
        account_name: str,
        password: str,
        transport: Callable[[str, dict, dict], Awaitable[dict]],
        msgid_factory: Callable[[], int],
        token_provider: Callable[[], Awaitable[str]] | None = None,
        clock: Callable[[], float] | None = None,
    ):
        self._base_url = base_url
        self._account_name = account_name
        self._password = password
        self._transport = transport
        self._msgid_factory = msgid_factory
        self._token_provider = token_provider
        self._clock = clock or time.time
        self._cached_token: str | None = None
        self._token_expiry_epoch: float = 0.0
        # 并发下码首触时用双检锁串行化登录，N 间房只打一次 apartmentLogin。
        self._login_lock = asyncio.Lock()

    # ---- RPC ----
    async def _call(
        self, method: str, data: dict, with_token: bool = True, token: str | None = None
    ) -> dict:
        """组装信封 → 发送 → resultCode 校验。成功返回 data，失败抛 HxjiotError。

        with_token=False 用于 apartmentLogin（登录本身不带 tokenId）。
        token 显式传入时用之（issue_code 需保证 AES 加密与请求用同一 tokenId）。
        """
        payload: dict = {"msgId": self._msgid_factory(), "method": method, "data": data}
        if with_token:
            payload["tokenId"] = token if token is not None else await self._resolve_token()
        resp = await self._transport(self._base_url, dict(_HEADERS), payload)
        if resp.get("resultCode") != 0:
            raise HxjiotError(resp.get("resultCode"), resp.get("reason", ""), method)
        return resp.get("data")

    async def _request(
        self, method: str, data_factory: Callable[[str], dict]
    ) -> dict:
        """带 token 生命周期管理的请求：resolve token → data_factory(token) → _call。

        收到 304（旧 token 被同账号别处登录顶失效）时：强制重登拿新 token →
        用新 token 重建 data（issue_code 的 AES 密码必须跟着换成新 token 重签）→ 重试一次。
        仅重试一次：重登后仍 304 就抛出，避免多 worker 互顶时无限重登。
        """
        token = await self._resolve_token()
        try:
            return await self._call(method, data_factory(token), token=token)
        except HxjiotError as exc:
            if exc.code != _ERR_TOKEN_EXPIRED:
                raise
            token = await self._force_relogin(token)
            return await self._call(method, data_factory(token), token=token)

    # ---- 下码 ----
    async def issue_code(
        self,
        vendor_room_id: str,
        phone: str,
        start_at: datetime,
        end_at: datetime,
        key_name: str,
        password: str | None = None,
    ) -> IssuedCode:
        """下一把客人/保洁码。AES 加密 password（与请求同一 tokenId），
        秒级时间戳，碰撞自动换码重试，锁离线→QUEUED，其余失败→FAILED（不抛异常）。
        """
        last_error: HxjiotError | None = None

        for _ in range(_PASSWORD_COLLISION_RETRIES):
            plaintext = password or gen_numeric_password()
            # password 的 AES 加密必须与请求 tokenId 用同一 token；304 重登后 _request
            # 会用新 token 重新调用本工厂，保证重试时 tokenId 与密文一致（否则解密失配）。
            def _data(token: str, _pt: str = plaintext) -> dict:
                return {
                    "roomId": vendor_room_id,
                    "phoneNo": phone,
                    "keyName": key_name,
                    "beginTime": int(start_at.timestamp()),   # 秒级 Unix 戳（tz-aware → 绝对）
                    "endTime": int(end_at.timestamp()),
                    "password": aes_encrypt(token, _pt),
                }
            try:
                resp = await self._request("apartmentAddPasswordKey", _data)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                # 客户端侧超时/网络抖动（生产服务端推码慢，常态）→ 受理待重试，绝不丢码
                return IssuedCode(outcome=IssueOutcome.QUEUED, password=plaintext, error=str(exc))
            except HxjiotError as exc:
                last_error = exc
                # 自动生成的密码撞了 → 换码重试；调用方指定的密码撞了不重试
                if exc.code == _ERR_PASSWORD_TAKEN and password is None:
                    continue
                if exc.code in (_ERR_LOCK_OFFLINE, _ERR_VENDOR_TIMEOUT):
                    return IssuedCode(
                        outcome=IssueOutcome.QUEUED, password=plaintext, error=str(exc)
                    )
                return IssuedCode(outcome=IssueOutcome.FAILED, password=plaintext, error=str(exc))

            return IssuedCode(
                outcome=IssueOutcome.GRANTED,
                password=plaintext,
                vendor_key_id=resp.get("keyId"),
                vendor_lock_key_id=resp.get("lockKeyId"),
                vendor_key_group_id=resp.get("keyGroupId"),
            )

        # 碰撞重试耗尽
        return IssuedCode(
            outcome=IssueOutcome.FAILED,
            password=None,
            error=str(last_error) if last_error else "password collision retries exhausted",
        )

    async def _call_ok(self, method: str, data: dict) -> bool:
        """布尔型操作：成功 True，厂商返错/超时 False（不抛异常）。
        经 _request → 304(token 被顶失效) 也会重登重试一次，删码/续期不因被顶而硬失败。"""
        try:
            await self._request(method, lambda _token: data)
            return True
        except (HxjiotError, httpx.TimeoutException, httpx.TransportError):
            return False

    async def revoke_code(self, vendor_key_id: str) -> bool:
        # deletedAuth=2：需联网下发删除（默认）
        return await self._call_ok("apartmentDelKey", {"keyId": vendor_key_id, "deletedAuth": 2})

    async def revoke_all(self, vendor_room_id: str) -> bool:
        return await self._call_ok("apartmentRoomCheckOut", {"roomId": vendor_room_id})

    async def renew(self, vendor_room_id: str, phone: str, end_at: datetime) -> bool:
        return await self._call_ok(
            "apartmentTenantRenewRoom",
            {"roomId": vendor_room_id, "phoneNo": phone, "endTime": int(end_at.timestamp())},
        )

    async def set_webhook(self, callback_url: str) -> bool:
        return await self._call_ok("apartmentCallBackUrlSet", {"callbackurl": callback_url})

    async def list_rooms(self) -> list[VendorRoom]:
        """分页拉全部房间，映射成 VendorRoom（供 lock_devices 入库）。"""
        rooms: list[VendorRoom] = []
        start = 0
        while True:
            page_start = start
            data = await self._request(
                "apartmentRoomList",
                lambda _token, s=page_start: {"startNum": s, "getNum": _ROOM_LIST_PAGE_SIZE},
            )
            page = data.get("list", []) or []
            for r in page:
                rooms.append(
                    VendorRoom(
                        vendor_room_id=r["roomId"],
                        name=r.get("roomName", ""),
                        online=r.get("lockState") == _LOCK_STATE_ONLINE,
                        battery=r.get("electricNum"),
                        lock_mac=r.get("lockMac"),
                        room_state=r.get("roomState"),
                    )
                )
            start += len(page)
            if not page or len(rooms) >= data.get("listSum", len(rooms)):
                break
        return rooms

    async def list_keys(self, vendor_room_id: str) -> list[VendorKey]:
        """按 roomId 拉云端钥匙列表（apartmentKeyList），映射成 VendorKey。

        云端视角，供重推前旁证「码已下发生效」（key_state==3）。分页同 list_rooms。
        """
        keys: list[VendorKey] = []
        start = 0
        while True:
            page_start = start
            data = await self._request(
                "apartmentKeyList",
                lambda _token, s=page_start: {
                    "roomId": vendor_room_id, "startNum": s, "getNum": _ROOM_LIST_PAGE_SIZE,
                },
            )
            page = data.get("list", []) or []
            for r in page:
                end = r.get("endTime")
                keys.append(
                    VendorKey(
                        phone_no=r.get("phoneNo"),
                        key_state=r.get("keyState"),
                        end_at=(datetime.fromtimestamp(int(end), tz=timezone.utc)
                                if isinstance(end, (int, float)) else None),
                        vendor_key_id=r.get("keyId"),
                    )
                )
            start += len(page)
            if not page or len(keys) >= data.get("listSum", len(keys)):
                break
        return keys

    # ---- 鉴权 / token 缓存 ----
    async def _resolve_token(self) -> str:
        if self._token_provider is not None:
            return await self._token_provider()
        return await self._get_token()

    async def _get_token(self) -> str:
        """返回有效 tokenId：缓存未临期则复用，否则重新登录。

        双检锁：并发下码时若缓存为空，只让第一个协程走 _login，其余等锁后命中缓存，
        避免 N 间房各打一次 apartmentLogin（既慢又可能触发厂商风控）。
        """
        if self._cached_token and (self._token_expiry_epoch - self._clock()) > _REFRESH_MARGIN_SECONDS:
            return self._cached_token
        async with self._login_lock:
            # 等锁期间可能已有别的协程登录好了 → 再查一次缓存
            if self._cached_token and (self._token_expiry_epoch - self._clock()) > _REFRESH_MARGIN_SECONDS:
                return self._cached_token
            return await self._login()

    async def _force_relogin(self, stale_token: str) -> str:
        """token 被外部会话顶失效(304)时：串行重登拿新 token。

        _get_token 只能按临期判缓存，无法预知 token 被别处顶掉；故收到 304 后由此强制重登。
        复用 _login_lock 双检：并发 N 间房同时被顶时只重登一次——先抢到锁的重登，其余等锁后
        发现 _cached_token 已换新（≠ 自己那个失效 token）即直接复用，不再各自重登互顶。
        注入 token_provider（测试/特殊场景）时无内部登录可走，退回再取一次交由 provider 决定。
        """
        if self._token_provider is not None:
            return await self._token_provider()
        async with self._login_lock:
            # 等锁期间可能已有别的协程重登好了（token 变了且非那个失效 token）→ 直接复用
            if self._cached_token is not None and self._cached_token != stale_token:
                return self._cached_token
            return await self._login()

    async def _login(self) -> str:
        """apartmentLogin → 缓存 tokenId 及绝对过期时间。password 取 MD5 大写。"""
        data = {
            "accountName": self._account_name,
            "password": hashlib.md5(self._password.encode()).hexdigest().upper(),
        }
        resp = await self._call("apartmentLogin", data, with_token=False)
        self._cached_token = resp["tokenId"]
        self._token_expiry_epoch = self._clock() + resp["expireTime"]
        return self._cached_token
