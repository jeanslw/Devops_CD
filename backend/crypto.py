"""敏感字段加密 - password / ssh_key 入库前加密,出库后解密.

加密格式: enc:<base64(ciphertext)>
"""

import base64
import hashlib
import logging
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from backend.config import BASE_DIR, settings

# ── 密钥管理 ──

# SECRET_KEY 读取优先级: 环境变量 > CD 项目根目录的 .cd_secret_key 文件 > 自动生成并写入文件

# 公开示例值视为未配置,强制使用随机密钥
_INSECURE_DEFAULT_KEYS = frozenset({"devops_cd_2026", "change_me_to_secret"})

# 兼容解密旧数据的固定盐
_LEGACY_SALT = b"cd-service-v1-salt"


def _resolve_secret() -> str:
    """返回派生 Fernet 密钥的原始秘密:env SECRET_KEY > 密钥文件 > 自动生成并写入文件."""
    raw = settings.secret_key.strip()
    if raw in _INSECURE_DEFAULT_KEYS:
        print("[WARN] SECRET_KEY 使用了公开的示例默认值，已忽略并改用随机密钥。")
        print("[WARN] 请在 .env 中配置随机 SECRET_KEY（openssl rand -base64 32），")
        print("[WARN]       否则已保存的服务器口令/私钥需要重新录入。")
        raw = ""
    if raw:
        return raw

    key_file = _key_file_path()
    if key_file.exists():
        try:
            stored = key_file.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as exc:
            # 读取失败时中止启动,不覆盖原文件
            raise RuntimeError(
                f"密钥文件 {key_file} 存在但读取失败（{exc}）。"
                "请检查文件权限与内容，或备份后删除该文件并配置 SECRET_KEY。"
            ) from exc
        if stored:
            return stored

    new_key = Fernet.generate_key().decode()
    try:
        key_file.write_text(new_key, encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(
            f"SECRET_KEY 未配置，且密钥文件 {key_file} 写入失败（容器只读 / 目录不可写）。"
            "必须在 .env 中设置随机 SECRET_KEY（openssl rand -base64 32）。"
            f" / SECRET_KEY is unset and the key file {key_file} cannot be written "
            "(read-only filesystem or unwritable directory). "
            "Set a random SECRET_KEY in .env (openssl rand -base64 32)."
        ) from exc
    print(f"[WARN] SECRET_KEY 未配置，已自动生成并保存到 {key_file}")
    print("[WARN] 请妥善保管该文件，丢失后将无法解密已有数据。")
    return new_key


def _get_secret_key() -> bytes:
    """派生主 Fernet 密钥(盐从 SECRET_KEY 派生)."""
    return _derive_key(_resolve_secret())


def _key_file_path() -> Path:
    """密钥文件位置:恒为 CD 项目根目录(BASE_DIR),与 DB_PATH 无关."""
    return BASE_DIR / ".cd_secret_key"


def _derive_salt(secret_raw: str) -> bytes:
    """从 SECRET_KEY 派生 PBKDF2 盐:无需文件写入,跨重启稳定,按部署唯一."""
    return hashlib.sha256(b"cd-service-pbkdf2-salt-v2:" + secret_raw.encode("utf-8")).digest()[:16]


def _derive_key(raw: str, salt: bytes | None = None) -> bytes:
    """将任意字符串派生为 32 字节 base64url Fernet 密钥.

    salt 缺省从 raw 派生;显式传 _LEGACY_SALT 用于兼容解密旧数据.
    """
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_derive_salt(raw) if salt is None else salt,
        iterations=100000,
    )
    return base64.urlsafe_b64encode(kdf.derive(raw.encode("utf-8")))


def _salt_file_paths() -> list[Path]:
    """盐文件候选位置:仅 CD 项目根目录(与密钥文件同目录)."""
    return [_key_file_path().with_name(".cd_secret_key.salt")]


def _read_salt(salt_file: Path) -> bytes | None:
    """读取单个盐文件;不存在 / 读取失败 / 为空 / 非法 base64 时返回 None."""
    try:
        stored = salt_file.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    if not stored:
        return None
    try:
        return base64.b64decode(stored)
    except ValueError:
        return None


def _stored_salts() -> list[bytes]:
    """汇总现存的盐文件内容(去重,只读)."""
    salts: list[bytes] = []
    for salt_file in _salt_file_paths():
        salt = _read_salt(salt_file)
        if salt and salt not in salts:
            salts.append(salt)
    return salts


_secret_raw = _resolve_secret()
# 主密钥:盐从 SECRET_KEY 派生
_fernet = Fernet(_derive_key(_secret_raw))
# 兼容密钥 1:历史固定盐,解密旧数据
_legacy_fernet = Fernet(_derive_key(_secret_raw, _LEGACY_SALT))
# 兼容密钥 2..n:盐文件存在才加载,解密旧数据
_salt_file_fernets = [Fernet(_derive_key(_secret_raw, salt)) for salt in _stored_salts()]

# ── 公开 API ──

ENCRYPT_PREFIX = "enc:"


def encrypt(value: str) -> str:
    """加密字符串,返回 enc:<base64> 格式.空值不加密."""
    if not value:
        return value
    if value.startswith(ENCRYPT_PREFIX):
        return value  # 已加密,不再重复加密
    token = _fernet.encrypt(value.encode("utf-8"))
    return ENCRYPT_PREFIX + token.decode("utf-8")


def decrypt(value: str) -> str:
    """解密字符串.

    - 空值直接返回;
    - 无 enc: 前缀的值按明文原样返回;
    - enc: 前缀时先按主密钥解,失败后依次回退兼容密钥(历史固定盐、盐文件盐);
    - 仍失败时返回空字符串并记录告警.
    """
    if not value:
        return value
    if not value.startswith(ENCRYPT_PREFIX):
        return value  # 明文历史数据,原样返回
    token = value[len(ENCRYPT_PREFIX) :].encode("utf-8")
    try:
        return _fernet.decrypt(token).decode("utf-8")
    except InvalidToken:
        # 旧数据:依次用兼容密钥重试
        for fallback in (_legacy_fernet, *_salt_file_fernets):
            try:
                return fallback.decrypt(token).decode("utf-8")
            except InvalidToken:
                continue
        # 所有密钥均失败:密钥已更换或密文被篡改
        logging.getLogger(__name__).warning("decrypt failed: SECRET_KEY 已更换或密文被篡改，该字段按空值处理")
        return ""
    except ValueError:
        # 非法 base64:密文损坏
        logging.getLogger(__name__).error("decrypt failed: 密文格式损坏（非法 base64），该字段按空值处理")
        return ""


def decrypt_server_row(row: dict) -> dict:
    """对服务器记录字典中的 password / ssh_key 字段就地解密."""
    if "password" in row:
        row["password"] = decrypt(row.get("password") or "")
    if "ssh_key" in row:
        row["ssh_key"] = decrypt(row.get("ssh_key") or "")
    return row
