"""轻量级分布式锁 - 基于 cd_config 表(无新增表).

key_name 固定为 'cd_lock.<name>',value 为 '<owner>|<13位零填充epoch秒>'。
抢锁与续约为原子条件替换;无显式释放,依赖 TTL 过期失效,持锁实例每轮循环续约。
时间戳为定长字符串,字典序比较等价于数值比较(SQLite 与 MySQL 通用)。
"""

import logging
import os
import socket
import time
import uuid

logger = logging.getLogger(__name__)

# 进程实例标识:主机名 + pid + 随机段,同机多 worker / 跨机多副本均唯一
INSTANCE_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def acquire(db, name: str, ttl_seconds: int = 90) -> bool:
    """尝试抢锁或续约(锁属于本实例、已过期或空闲时成功)。他人持锁或数据库异常均返回 False。"""
    now = int(time.time())
    now_padded = f"{now:013d}"
    key = f"cd_lock.{name}"
    value = f"{INSTANCE_ID}|{now_padded}"
    try:
        with db.conn() as conn:
            row = conn.execute("SELECT value FROM cd_config WHERE key_name=?", (key,)).fetchone()
            if row is None:
                try:
                    conn.execute("INSERT INTO cd_config (key_name, value) VALUES (?,?)", (key, value))
                    return True
                except Exception:
                    # 主键冲突:另一实例恰好抢先插入
                    return False
            old = row["value"] or ""
            old_owner, _, old_ts = old.partition("|")
            is_mine = old_owner == INSTANCE_ID
            # 格式非法视为已过期,允许接管
            is_stale = (not old_ts.isdigit()) or (now - int(old_ts)) >= ttl_seconds
            if not (is_mine or is_stale):
                return False
            # 仅当 value 仍为旧值时写入,并发下只有一个实例成功
            cur = conn.execute(
                "UPDATE cd_config SET value=? WHERE key_name=? AND value=?",
                (value, key, old),
            )
            return (getattr(cur, "rowcount", 0) or 0) > 0
    except Exception:
        logger.exception("dlock acquire failed for %s", name)
        return False


def release(db, name: str) -> None:
    """主动释放锁,仅当锁属于本实例."""
    key = f"cd_lock.{name}"
    try:
        with db.conn() as conn:
            conn.execute("DELETE FROM cd_config WHERE key_name=? AND value LIKE ?", (key, f"{INSTANCE_ID}|%"))
    except Exception:
        logger.exception("dlock release failed for %s", name)
