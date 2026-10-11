"""CD 部署闸门回归：手动导入 init_mysql.sql 且「表建全」才是「启用 CD」的开关。

覆盖：
  - MySQL 模式绝不对共享库下发任何建表/建索引 DDL（未启用 CD 时零副作用）
  - 闸门清单 CD_REQUIRED_TABLES 与 database/init_mysql.sql、database.py 的建表严格一致（双向）
  - 清单是唯一真相源：SQLite 自动建表必须覆盖清单每一项，漏改清单/漏改 DDL 当场报错（不写死张数）
  - 表必须建全：少任何一张 → cd_missing_tables() 精确报出缺失表、cd_deployed() 为 False
  - 未建全时禁止一切操作：authenticate / verify_token / get_current_user / Webhook 接收 / WebShell 握手
    统一 503 errors.cd_not_deployed（而不是缺表 500），且绝不顺手建表
  - 补表动作按驱动说实话（MySQL 导入脚本 / SQLite 重启自愈），不给错指令
  - 数据库故障（非缺表）不得被误判成「未部署」：库不可用一律 503 errors.db_unavailable（不是裸 500）
    （建表失败 / 打不开库文件 / 构造期校验失败），而清单自检的 RuntimeError 不得被 503 掩盖

运行（项目根）:
    python -m pytest backend/tests/test_cd_deploy_gate.py -q
"""

import asyncio
import os
import re
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# 测试一律用临时 SQLite，忽略 .env 中可能配置的 MySQL（必须在导入 Database 前设置）
from backend.config import settings

settings.db_driver = "sqlite"

from fastapi import HTTPException, Request  # noqa: E402
from fastapi.security import HTTPAuthorizationCredentials  # noqa: E402

from backend.auth import authenticate, cd_deploy_gate, get_current_user, verify_token  # noqa: E402
from backend.database import (  # noqa: E402
    CD_REQUIRED_TABLES,
    Database,
    _is_missing_table_error,
    cd_deploy_hint,
)
from backend.exceptions import AppException  # noqa: E402
from backend.routers.terminal import _ws_verify  # noqa: E402
from backend.routers.webhooks import receive_webhook  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


class _SqlRecorder:
    """记录实际下发的 SQL：用来断言 MySQL 迁移路径「绝不建表」。"""

    def __init__(self):
        self.statements: list[str] = []

    def execute(self, sql, params=None):
        self.statements.append(" ".join(sql.split()))
        return self

    def commit(self):
        pass


def _create_marker(path):
    """Database 启动校验要求与 Devops-Glue 共享库（ci_pipeline_artifacts 为标记表）。"""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE ci_pipeline_artifacts (id INTEGER PRIMARY KEY AUTOINCREMENT, project_key VARCHAR(255))")
    conn.commit()
    conn.close()


class TestMysqlNeverCreatesTables(unittest.TestCase):
    def test_mysql_migrations_issue_no_ddl(self):
        """MySQL 迁移路径只允许对已存在的表 ADD COLUMN，不得出现任何 CREATE。"""
        db = Database.__new__(Database)  # 不跑 __init__（那会连真实数据库）
        db._driver = "mysql"
        rec = _SqlRecorder()
        db._ensure_mysql_migrations(rec)  # type: ignore[arg-type]

        self.assertTrue(rec.statements, "缺失列迁移应至少下发一条 ALTER TABLE")
        joined = " ".join(rec.statements).lower()
        for sql in rec.statements:
            upper = sql.upper()
            self.assertTrue(upper.startswith("ALTER TABLE"), sql)
            self.assertNotIn("CREATE TABLE", upper)
            self.assertNotIn("CREATE INDEX", upper)
        # 回归：cd_sessions 曾在此被「自愈」建表（既越界又不自洽——只补建清单里的一张表）
        self.assertNotIn("cd_sessions", joined)


def _req(cookies=None) -> Request:
    # FastAPI 会注入真实 Request；直接调用 verify_token/get_current_user 时用最小 mock（运行期只访问 .cookies）
    return cast(Request, types.SimpleNamespace(cookies=cookies or {}))


def _creds(token: str = "any-token") -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


class TestRequiredTableListMatchesSources(unittest.TestCase):
    """闸门清单必须与两个建表来源严格一致：多一张/少一张都会误判部署状态。"""

    def test_list_matches_init_mysql_sql(self):
        """MySQL 侧：init_mysql.sql 建的表集合 = CD_REQUIRED_TABLES（双向相等）。"""
        sql = (ROOT / "database" / "init_mysql.sql").read_text(encoding="utf-8")
        created = set(re.findall(r"CREATE TABLE IF NOT EXISTS\s+(cd_\w+)\s*\(", sql))
        self.assertEqual(created, set(CD_REQUIRED_TABLES))
        # 张数由 CD_REQUIRED_TABLES 自身定义，此处刻意不锁定数字：增删表只改那三处，测试自动跟随。

    def test_list_matches_sqlite_autocreate(self):
        """SQLite 侧：database.py 自动建表必须建全清单里的同样那些表，否则开发模式会被自家闸门拦住。"""
        src = (ROOT / "backend" / "database.py").read_text(encoding="utf-8")
        created = set(re.findall(r"CREATE TABLE IF NOT EXISTS\s+(cd_\w+)\s*\(", src))
        self.assertEqual(created, set(CD_REQUIRED_TABLES))

    def test_glue_contract_view_not_required(self):
        """v_glue_deploy_logs 是给 Devops-Glue 的契约视图、非 CD 自身表，不参与闸门。"""
        self.assertFalse([t for t in CD_REQUIRED_TABLES if not t.startswith("cd_")])


class TestCdDeployedCompleteness(unittest.TestCase):
    """表必须「建全」：少任何一张 = 未部署（半套 schema 不放行）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "test_gate.db")
        _create_marker(self.db_path)
        Database._tables_ensured = False

    def tearDown(self):
        Database._tables_ensured = False  # 类变量在测试间共享，恢复默认避免污染其它用例
        self._tmp.cleanup()

    def test_sqlite_autocreated_db_is_deployed(self):
        """SQLite 模式随库自动建表 → 清单全齐 → 已部署（开发/单机场景）。"""
        db = Database(self.db_path)
        with db.conn():
            pass  # 触发 SQLite 自动建表
        self.assertEqual(db.cd_missing_tables(), [])
        self.assertTrue(db.cd_deployed())
        with db.conn() as conn:
            row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='cd_sessions'").fetchone()
        self.assertIsNotNone(row)

    def test_one_missing_table_means_not_deployed(self):
        """只缺一张也当作未部署：精确报出缺失表，且探测本身不补建。"""
        db = Database(self.db_path)
        with db.conn() as conn:
            conn.execute("DROP TABLE cd_approvals")
        self.assertEqual(db.cd_missing_tables(), ["cd_approvals"])
        self.assertFalse(db.cd_deployed())
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='cd_approvals'").fetchone()
        self.assertIsNone(row, "探测未部署状态时绝不允许顺手建表")

    def test_all_tables_missing_reports_full_list(self):
        """CD 表从未导入（模拟 MySQL 共享库）→ 清单内全部报缺（张数取 len 清单，不写死），且零 DDL 痕迹。"""
        db = Database(self.db_path)
        with patch.object(Database, "_ensure_cd_tables", lambda self, conn: None):
            self.assertEqual(db.cd_missing_tables(), list(CD_REQUIRED_TABLES))
            self.assertFalse(db.cd_deployed())
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='cd_sessions'").fetchone()
        self.assertIsNone(row, "探测未部署状态时绝不允许顺手建表")

    def test_db_failure_not_reported_as_not_deployed(self):
        """连接故障（非缺表）原样抛出：不能误导运维去重跑建表脚本掩盖数据库宕机。"""
        db = Database(self.db_path)
        with patch.object(Database, "conn", side_effect=RuntimeError("db down")):
            with self.assertRaises(RuntimeError):
                db.cd_missing_tables()

    def test_deployed_invalid_token_is_401(self):
        """已部署时闸门不误伤鉴权：无效 token 仍是 401（而非 503）。"""
        db = Database(self.db_path)
        with db.conn():
            pass
        with self.assertRaises(HTTPException) as ctx:
            verify_token(_req(), _creds("bogus"), db)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_authenticate_rejected_when_tables_incomplete(self):
        """表不全时 authenticate 抛 503 errors.cd_not_deployed（而非缺表 500 / 静默建表）。"""
        db = Database(self.db_path)
        with db.conn() as conn:
            conn.execute("DROP TABLE cd_webhooks")  # 只缺一张，也必须整体拒绝登录
        with self.assertRaises(AppException) as ctx:
            authenticate("alice", "secret", db)
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.error_key, "errors.cd_not_deployed")
        self.assertEqual(ctx.exception.error_params["tables"], "cd_webhooks")
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='cd_webhooks'").fetchone()
        self.assertIsNone(row, "闸门只报警不修补：绝不允许顺手建表")

    def test_authenticate_proceeds_when_deployed(self):
        """已部署时闸门放行：缺密码哈希/无用户 → 返回 None（凭据失败语义不变）。"""
        db = Database(self.db_path)
        with db.conn() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS admin_users ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, username VARCHAR(64) UNIQUE, "
                "password_hash VARCHAR(255), role VARCHAR(32) DEFAULT '', "
                "systems VARCHAR(255) DEFAULT '', status INTEGER DEFAULT 1)"
            )
        self.assertIsNone(authenticate("nobody", "secret", db))


class TestTableInventoryIsSingleSource(unittest.TestCase):
    """CD_REQUIRED_TABLES 是唯一维护清单：SQLite 自动建表必须与它逐张对齐，漏改当场报错。

    清单以后会加表/删表，所以这里一律用 len(CD_REQUIRED_TABLES) / 集合比较，不写死张数；
    新增表时三处同步（清单 + init_mysql.sql + _ensure_cd_tables），漏掉任一处都会被拦下。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "test_gate_inv.db")
        _create_marker(self.db_path)
        Database._tables_ensured = False

    def tearDown(self):
        Database._tables_ensured = False
        self._tmp.cleanup()

    def test_sqlite_autocreate_covers_whole_inventory(self):
        """SQLite 首次连库建表后，清单里的每一张都存在（且自动建表工作正常）。"""
        db = Database(self.db_path)
        with db.conn():
            pass
        self.assertEqual(db.cd_missing_tables(), [])
        self.assertTrue(db.cd_deployed())
        raw = sqlite3.connect(self.db_path)
        try:
            tables = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            views = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='view'").fetchall()}
        finally:
            raw.close()
        self.assertTrue(set(CD_REQUIRED_TABLES) <= tables, set(CD_REQUIRED_TABLES) - tables)
        # 给 Glue 的契约视图不是 CD 自身表：它在 sqlite_master 里是 view，闸门只核对 table
        # （MySQL 侧同理限定 BASE TABLE），所以清单里不该出现它
        self.assertIn("v_glue_deploy_logs", views)
        self.assertNotIn("v_glue_deploy_logs", CD_REQUIRED_TABLES)

    def test_fail_fast_when_inventory_has_no_sqlite_ddl(self):
        """清单里多了表、SQLite 建表逻辑却没补 → 首次连库即 RuntimeError 点名该表（绝不半套运行）。"""
        ghost = "cd_ghost_table"
        with patch("backend.database.CD_REQUIRED_TABLES", CD_REQUIRED_TABLES + (ghost,)):
            db = Database(self.db_path)
            with self.assertRaises(RuntimeError) as ctx:
                with db.conn():
                    pass
        self.assertIn(ghost, str(ctx.exception))
        self.assertIn("_ensure_cd_tables", str(ctx.exception))

    def test_runtime_drop_blocks_then_restart_heals(self):
        """运行期被人为 DROP：闸门立刻拒绝（不静默补救）；重启服务后自动建全自愈。"""
        db = Database(self.db_path)
        with db.conn():
            pass
        with db.conn() as conn:
            conn.execute("DROP TABLE cd_bots")
        self.assertEqual(db.cd_missing_tables(), ["cd_bots"])
        with sqlite3.connect(self.db_path) as conn:  # 探测不补建
            row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='cd_bots'").fetchone()
        self.assertIsNone(row)
        Database._tables_ensured = False  # 等价于重启服务
        with db.conn():
            pass
        self.assertEqual(db.cd_missing_tables(), [])


class TestDeployHintMatchesDriver(unittest.TestCase):
    """补表动作必须按驱动说实话：SQLite 用户不该被指去导入 MySQL 脚本（那是错误的运维指令）。"""

    class _FakeDb:
        def __init__(self, driver: str, missing: list[str]):
            self.driver = driver
            self._missing = missing

        def cd_missing_tables(self) -> list[str]:
            return list(self._missing)

    def test_hint_text_per_driver(self):
        self.assertIn("init_mysql.sql", cd_deploy_hint("mysql"))
        self.assertNotIn("init_mysql.sql", cd_deploy_hint("sqlite"))
        self.assertIn("重启", cd_deploy_hint("sqlite"))

    def test_gate_message_follows_driver(self):
        """闸门消息 = 中性描述 + 缺失表 + 按驱动动作；error_params 仍只给表名（前端 i18n 插值）。"""
        for driver in ("mysql", "sqlite"):
            with self.assertRaises(AppException) as ctx:
                cd_deploy_gate(cast(Database, self._FakeDb(driver, ["cd_webhooks", "cd_sessions"])))
            msg = ctx.exception.message
            self.assertEqual(ctx.exception.status_code, 503)
            self.assertEqual(ctx.exception.error_key, "errors.cd_not_deployed")
            self.assertIn("cd_webhooks", msg)
            self.assertIn(cd_deploy_hint(driver), msg)
            self.assertEqual(ctx.exception.error_params["tables"], "cd_webhooks、cd_sessions")


class TestRequestGateBlocksOperations(unittest.TestCase):
    """即使能登录，也禁止做任何操作：未建全时所有带身份的操作都 503（而非缺表 500）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "test_gate_req.db")
        _create_marker(self.db_path)
        Database._tables_ensured = False
        # 关掉 SQLite 自动建表，模拟「MySQL 共享库里 CD 表从未导入」
        self._no_autocreate = patch.object(Database, "_ensure_cd_tables", lambda self, conn: None)
        self._no_autocreate.start()

    def tearDown(self):
        self._no_autocreate.stop()
        Database._tables_ensured = False
        self._tmp.cleanup()

    def _db(self) -> Database:
        return Database(self.db_path)

    def test_verify_token_returns_503_for_every_model(self):
        """已持有 token 的请求（verify_token）也一律拒绝，并报出全部缺失表。"""
        db = self._db()
        with self.assertRaises(AppException) as ctx:
            verify_token(_req(), _creds(), db)
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.error_key, "errors.cd_not_deployed")
        self.assertEqual(len(ctx.exception.error_params["tables"].split("、")), len(CD_REQUIRED_TABLES))

    def test_get_current_user_returns_503(self):
        """get_current_user（含 require_perm 链路）同样被闸门拦住。"""
        db = self._db()
        with self.assertRaises(AppException) as ctx:
            get_current_user(_req(), _creds(), db)
        self.assertEqual(ctx.exception.status_code, 503)

    def test_public_webhook_receive_returns_503(self):
        """公开 Webhook 接收端点（无登录、靠 token 鉴权）也拒绝：写入必然缺表。"""
        db = self._db()
        with self.assertRaises(AppException) as ctx:
            asyncio.run(receive_webhook("tok", cast(Request, types.SimpleNamespace()), db))
        self.assertEqual(ctx.exception.status_code, 503)

    def test_webshell_verify_returns_503(self):
        """WebShell（最高危操作）走独立鉴权路径，同样被闸门拒绝：转 503 供 WS 干净关闭。"""
        from backend.config import settings as cfg

        original = cfg.db_path
        cfg.db_path = self.db_path
        try:
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(_ws_verify("any-token"))
        finally:
            cfg.db_path = original
        self.assertEqual(ctx.exception.status_code, 503)
        # SQLite 下补表动作是「重启自动建表」，绝不能告诉用户去导入 MySQL 脚本
        self.assertIn("重启", str(ctx.exception.detail))
        self.assertNotIn("init_mysql.sql", str(ctx.exception.detail))

    def test_missing_token_still_401(self):
        """没带 token 依旧是「请先登录」401：部署问题不该掩盖未认证语义。"""
        db = self._db()
        with self.assertRaises(HTTPException) as ctx:
            verify_token(_req(), None, db)
        self.assertEqual(ctx.exception.status_code, 401)


class TestDatabaseUnavailableIs503(unittest.TestCase):
    """库不可用（建不了表 / 打不开库）必须 503 errors.db_unavailable，而不是裸 500。

    与「未部署」严格区分：前者要查环境（连接/权限/磁盘），后者才是去补建表脚本；
    混为一谈会把运维指向错误动作。但清单自检抛的 RuntimeError 属代码缺陷，必须原样冒泡。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "test_gate_dbunavail.db")
        _create_marker(self.db_path)
        Database._tables_ensured = False

    def tearDown(self):
        Database._tables_ensured = False
        self._tmp.cleanup()

    def test_sqlite_create_table_failure_is_503(self):
        """SQLite 建表失败（库只读 / 磁盘满 / 被占用）→ 503 errors.db_unavailable（原先会漏成裸 500）。"""
        db = Database(self.db_path)
        boom = sqlite3.OperationalError("attempt to write a readonly database")
        with patch.object(Database, "_ensure_cd_tables", side_effect=boom):
            with self.assertRaises(AppException) as ctx:
                cd_deploy_gate(db)
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.error_key, "errors.db_unavailable")
        self.assertEqual(ctx.exception.error_params["driver"], "sqlite")
        self.assertIn("readonly", ctx.exception.error_params["reason"])
        # 绝不能报成「未部署」：那会把运维指向「重跑建表脚本」，而真凶是文件权限/磁盘
        self.assertNotEqual(ctx.exception.error_key, "errors.cd_not_deployed")

    def test_sqlite_open_failure_is_503(self):
        """库文件打不开（目录不存在 / 不可写）→ 503 errors.db_unavailable。"""
        db = Database(self.db_path)
        boom = sqlite3.OperationalError("unable to open database file")
        with patch.object(Database, "_connect_sqlite", side_effect=boom):
            with self.assertRaises(AppException) as ctx:
                cd_deploy_gate(db)
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.error_key, "errors.db_unavailable")

    def test_shared_db_probe_failure_is_503(self):
        """构造期校验失败（没和 Devops-Glue 共用同一库）→ 503 而非 500。"""
        with self.assertRaises(AppException) as ctx:
            Database(os.path.join(self._tmp.name, "not_glue.db"))
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.error_key, "errors.db_unavailable")

    def test_inventory_selfcheck_error_is_not_masked(self):
        """清单自检的 RuntimeError（清单与 DDL 漂移 = 代码/配置缺陷）必须原样冒泡，不许被伪装成 503。"""
        db = Database(self.db_path)
        with patch.object(Database, "_ensure_cd_tables", side_effect=RuntimeError("清单与 DDL 不一致")):
            with self.assertRaises(RuntimeError):
                cd_deploy_gate(db)


class TestMissingTableErrorDetection(unittest.TestCase):
    def test_detects_sqlite_and_mysql_missing_table(self):
        self.assertTrue(_is_missing_table_error(sqlite3.OperationalError("no such table: cd_sessions")))

        class _FakeMysqlError(Exception):
            def __init__(self):
                super().__init__(1146, "Table 'devops_glue.cd_sessions' doesn't exist")

        self.assertTrue(_is_missing_table_error(_FakeMysqlError()))
        # 其它错误（如连接失败）不得被误判成「未部署」
        self.assertFalse(_is_missing_table_error(ConnectionError("connection refused")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
