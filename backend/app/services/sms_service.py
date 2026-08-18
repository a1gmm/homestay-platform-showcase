"""
SMS sending service.

Two modes:
- "dev": logs the OTP to stdout, does not actually send. Safe for local/staging.
- "aliyun": sends via Aliyun DySmsApi. Requires SMS_* env vars.

The OTP lifecycle (generate, store, verify, rate limit) lives in
services/otp_service.py and uses Redis as storage.
"""
import asyncio
import logging
import json
from typing import Optional

from app.core.config import settings

logger = logging.getLogger(__name__)


async def send_otp_sms(phone: str, code: str) -> None:
    """
    Send an OTP SMS to the given phone.
    Raises on failure so the API can return 500 to the caller.
    """
    provider = (settings.SMS_PROVIDER or "dev").lower()

    if provider == "dev":
        # Dev stub: print OTP so you can log in without a real SMS account.
        logger.warning("[SMS-DEV] phone=%s code=%s (not sent, provider=dev)", phone, code)
        return

    if provider == "aliyun":
        await _send_aliyun(phone, code)
        return

    raise RuntimeError(f"Unknown SMS_PROVIDER: {provider}")


async def _send_aliyun(phone: str, code: str) -> None:
    """
    Send via Aliyun DySmsApi. Lazy-import so dev environments without the
    SDK installed don't break. Install with:
        pip install alibabacloud_dysmsapi20170525
    """
    try:
        from alibabacloud_dysmsapi20170525.client import Client as DysmsClient
        from alibabacloud_dysmsapi20170525 import models as dysms_models
        from alibabacloud_tea_openapi import models as open_api_models
    except ImportError as e:
        raise RuntimeError(
            "Aliyun SMS SDK not installed. Run: pip install alibabacloud_dysmsapi20170525"
        ) from e

    if not (settings.SMS_ACCESS_KEY_ID and settings.SMS_ACCESS_KEY_SECRET
            and settings.SMS_SIGN_NAME and settings.SMS_OTP_TEMPLATE_CODE):
        raise RuntimeError("Aliyun SMS config incomplete (SMS_ACCESS_KEY_ID/SECRET/SIGN_NAME/TEMPLATE_CODE)")

    config = open_api_models.Config(
        access_key_id=settings.SMS_ACCESS_KEY_ID,
        access_key_secret=settings.SMS_ACCESS_KEY_SECRET,
    )
    config.endpoint = "dysmsapi.aliyuncs.com"
    client = DysmsClient(config)

    req = dysms_models.SendSmsRequest(
        phone_numbers=phone,
        sign_name=settings.SMS_SIGN_NAME,
        template_code=settings.SMS_OTP_TEMPLATE_CODE,
        template_param=json.dumps({"code": code}),
    )
    # client.send_sms 是同步阻塞 HTTP 调用：放线程池 + 超时，别卡事件循环（批5）。
    resp = await asyncio.wait_for(asyncio.to_thread(client.send_sms, req), timeout=15)
    body = resp.body
    if body.code != "OK":
        raise RuntimeError(f"Aliyun SMS failed: {body.code} {body.message}")
    logger.info("[SMS-ALIYUN] sent phone=%s biz_id=%s", phone, body.biz_id)
