"""部署记录只增不删（append-only）触发器回归（P1-5）。

cd_deploy_logs 是部署审计轨迹：禁止 DELETE 抹掉历史；运行期状态流转仍允许 UPDATE
（pending→running→终态，及恢复时的 interrupted→pending 等应用自身驱动的合法更新）。

用真实临时 SQLite 库验证 BEFORE DELETE 触发器生效，且 INSERT/UPDATE 不受影响、
触发器重复创建（幂等）不报错。

运行（项目根）:
    python -m pytest backend/tests/test_deploy_logs_append_only.py -q
"""

import os
import sqlite3
import sys
import tempfile
import unittest

# 允许直接运行时找到 backend 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# 测试一律用临时 SQLite，忽略 .env 中可能配置的 MySQL（必须在导入 Database 前设置）
from backend.config import settings

settings.db_driver = "sqlite"

from backend.database import Database  # noqa: E402


class DeployLogsAppendOnlyTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "test_append_only.db")
        self._create_marker(self.db_path)
        # 每个临时库都是"全新库"，强制重新建表（类变量在测试间共享）
        Database._tables_ensured = False
        self.db = Database(self.db_path)
        with self.db.conn() as conn:
            conn.execute("SELECT 1")
        Database._tables_ensured = True

    @staticmethod
    def _create_marker(path):
        # Database 启动校验要求与 Devops-Glue 共享库（ci_pipeline_artifacts 为标记表）
        raw = sqlite3.connect(path)
        raw.execute("CREATE TABLE IF NOT EXISTS ci_pipeline_artifacts (id INTEGER PRIMARY KEY)")
        raw.commit()
        raw.close()

    def tearDown(self):
        self._tmp.cleanup()

    def _insert(self, status="running", project="proj-a"):
        with self.db.conn() as conn:
            cur = conn.execute(
                "INSERT INTO cd_deploy_logs (project, tag, image, deploy_type, target, status) VALUES (?,?,?,?,?,?)",
                (project, "v1.0", "img:v1.0", "ssh", "srv1", status),
            )
            return getattr(cur, "lastrowid", 0)

    def test_delete_is_blocked(self):
        row_id = self._insert()
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            with self.db.conn() as conn:
                conn.execute("DELETE FROM cd_deploy_logs WHERE id=?", (row_id,))
        self.assertIn("append-only", str(ctx.exception))

    def test_insert_and_update_still_allowed(self):
        row_id = self._insert()
        with self.db.conn() as conn:
            conn.execute("UPDATE cd_deploy_logs SET status='ok' WHERE id=?", (row_id,))
            row = conn.execute("SELECT status FROM cd_deploy_logs WHERE id=?", (row_id,)).fetchone()
            count = conn.execute("SELECT COUNT(*) AS c FROM cd_deploy_logs").fetchone()["c"]
        self.assertEqual(row["status"], "ok")
        self.assertEqual(count, 1)

    def test_trigger_creation_is_idempotent(self):
        # 建表流程自带 CREATE TRIGGER IF NOT EXISTS，重复执行不应报错
        with self.db.conn() as conn:
            conn.execute(
                "CREATE TRIGGER IF NOT EXISTS trg_cdl_no_delete "
                "BEFORE DELETE ON cd_deploy_logs "
                "BEGIN SELECT RAISE(ABORT, 'append-only'); END"
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
