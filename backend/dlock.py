"""轻量级分布式锁 — 基于本项目自有的 cd_config 表（无新增表）。

用途：多副本（K8s Deployment replicas>1）或多 worker（uvicorn --workers N）部署时，
防止后台线程（镜像同步 / 告警检测 / 定时发布调度器）在每个实例各跑一份、多头重复运行。
其他实例检测到锁被存活实例持有时，静默跳过本轮，不打日志刷屏。

机制：
- key_name 固定为 "cd_lock.<name>"，value 为 "<owner>|<13位零填充epoch秒>"；
- 抢锁/续约：UPDATE ... WHERE value=<旧值> 原子条件替换（仅一个实例能成功）；
  无行则 INSERT，主键冲突即他人抢先；
- 无显式释放：依赖 TTL 过期自动失效。持锁实例每轮循环续约（重新 acquire），
  崩溃后最多 TTL 秒被其他实例接管。时间戳为定长字符串，字典序比较即数值比较
  （SQLite 与 MySQL 双驱动通用，不依赖 CAST）。
"""

import logging
import os
import socket
import time
import uuid

logger = logging.getLogger(__name__)

# 进程实例标识：主机名 + pid + 随机段，同机多 worker / 跨机多副本均唯一
INSTANCE_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def acquire(db, name: str, ttl_seconds: int = 90) -> bool:
    """尝试抢锁或续约（锁已是本实例的、或已过期/空闲时成功）。返回是否持锁成功。

    失败（他人持锁）返回 False，调用方静默跳过本轮；数据库异常也返回 False
    （fail-closed：DB 不可用时本轮后台任务本来就无法工作）。
    """
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
                    # 主键冲突：另一实例恰好抢先插入
                    return False
            old = row["value"] or ""
            old_owner, _, old_ts = old.partition("|")
            is_mine = old_owner == INSTANCE_ID
            # 格式非法（无时间戳段，如被手工改动）视为已过期，允许接管——
            # 否则损坏的锁值会让所有实例永久静默跳过（锁卡死）
            is_stale = (not old_ts.isdigit()) or (now - int(old_ts)) >= ttl_seconds
            if not (is_mine or is_stale):
                return False
            # 原子条件替换：仅当 value 仍是我们读到的旧值时才写入（防两实例同时接管）
            cur = conn.execute(
                "UPDATE cd_config SET value=? WHERE key_name=? AND value=?",
                (value, key, old),
            )
            return (getattr(cur, "rowcount", 0) or 0) > 0
    except Exception:
        logger.exception("dlock acquire failed for %s", name)
        return False


def release(db, name: str) -> None:
    """主动释放锁（仅当锁属于本实例）。后台循环正常退出时调用，缩短接管等待。"""
    key = f"cd_lock.{name}"
    try:
        with db.conn() as conn:
            conn.execute("DELETE FROM cd_config WHERE key_name=? AND value LIKE ?", (key, f"{INSTANCE_ID}|%"))
    except Exception:
        logger.exception("dlock release failed for %s", name)
