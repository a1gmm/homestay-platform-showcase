"""慧享家 AES 加密 —— 下码请求 password 字段必用。

算法（与慧享家文档对齐，文档样本实测通过 2026-06-13，见 plan §6.10）：

    key = MD5(tokenId).hexdigest().upper()[8:24]   # 16 bytes → AES-128
    iv  = key
    AES/CBC/PKCS5Padding，输出 Base64

⚠️ landmine：tokenId 内含真换行符 \n，参与 MD5 时按原字节算，
   禁止 strip/replace —— 去掉换行会算出完全不同的 key 与密文。
"""
import hashlib
from base64 import b64encode

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

_AES_BLOCK_BITS = 128  # AES block size for PKCS7 padding


def _derive_key(token_id: str) -> bytes:
    """key = MD5(tokenId) 大写 hex 的 [8:24] 这 16 字节。tokenId 原样参与（含 \n）。"""
    md5_hex = hashlib.md5(token_id.encode()).hexdigest().upper()
    return md5_hex[8:24].encode()


def aes_encrypt(token_id: str, plaintext: str) -> str:
    """用 tokenId 派生的 key（IV=key）AES-128/CBC/PKCS5 加密明文，返回 Base64。"""
    key = _derive_key(token_id)
    iv = key
    padder = PKCS7(_AES_BLOCK_BITS).padder()
    data = padder.update(plaintext.encode()) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    return b64encode(encryptor.update(data) + encryptor.finalize()).decode()
