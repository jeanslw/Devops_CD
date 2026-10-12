"""测试辅助：把临时共享库「启用 CD」——等价于运维在 Devops-Glue 后台点「集成服务 → 启用」。

1.6.0 起 CD 以 Devops-Glue 的 `integrated_services` 表为唯一启用开关（CI 是唯一写入方）：
  - 契约表 / 契约行缺失，或 enabled<>1 → 零 DDL + 入口统一 503 errors.cd_not_enabled；
  - enabled=1 + provision='auto'        → CD 自动建表（MySQL / SQLite 同理）；
  - enabled=1 + provision='manual'      → CD 零 DDL，只认运维预建表。
测试里的临时库默认没有这一行，所以「建库后显式启用」是必要步骤 —— 它同时让测试走真实闸门
（而不是绕过），想测「未启用 / manual」时只要不调用本辅助或改用 provision='manual'。

被 test_cd_deploy_gate.py / test_cd_schema_sources.py 之外的既有测试共用（审批、会话、镜像同步、
契约视图），因此单独成模块，避免每个文件各写一份契约 DDL。
"""

import sqlite3

# 与 Devops-Glue 的 sqlite_init.sql 同构（测试只需服务 name/enabled/provision 三列可用）
CONTRACT_DDL = (
    "CREATE TABLE IF NOT EXISTS integrated_services ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "service_name TEXT NOT NULL UNIQUE, "
    "enabled INTEGER NOT NULL DEFAULT 0, "
    "provision TEXT NOT NULL DEFAULT 'auto', "
    "api_token_id INTEGER DEFAULT NULL, "
    "updated_by TEXT DEFAULT '', "
    "created_at TEXT DEFAULT '', "
    "updated_at TEXT DEFAULT '')"
)


def set_cd_contract(
    db_path: str, enabled=1, provision: str = "auto", with_row: bool = True, create_table: bool = True
) -> None:
    """按需构造契约状态（未启用 / enabled=0 / manual 等），供闸门测试直接摆场景。"""
    conn = sqlite3.connect(db_path)
    try:
        if create_table:
            conn.execute(CONTRACT_DDL)
        if with_row:
            conn.execute(
                "INSERT OR REPLACE INTO integrated_services (service_name, enabled, provision) VALUES ('cd', ?, ?)",
                (enabled, provision),
            )
        else:
            conn.execute("DELETE FROM integrated_services")  # 清空 = 回到「契约行不存在」
        conn.commit()
    finally:
        conn.close()


def enable_cd(db_path: str, provision: str = "auto") -> None:
    """写入契约行 service_name='cd' + enabled=1（幂等，可重复调用）。"""
    set_cd_contract(db_path, enabled=1, provision=provision)
