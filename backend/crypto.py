"""敏感字段加密 — password / ssh_key 入库前加密，出库后解密。

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

# 优先从环境变量读取 SECRET_KEY；否则尝试从 CD 项目根目录的 .cd_secret_key 文件读取；
# 都不存在则生成新密钥并持久化到该文件。
# 落盘位置恒为 CD 项目根目录（BASE_DIR）：DB_PATH 指向的是与 Devops-Glue 共享的数据库
# 文件，可能位于第三方项目目录 —— CD 只在自身项目目录内读写运行时文件，绝不把密钥 /
# 盐文件写进 Devops-Glue 等项目目录（历史上 sqlite 模式会写进去，见 _key_file_path）。
# 注意：docker compose 部署为 read_only 容器，密钥文件写不出来 —— 该场景由
# env-check 预检强制要求在 .env 设置 SECRET_KEY（见 docker-compose.yml）。


# 历史版本/配套仓库 .env(.example) 中出现过的公开示例值。所有照抄示例的部署共享同一把密钥，
# 一旦库泄露即可批量解密所有服务器口令/私钥，因此把它视为“未配置”强制走随机密钥。
_INSECURE_DEFAULT_KEYS = frozenset({"devops_cd_2026", "change_me_to_secret"})

# 历史固定盐：v1.5.6 及以前所有部署共享的 PBKDF2 盐（可被预计算彩虹表）。仅保留用于
# 兼容解密旧数据；新加密改用从 SECRET_KEY 派生的盐，消除跨部署共享盐风险。
_LEGACY_SALT = b"cd-service-v1-salt"


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
    try:
        key_file.write_text(new_key, encoding="utf-8")
    except OSError as exc:
        # read_only 容器 / 目录不可写时密钥文件写不出来：给出可操作的修复指引，
        # 而不是让裸 OSError 栈在导入期直接崩溃。
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
    """派生主 Fernet 密钥（盐从 SECRET_KEY 派生）。保留此函数名供测试 / 外部引用。"""
    return _derive_key(_resolve_secret())


def _key_file_path() -> Path:
    """密钥文件位置：恒为 CD 项目根目录（BASE_DIR），不随 DB_PATH / 数据库模式变化。

    DB_PATH 只用于打开与 Devops-Glue 共享的数据库文件，绝不参与任何落盘路径推导：
    历史版本在 sqlite 模式下取 Path(DB_PATH).parent，会把 .cd_secret_key /
    .cd_secret_key.salt 写进共享库所在的 Glue 目录（跨项目污染）。本版起只认 CD 项目目录。
    """
    return BASE_DIR / ".cd_secret_key"


def _derive_salt(secret_raw: str) -> bytes:
    """从 SECRET_KEY 本身派生 PBKDF2 盐：无需任何文件写入（read_only 容器完全兼容）、
    跨重启稳定，且因 SECRET_KEY 按部署随机而天然按部署唯一。"""
    return hashlib.sha256(b"cd-service-pbkdf2-salt-v2:" + secret_raw.encode("utf-8")).digest()[:16]


def _derive_key(raw: str, salt: bytes | None = None) -> bytes:
    """将任意字符串派生为 32 字节 base64url Fernet 密钥。

    salt 缺省用 _derive_salt(raw) 从 SECRET_KEY 派生；显式传 _LEGACY_SALT
    得到与 v1.5.6 及以前一致的密钥，仅用于兼容解密旧数据。
    """
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_derive_salt(raw) if salt is None else salt,
        iterations=100000,
    )
    return base64.urlsafe_b64encode(kdf.derive(raw.encode("utf-8")))


def _salt_file_paths() -> list[Path]:
    """v1.5.7 可能落盘盐文件的候选位置：只查「与密钥文件同目录」= CD 项目根目录。

    v1.5.7 的盐文件与密钥文件同目录，而本版起密钥文件恒在 CD 项目根目录，因此这里也只认
    这一个位置 —— 不再回看 DB_PATH 目录：那是 Devops-Glue 的目录，CD 不读取、更不写入
    其他项目的文件（DB_PATH 只用于打开共享数据库）。
    """
    return [_key_file_path().with_name(".cd_secret_key.salt")]


def _read_salt(salt_file: Path) -> bytes | None:
    """读取单个落盘盐；文件不存在 / 为空 / 非法 base64 时返回 None。"""
    if not salt_file.exists():
        return None
    stored = salt_file.read_text(encoding="utf-8").strip()
    if not stored:
        return None
    try:
        return base64.b64decode(stored)
    except ValueError:
        return None


def _stored_salts() -> list[bytes]:
    """汇总现存的 v1.5.7 落盘盐（去重）。

    只读、绝不写入：read_only 容器里这些文件都不存在，行为与不读取完全一致；
    裸机 / 挂载可写卷的部署若曾用该盐加密凭据，仍可解出，避免静默丢数据。
    """
    salts: list[bytes] = []
    for salt_file in _salt_file_paths():
        salt = _read_salt(salt_file)
        if salt and salt not in salts:
            salts.append(salt)
    return salts


# v1.5.7 曾引入「按部署随机盐落盘 .cd_secret_key.salt」的形态，但该形态在 read_only 容器
# 下盐文件永远写不出来、静默退回历史固定盐，故本版放弃它作为**主密钥盐**：主密钥盐改为从
# SECRET_KEY 派生（见 _derive_salt），不再依赖任何文件写入。
# 且可写环境（裸机 / 挂载可写卷）确实会写出盐文件 —— 该文件若仍在 CD 项目根目录下，
# 就保留只读的兼容密钥（_salt_file_fernets）用于解密当时写入的数据；文件缺失即自动跳过。
_secret_raw = _resolve_secret()
# 主密钥（SECRET_KEY 派生盐）：加密与解密的首选
_fernet = Fernet(_derive_key(_secret_raw))
# 兼容密钥 1：历史固定盐，用于解密 v1.5.6 及以前的旧数据
_legacy_fernet = Fernet(_derive_key(_secret_raw, _LEGACY_SALT))
# 兼容密钥 2..n：v1.5.7 落盘盐（盐文件存在才有；只查 CD 项目根目录，DB_PATH 不参与）
_salt_file_fernets = [Fernet(_derive_key(_secret_raw, salt)) for salt in _stored_salts()]

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
    - enc: 前缀时先按主密钥（SECRET_KEY 派生盐）解，失败后依次回退兼容密钥：
      历史固定盐（v1.5.6 及以前）、v1.5.7 落盘盐（CD 项目根目录下仍有 .cd_secret_key.salt 时）；
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
        # 升级前旧数据：依次用兼容密钥重试（历史固定盐 → v1.5.7 落盘盐，可能有多把）
        for fallback in (_legacy_fernet, *_salt_file_fernets):
            try:
                return fallback.decrypt(token).decode("utf-8")
            except InvalidToken:
                continue
        # 分级告警（WARN）：所有密钥都解不开 → 密钥已更换或密文被篡改
        logging.getLogger(__name__).warning("decrypt failed: SECRET_KEY 已更换或密文被篡改，该字段按空值处理")
        return ""
    except ValueError:
        # 分级告警（ERROR）：非法 base64 → 密文本身损坏（数据完整性）
        logging.getLogger(__name__).error("decrypt failed: 密文格式损坏（非法 base64），该字段按空值处理")
        return ""


def decrypt_server_row(row: dict) -> dict:
    """对服务器记录字典中的 password / ssh_key 字段就地解密。"""
    if "password" in row:
        row["password"] = decrypt(row.get("password") or "")
    if "ssh_key" in row:
        row["ssh_key"] = decrypt(row.get("ssh_key") or "")
    return row
