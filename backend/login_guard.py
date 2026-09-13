"""登录失败限流 — 与 Devops-Glue（CI）共享 cache 表的同一把锁。

两边共享 admin_users 密码表，若各自计数则攻击者可绕过 CI 的 5 次锁定只打 CD 入口。
因此本模块严格复刻 Glue AdminAuthService 的键格式与窗口：
  表/键: cache.cache_key = 'login_fail_' + md5(ip + ':' + lower(username))
  值:    连续失败次数；expires_at = 窗口到期 unix 时间戳
  策略:  5 次失败锁 15 分钟，登录成功清零

DB 异常一律 fail-open（与 Glue 一致，保可用），仅记日志。
"""

import hashlib
import ipaddress
import logging
import time

from fastapi import Request

from backend.config import settings
from backend.database import Database

logger = logging.getLogger(__name__)

CACHE_TABLE = "cache"
KEY_PREFIX = "login_fail_"
MAX_ATTEMPTS = 5
LOCK_SECONDS = 900  # 15 分钟


def client_ip(request: Request) -> str:
    """推导客户端真实 IP，语义必须与 Glue 修复后的 clientIp 完全一致：

    - TRUSTED_PROXY_HOPS=0（默认，安全默认）：直接用对端地址，不信任任何代理头；
    - hops>0 且对端是可信代理（loopback/私网/链路本地）时，从 X-Forwarded-For
      **右端**取倒数第 hops 个地址（攻击者无法用自己加的左端假地址冒充）；
    - 任何一步不合法都回退对端地址。
    """
    peer = request.client.host if request.client else ""
    hops = settings.trusted_proxy_hops
    if not peer or hops <= 0:
        return peer or "0.0.0.0"
    try:
        peer_addr = ipaddress.ip_address(peer)
    except ValueError:
        return peer
    # 只有对端本身是本机/内网代理时才考虑 XFF（直连客户端发来的 XFF 不可信）
    if not (peer_addr.is_loopback or peer_addr.is_private or peer_addr.is_link_local):
        return peer

    xff = request.headers.get("x-forwarded-for", "")
    parts = [p.strip() for p in xff.split(",") if p.strip()]
    if len(parts) < hops:
        return peer
    candidate = parts[len(parts) - hops]
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return peer
    return candidate


def _key(ip: str, username: str) -> str:
    return KEY_PREFIX + hashlib.md5(f"{ip}:{(username or '').strip().lower()}".encode("utf-8")).hexdigest()


def _upsert_sql() -> str:
    if settings.db_driver == "mysql":
        return (
            f"INSERT INTO {CACHE_TABLE} (cache_key, value, expires_at) VALUES (?,?,?) "
            "ON DUPLICATE KEY UPDATE value=VALUES(value), expires_at=VALUES(expires_at)"
        )
    return (
        f"INSERT INTO {CACHE_TABLE} (cache_key, value, expires_at) VALUES (?,?,?) "
        "ON CONFLICT(cache_key) DO UPDATE SET value=excluded.value, expires_at=excluded.expires_at"
    )


def is_login_locked(db: Database, ip: str, username: str) -> bool:
    """是否已达锁定阈值（键存在且未过期且计数 >= 5）。"""
    try:
        with db.conn() as conn:
            row = conn.execute(
                f"SELECT value FROM {CACHE_TABLE} WHERE cache_key=? AND expires_at > ?",
                (_key(ip, username), int(time.time())),
            ).fetchone()
        if not row:
            return False
        return int(row["value"] or 0) >= MAX_ATTEMPTS
    except Exception:
        logger.warning("is_login_locked 查询失败，fail-open 放行", exc_info=True)
        return False


def record_login_failure(db: Database, ip: str, username: str) -> None:
    """失败计数 +1，并刷新 15 分钟窗口。"""
    try:
        key = _key(ip, username)
        now = int(time.time())
        with db.conn() as conn:
            row = conn.execute(
                f"SELECT value FROM {CACHE_TABLE} WHERE cache_key=? AND expires_at > ?",
                (key, now),
            ).fetchone()
            count = (int(row["value"]) if row and row["value"] is not None else 0) + 1
            conn.execute(_upsert_sql(), (key, str(count), now + LOCK_SECONDS))
    except Exception:
        logger.warning("record_login_failure 写入失败（忽略）", exc_info=True)


def clear_login_failure(db: Database, ip: str, username: str) -> None:
    """登录成功清除计数。"""
    try:
        with db.conn() as conn:
            conn.execute(f"DELETE FROM {CACHE_TABLE} WHERE cache_key=?", (_key(ip, username),))
    except Exception:
        logger.warning("clear_login_failure 删除失败（忽略）", exc_info=True)
