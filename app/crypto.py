"""敏感字段加密 — password / ssh_key 入库前加密，出库后解密。

加密格式: enc:<base64(ciphertext)>
"""

import base64
from pathlib import Path
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from app.config import settings

# ── 密钥管理 ──

# 优先从环境变量读取 SECRET_KEY；否则尝试从 .cd_secret_key 文件读取；
# 都不存在则生成新密钥并持久化到 .cd_secret_key（与数据库同目录）


def _get_secret_key() -> bytes:
    raw = settings.secret_key.strip()
    if raw:
        # 用户已配置：直接使用
        return _derive_key(raw)

    # 尝试从文件读取
    key_file = _key_file_path()
    if key_file.exists():
        stored = key_file.read_text().strip()
        if stored:
            return _derive_key(stored)

    # 生成新密钥并保存
    new_key = Fernet.generate_key().decode()
    key_file.write_text(new_key, encoding="utf-8")
    print(f"[WARN] SECRET_KEY 未配置，已自动生成并保存到 {key_file}")
    print(f"[WARN] 请妥善保管该文件，丢失后将无法解密已有数据。")
    return _derive_key(new_key)


def _key_file_path() -> Path:
    if settings.db_driver == "sqlite" and settings.db_path:
        return Path(settings.db_path).parent / ".cd_secret_key"
    return Path(__file__).parent.parent / ".cd_secret_key"


def _derive_key(raw: str) -> bytes:
    """将任意字符串派生为 32 字节 base64url Fernet 密钥。"""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"cd-service-v1-salt",
        iterations=100000,
    )
    return base64.urlsafe_b64encode(kdf.derive(raw.encode("utf-8")))


_fernet = Fernet(_get_secret_key())

# ── 公开 API ──

ENCRYPT_PREFIX = "enc:"


def encrypt(value: str) -> str:
    """加密字符串，返回 enc:<base64> 格式。空值不加密。"""
    if not value:
        return value
    if value.startswith(ENCRYPT_PREFIX):
        return value  # 已加密，不再重复加密
    token = _fernet.encrypt(value.encode("utf-8"))
    return ENCRYPT_PREFIX + token.decode("utf-8")


def decrypt(value: str) -> str:
    """解密字符串。空值直接返回。"""
    if not value:
        return value
    token = value.removeprefix(ENCRYPT_PREFIX).encode("utf-8")
    return _fernet.decrypt(token).decode("utf-8")


def decrypt_server_row(row: dict) -> dict:
    """对服务器记录字典中的 password / ssh_key 字段就地解密。"""
    if "password" in row:
        row["password"] = decrypt(row.get("password") or "")
    if "ssh_key" in row:
        row["ssh_key"] = decrypt(row.get("ssh_key") or "")
    return row
