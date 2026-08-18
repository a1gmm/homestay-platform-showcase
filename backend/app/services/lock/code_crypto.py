"""门锁码静态加密（plan §8.2）—— Fernet at-rest。

password 列存 Fernet 密文，DB 泄露不泄露住期内有效门锁码。
key 优先取 settings.LOCK_CODE_FERNET_KEY；空则从 JWT_SECRET_KEY 确定性派生
（base64.urlsafe_b64encode(sha256(JWT_SECRET_KEY).digest())），无需额外配置即可启用。
decrypt 对非密文（Phase 1 历史明文老码）做兼容：原样返回，不抛。
"""
import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

logger = logging.getLogger(__name__)


def _fernet() -> Fernet:
    key = settings.LOCK_CODE_FERNET_KEY
    if not key:
        key = base64.urlsafe_b64encode(
            hashlib.sha256(settings.JWT_SECRET_KEY.encode()).digest()
        ).decode()
    return Fernet(key)


def encrypt_code(plaintext: str) -> str:
    """明文门锁码 → Fernet 密文（写入 password 列前调用）。"""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_code(token: str) -> str:
    """Fernet 密文 → 明文。非密文（历史明文老码）原样返回（§8.2 迁移兼容）。"""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        # 历史明文老码 → 原样返回（§8.2 迁移兼容）。但同一分支也会命中
        # key 轮换 / 密文损坏 —— 那才是哑炮（密文被当门锁码下发）。记一条 warning
        # 让运维可见；绝不记 token 值本身（可能是明文老码，记了即泄密）。
        logger.warning(
            "door code decrypt failed (legacy plaintext, key rotation, or corruption); "
            "passing value through unchanged"
        )
        return token
