"""CD→Glue 只读契约视图 v_glue_deploy_logs 的契约测试（P2-6 的 CD 自有部分）。

背景：P2-6 原建议「共用 schema_version 表」违反「CD 不碰 CI 表」原则，已按用户指示不做；
而 CREATE TABLE IF NOT EXISTS「不补列」的隐患，已由 database.py 的幂等逐列 ALTER 迁移
（_ensure_cd_tables / _ensure_mysql_migrations）闭环，无需再引入版本号表。

本测试只锁定 CD 自有的对外契约面：v_glue_deploy_logs 视图列。该视图是 Devops-Glue
只读读取 CD 部署记录的唯一通道，SQLite 与 MySQL 两个驱动必须暴露完全一致的列集合；
未来 CD 内部重命名 / 拆表都应在此视图内吸收，保证 Glue 侧契约稳定。

运行（项目根）:
    python -m pytest backend/tests/test_schema_contract.py -q
"""

import os
import re
import sqlite3
import sys
import tempfile
import unittest

# 允许直接运行时找到 backend 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.config import settings

# 测试一律用临时 SQLite，忽略 .env 中可能配置的 MySQL（必须在导入 Database 前设置）
settings.db_driver = "sqlite"

from backend.database import Database  # noqa: E402

# CD→Glue 契约列（与 database.py _ensure_cd_tables 及 database/init_mysql.sql 保持一致）
GLUE_DEPLOY_LOG_COLUMNS = [
    "id",
    "project",
    "tag",
    "image",
    "deploy_type",
    "target",
    "status",
    "triggered_by",
    "deploy_note",
    "created_at",
]


def _extract_mysql_view_columns(sql_path: str):
    """从 init_mysql.sql 提取 v_glue_deploy_logs 视图的 SELECT 列名列表。

    找不到视图定义时返回 None（由测试判失败），不做任何默认值兜底。
    """
    text = open(sql_path, encoding="utf-8").read()
    m = re.search(
        r"CREATE OR REPLACE VIEW v_glue_deploy_logs AS\s*SELECT\s+(.*?)\s+FROM\s+cd_deploy_logs",
        text,
        re.S,
    )
    if not m:
        return None
    return [c.strip() for c in m.group(1).split(",") if c.strip()]


class GlueViewContractTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "test_contract.db")
        # Database 启动校验要求与 Devops-Glue 共享库（ci_pipeline_artifacts 为标记表）
        raw = sqlite3.connect(self.db_path)
        raw.execute("CREATE TABLE IF NOT EXISTS ci_pipeline_artifacts (id INTEGER PRIMARY KEY)")
        raw.commit()
        raw.close()

        # 每个临时库都是「全新库」，强制重新建表（类变量在测试间共享）
        Database._tables_ensured = False
        self.db = Database(self.db_path)
        with self.db.conn() as conn:
            conn.execute("SELECT 1")
        Database._tables_ensured = True

    def tearDown(self):
        self._tmp.cleanup()

    def test_sqlite_view_exposes_contract_columns(self):
        # 真实建库后，v_glue_deploy_logs 暴露的列必须与契约完全一致（含顺序）
        with self.db.conn() as conn:
            cur = conn.execute("SELECT * FROM v_glue_deploy_logs LIMIT 0")
            cols = [d[0] for d in cur.description]
        self.assertEqual(cols, GLUE_DEPLOY_LOG_COLUMNS)

    def test_mysql_view_matches_sqlite_contract(self):
        # MySQL 驱动（init_mysql.sql）暴露的列必须与 SQLite 契约一致，
        # 防止两个驱动对 Glue 暴露的契约发生漂移。
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        mysql_cols = _extract_mysql_view_columns(os.path.join(root, "database", "init_mysql.sql"))
        self.assertEqual(mysql_cols, GLUE_DEPLOY_LOG_COLUMNS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
