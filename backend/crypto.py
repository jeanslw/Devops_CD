"""敏感字段加密 — password / ssh_key 入库前加密，出库后解密。

加密格式: enc:<base64(ciphertext)>
"""

import base64
import logging
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from backend.config import settings

# ── 密钥管理 ──

# 优先从环境变量读取 SECRET_KEY；否则尝试从 .cd_secret_key 文件读取；
# 都不存在则生成新密钥并持久化到 .cd_secret_key（与数据库同目录）


# 历史版本/配套仓库 .env(.example) 中出现过的公开示例值。所有照抄示例的部署共享同一把密钥，
# 一旦库泄露即可批量解密所有服务器口令/私钥，因此把它视为“未配置”强制走随机密钥。
_INSECURE_DEFAULT_KEYS = frozenset({"devops_cd_2026", "change_me_to_secret"})


def _resolve_secret() -> str:
    """返回派生 Fernet 密钥的原始秘密：env SECRET_KEY > 密钥文件 > 自动生成并落盘。"""
    raw = settings.secret_key.strip()
    if raw in _INSECURE_DEFAULT_KEYS:
        print("[WARN] SECRET_KEY 使用了公开的示例默认值，已忽略并改用随机密钥。")
        print("[WARN] 请在 .env 中配置随机 SECRET_KEY（openssl rand -base64 32），")
        print("[WARN]       否则已保存的服务器口令/私钥需要重新录入。")
        raw = ""
    if raw:
        return raw

    # 尝试从文件读取
    key_file = _key_file_path()
    if key_file.exists():
        stored = key_file.read_text().strip()
        if stored:
            return stored

    # 生成新密钥并保存
    new_key = Fernet.generate_key().decode()
    key_file.write_text(new_key, encoding="utf-8")
    print(f"[WARN] SECRET_KEY 未配置，已自动生成并保存到 {key_file}")
    print("[WARN] 请妥善保管该文件，丢失后将无法解密已有数据。")
    return new_key


def _get_secret_key() -> bytes:
    """派生主 Fernet 密钥（按部署随机盐）。保留此函数名供测试 / 外部引用。"""
    return _derive_key(_resolve_secret())


def _key_file_path() -> Path:
    if settings.db_driver == "sqlite" and settings.db_path:
        return Path(settings.db_path).parent / ".cd_secret_key"
    return Path(__file__).parent.parent / ".cd_secret_key"


# 历史固定盐：v1.5.x 之前所有部署共享的 PBKDF2 盐（可被预计算彩虹表）。仅保留用于
# 兼容解密旧数据；新加密改用下面按部署随机持久化的盐，消除跨部署共享盐风险。
_LEGACY_SALT = b"cd-service-v1-salt"


def _salt_file_path() -> Path:
    """随机盐持久化位置：与密钥文件同目录的 .cd_secret_key.salt。"""
    return _key_file_path().with_name(".cd_secret_key.salt")


def _get_or_create_salt() -> bytes:
    """按部署随机盐：首次生成并落盘，后续稳定复用。

    read_only 容器无法落盘时退回历史固定盐（跨重启稳定、与旧数据兼容）——
    绝不返回易失随机盐，否则每次启动派生密钥不同，已加密数据会全部失效。
    """
    salt_file = _salt_file_path()
    if salt_file.exists():
        stored = salt_file.read_text(encoding="utf-8").strip()
        if stored:
            try:
                return base64.b64decode(stored)
            except ValueError:
                pass
    salt = os.urandom(16)
    try:
        salt_file.write_text(base64.b64encode(salt).decode("ascii"), encoding="utf-8")
    except OSError:
        return _LEGACY_SALT
    return salt


def _derive_key(raw: str, salt: bytes | None = None) -> bytes:
    """将任意字符串派生为 32 字节 base64url Fernet 密钥。

    salt 缺省使用按部署随机盐；显式传 _LEGACY_SALT 用于兼容解密旧数据。
    """
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_get_or_create_salt() if salt is None else salt,
        iterations=100000,
    )
    return base64.urlsafe_b64encode(kdf.derive(raw.encode("utf-8")))


_secret_raw = _resolve_secret()
# 主密钥（按部署随机盐）+ 兼容密钥（历史固定盐，仅用于解密升级前的旧数据）
_fernet = Fernet(_derive_key(_secret_raw))
_legacy_fernet = Fernet(_derive_key(_secret_raw, _LEGACY_SALT))

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
    """解密字符串。

    - 空值直接返回；
    - 无 enc: 前缀的值视为历史明文 / 外部直写，原样返回（兼容旧数据）；
    - enc: 前缀时先按主密钥（随机盐）解，失败回退兼容密钥（历史固定盐）解旧数据；
    - 仍失败时降级返回空字符串并分级告警，避免 InvalidToken 让部署、连接测试、
      WebShell、文件上传全链路崩溃。
    """
    if not value:
        return value
    if not value.startswith(ENCRYPT_PREFIX):
        return value  # 明文历史数据，原样返回
    token = value[len(ENCRYPT_PREFIX) :].encode("utf-8")
    try:
        return _fernet.decrypt(token).decode("utf-8")
    except InvalidToken:
        try:
            # 升级前旧数据：用历史固定盐派生的兼容密钥再试一次
            return _legacy_fernet.decrypt(token).decode("utf-8")
        except InvalidToken:
            # 分级告警（WARN）：两把密钥都解不开 → 密钥已更换或密文被篡改
            logging.getLogger(__name__).warning("decrypt failed: SECRET_KEY 已更换或密文被篡改，该字段按空值处理")
            return ""
    except ValueError:
        # 分级告警（ERROR）：非法 base64 → 密文本身损坏（数据完整性），比密钥不匹配更严重
        logging.getLogger(__name__).error("decrypt failed: 密文格式损坏（非法 base64），该字段按空值处理")
        return ""


def decrypt_server_row(row: dict) -> dict:
    """对服务器记录字典中的 password / ssh_key 字段就地解密。"""
    if "password" in row:
        row["password"] = decrypt(row.get("password") or "")
    if "ssh_key" in row:
        row["ssh_key"] = decrypt(row.get("ssh_key") or "")
    return row
