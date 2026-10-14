"""CD 部署闸门回归:Devops-Glue 的集成服务契约(enabled + provision)决定 CD 能否建表与放行.

契约只经 Glue 的窄只读端点读取(CI 是契约的唯一写入方;读法见 backend/database.py 顶部注释):
  - 端点无答复(无契约行)或 enabled<>1 → CD **未启用**:零 DDL,入口统一 503 errors.cd_not_enabled;
  - 端点问不到(网络 / 鉴权 / 未配 CI)→ 按契约不可达 fail-closed:503 errors.ci_contract_unreachable;
  - enabled=1 + provision='auto'   → **授权 CD 自动建表**(MySQL / SQLite 同理),缺表 503 cd_not_deployed;
  - enabled=1 + provision='manual' → CD **零 DDL**,只认运维预建表(MySQL 导入 database/init_mysql.sql).

覆盖:
  - 未启用(端点答复未启用:契约行缺失 / enabled=0)一律 503 errors.cd_not_enabled 且零 DDL;
  - 端点不可达一律 503 errors.ci_contract_unreachable(fail-closed,不误报"未启用");
  - 启用后**无需重启**即生效(CI 一改契约,下一个请求就建表/放行);
  - auto 模式 MySQL 会下发 CREATE TABLE 覆盖清单全集,且不触碰 ci_* / ci_admin_users 等共享表;
  - manual 模式 MySQL 零 DDL(连 ALTER 都不发):不越权改共享库结构;
  - 表必须建全:少任何一张 → 503 errors.cd_not_deployed + 缺表明细,探测/闸门不顺手补建;
  - 补表动作按 provision 说实话(auto:重启自愈;manual:MySQL 导入脚本 / SQLite 交运维);
  - 数据库故障(非缺表)一律 503 errors.db_unavailable,清单自检的 RuntimeError 不被掩盖;
  - 未启用 / 未建全时:verify_token,get_current_user,Webhook 接收,WebShell 握手
    全部被拒(分别是 cd_not_enabled / cd_not_deployed,而不是缺表 500).

清单与 DDL 的'源码一致性'(CD_REQUIRED_TABLES ↔ init_mysql.sql ↔ _MYSQL_TABLE_DDL ↔ SQLite 建表)
由 backend/tests/test_cd_schema_sources.py 锁定,本文件只管运行时行为.

运行(项目根):
    python -m pytest backend/tests/test_cd_deploy_gate.py -q
"""

import asyncio
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import closing
from pathlib import Path
from typing import cast
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# 测试一律用临时 SQLite,忽略 .env 中可能配置的 MySQL(必须在导入 Database 前设置)
from backend.config import settings

settings.db_driver = "sqlite"

import backend.database as database  # noqa: E402

from fastapi import HTTPException, Request  # noqa: E402
from fastapi.security import HTTPAuthorizationCredentials  # noqa: E402

from backend.auth import (  # noqa: E402
    CD_NOT_ENABLED_MSG,
    cd_deploy_gate,
    get_current_user,
    verify_token,
)
from backend.database import (  # noqa: E402
    CD_REQUIRED_TABLES,
    CdIntegrationState,
    ContractUnavailableError,
    Database,
    DatabaseUnavailableError,
    PROVISION_AUTO,
    _is_missing_table_error,
    cd_deploy_hint,
    cd_not_enabled_hint,
)
from backend.exceptions import AppException  # noqa: E402
from backend.routers.terminal import _ws_verify  # noqa: E402
from backend.routers.webhooks import receive_webhook  # noqa: E402

from cd_contract import CONTRACT_DDL, set_cd_contract  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
_CONTRACT_DDL = CONTRACT_DDL  # 契约表 DDL 只有一份(backend/tests/cd_contract.py)


def _write_contract(path, enabled=1, provision="auto", with_row=True, create_table=True):
    """模拟 CI 后台的启用 / 关闭动作:驱动契约替身返回对应答复(并同步假库里的契约行)."""
    set_cd_contract(path, enabled=enabled, provision=provision, with_row=with_row, create_table=create_table)


class _FakeCursor:
    """伪游标:把已下发的 CREATE TABLE 伪装成 information_schema 的结果,让清单自检跑通."""

    def __init__(self, rows):
        self._rows = list(rows)

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _RecordedCursor:
    """_MysqlWrapper 需要 raw 连接提供 cursor():把执行转发给记录器,再回放结果."""

    def __init__(self, rec):
        self._rec = rec
        self._result = _FakeCursor([])

    def execute(self, sql, params=None):
        self._result = self._rec.execute(sql, params)
        return self

    def fetchall(self):
        return self._result.fetchall()

    def fetchone(self):
        return self._result.fetchone()

    def close(self):
        pass


class _SqlRecorder:
    """记录实际下发的 SQL -- 用来断言'auto 建表'与'manual / 未启用时零 DDL'.

    契约状态不走 SQL(CD 不读表):_run_conn 会把 _fetch_contract_http 换成按本记录器
    contract_row / contract_endpoint_down 字段生成答复的替身.
    """

    def __init__(self, contract_row=None, contract_endpoint_down=False):
        self.statements: list[str] = []
        self.contract_row = contract_row  # None = 端点答复"未启用"(无契约行)
        self.contract_endpoint_down = contract_endpoint_down  # True = 契约端点不可达(网络/鉴权/未配 CI)
        self.created_tables: set[str] = set()

    def contract_state(self):
        """按记录器字段生成契约端点答复(供 _run_conn 打桩)."""
        if self.contract_endpoint_down:
            raise ContractUnavailableError("契约端点请求失败：模拟网络故障")
        if self.contract_row is None:
            return CdIntegrationState(False, PROVISION_AUTO, False)
        return CdIntegrationState(
            bool(self.contract_row.get("enabled")),
            self.contract_row.get("provision") or PROVISION_AUTO,
            True,
        )

    def cursor(self):
        """raw 连接接口(_MysqlWrapper 会调用它)."""
        return _RecordedCursor(self)

    def execute(self, sql, params=None):
        flat = " ".join(sql.split())
        self.statements.append(flat)
        upper = flat.upper()
        if upper.startswith("CREATE TABLE IF NOT EXISTS"):
            name = flat.split("CREATE TABLE IF NOT EXISTS", 1)[1].strip().split("(")[0].strip()
            self.created_tables.add(name)
            return _FakeCursor([])
        if "INFORMATION_SCHEMA.TABLES" in upper:
            return _FakeCursor([{"table_name": t} for t in sorted(self.created_tables)])
        return _FakeCursor([])

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def _mysql_db(tmpdir) -> Database:
    """构造'驱动是 mysql 但不连真库'的 Database(用 __new__ 绕过 __init__ 以强制 mysql 驱动)."""
    db = Database.__new__(Database)
    db._driver = "mysql"
    db._path = Path(tmpdir)
    return db


def _serve_recorder(rec: _SqlRecorder):
    """让 db.conn() 直接使用 rec 作为连接,并挡掉 MySQL 连接池初始化."""
    pool = types.SimpleNamespace(connection=lambda: rec)
    return [
        patch.object(Database, "_init_pool", lambda self: None),
        patch.object(Database, "_pool", pool),
    ]


def _req(cookies=None) -> Request:
    # FastAPI 会注入真实 Request;直接调用 verify_token/get_current_user 时用最小 mock(运行期只访问 .cookies)
    return cast(Request, types.SimpleNamespace(cookies=cookies or {}))


def _creds(token: str = "any-token") -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


class TestIntegrationStateReadsContract(unittest.TestCase):
    """契约读语义:端点答复'未启用'就是未启用(不种子,不回退),且不越权建表."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "test_contract.db")
        Database._tables_ensured = False

    def tearDown(self):
        Database._tables_ensured = False
        self._tmp.cleanup()

    def _db(self) -> Database:
        return Database(self.db_path)

    def _tables(self) -> set[str]:
        # closing 包裹:sqlite3 连接的 with 只管事务不关连接,Windows 上句柄会锁住库文件
        # 导致 tearDown 清理临时目录报 NotADirectoryError
        with closing(sqlite3.connect(self.db_path)) as conn:
            return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def test_no_contract_answer_means_disabled_without_ddl(self):
        """端点答复未启用(替身默认 = 无契约行)→ 未启用,零 DDL:CD 表不许凭空出现."""
        db = self._db()
        state = db.cd_integration_state()
        self.assertFalse(state.enabled)
        self.assertFalse(state.row_exists)
        # 未启用 → 一个 DDL 都不该下发:CD 表不许凭空出现
        self.assertFalse([t for t in self._tables() if t.startswith("cd_")], self._tables())
        # 闸门统一 503 cd_not_enabled(而不是 cd_not_deployed 骗人去补表)
        with self.assertRaises(AppException) as ctx:
            cd_deploy_gate(db)
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.error_key, "errors.cd_not_enabled")
        self.assertEqual(ctx.exception.error_params["service"], "cd")
        self.assertIn("集成服务", ctx.exception.message)

    def test_contract_table_without_row_means_disabled(self):
        _write_contract(self.db_path, with_row=False)
        db = self._db()
        state = db.cd_integration_state()
        self.assertFalse(state.enabled)
        self.assertFalse(state.row_exists, "有表无行 = 从未启用过，不得默认放行")
        with self.assertRaises(AppException) as ctx:
            cd_deploy_gate(db)
        self.assertEqual(ctx.exception.error_key, "errors.cd_not_enabled")

    def test_disabled_row_freezes_admission_but_keeps_tables(self):
        """enabled=0 = 冻结准入:即使 CD 表都在(曾启用过)也必须拒绝,且不 DROP 任何表."""
        _write_contract(self.db_path, enabled=1, provision="auto")
        db = self._db()
        with db.conn():  # auto 模式:首次连库自动建全
            pass
        self.assertEqual(db.cd_missing_tables(), [])
        self.assertIsNone(cd_deploy_gate(db))  # 放行
        _write_contract(self.db_path, enabled=0, provision="auto")  # CI 后台关闭开关
        state = db.cd_integration_state()
        self.assertFalse(state.enabled)
        self.assertTrue(state.row_exists)
        with self.assertRaises(AppException) as ctx:
            cd_deploy_gate(db)
        self.assertEqual(ctx.exception.error_key, "errors.cd_not_enabled")
        # 关闭只冻结准入:表与数据原样保留
        self.assertEqual(db.cd_missing_tables(), [])
        self.assertTrue(set(CD_REQUIRED_TABLES) <= self._tables())

    def test_enable_takes_effect_without_restart(self):
        """CI 后台一启用,运行中的 CD 下一个请求即生效(无需重启,无需手工重置 _tables_ensured)."""
        db = self._db()
        with self.assertRaises(AppException) as ctx:
            cd_deploy_gate(db)
        self.assertEqual(ctx.exception.error_key, "errors.cd_not_enabled")
        self.assertFalse(Database._tables_ensured)
        _write_contract(self.db_path, enabled=1, provision="auto")
        self.assertIsNone(cd_deploy_gate(db))
        self.assertEqual(db.cd_missing_tables(), [], "auto 模式下应已自动建全")
        self.assertTrue(Database._tables_ensured)

    def test_login_route_blocked_when_not_enabled(self):
        """未启用时 SSO 登录入口(start)也在闸门处被拒:503 cd_not_enabled,凭据流程不会开始."""
        from backend.routers.oidc import start as oidc_start

        db = self._db()
        # XHR / 脚本请求(非整页导航):闸门抛 AppException,而不是 302 回登录页
        req = cast(
            Request,
            types.SimpleNamespace(cookies={}, headers=types.SimpleNamespace(get=lambda k, d="": "application/json")),
        )
        with (
            patch.object(settings, "oidc_issuer", "https://glue.example.com"),
            patch.object(settings, "oidc_client_secret", "s"),
            patch.object(settings, "oidc_redirect_uri", "https://cd.example.com/api/auth/oidc/callback"),
            self.assertRaises(AppException) as ctx,
        ):
            oidc_start(req, db)
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.error_key, "errors.cd_not_enabled")

    def test_manual_provision_never_creates_tables(self):
        """manual:即使已启用也不建表(只认预建表)→ 零 DDL,闸门报 cd_not_deployed."""
        _write_contract(self.db_path, enabled=1, provision="manual")
        db = self._db()
        with db.conn() as conn:
            conn.execute("SELECT 1")
        self.assertFalse([t for t in self._tables() if t.startswith("cd_")], self._tables())
        self.assertEqual(db.cd_missing_tables(), list(CD_REQUIRED_TABLES))
        with self.assertRaises(AppException) as ctx:
            cd_deploy_gate(db)
        self.assertEqual(ctx.exception.error_key, "errors.cd_not_deployed")
        self.assertIn("manual", ctx.exception.message)


class TestMysqlProvisionMatrix(unittest.TestCase):
    """MySQL 侧建表授权完全跟随 provision:auto 建表,manual / 未启用零 DDL(且不碰共享表)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        Database._tables_ensured = False

    def tearDown(self):
        Database._tables_ensured = False
        self._tmp.cleanup()

    def _run_conn(self, rec: _SqlRecorder) -> _SqlRecorder:
        """在'假 MySQL 连接'上跑一次 conn()(触发建表闸门),返回 SQL 记录."""
        db = _mysql_db(self._tmp.name)
        patches = _serve_recorder(rec) + [
            patch.object(database, "_fetch_contract_http", lambda service="cd": rec.contract_state()),
        ]
        for p in patches:
            p.start()
        try:
            with db.conn() as conn:
                conn.execute("SELECT 1")
        finally:
            for p in patches:
                p.stop()
        return rec

    def _ddl(self, rec: _SqlRecorder) -> list[str]:
        return [s for s in rec.statements if s.upper().startswith(("CREATE", "ALTER", "DROP"))]

    def test_auto_creates_every_listed_table(self):
        rec = self._run_conn(_SqlRecorder(contract_row={"enabled": 1, "provision": "auto"}))
        self.assertEqual(rec.created_tables, set(CD_REQUIRED_TABLES))
        self.assertTrue(Database._tables_ensured)

    def test_auto_ddl_never_touches_shared_tables(self):
        """建表 / 建索引不许出现共享表(ci_* / admin_users / roles)-- CD 只改自己的 cd_* 对象."""
        rec = self._run_conn(_SqlRecorder(contract_row={"enabled": 1, "provision": "auto"}))
        joined = " ".join(rec.statements)
        for shared in ("ci_pipeline_artifacts", "ci_job_git_map", "admin_users", "roles", "permissions"):
            self.assertNotIn(shared, joined)
        self.assertNotIn("DROP ", joined.upper())  # 关闭集成服务也不许 DROP(数据必须留住)

    def test_auto_also_runs_indexes_and_migrations(self):
        rec = self._run_conn(_SqlRecorder(contract_row={"enabled": 1, "provision": "auto"}))
        joined = " ".join(rec.statements)
        self.assertIn("CREATE INDEX idx_cdl_project ON cd_deploy_logs(project)", joined)
        self.assertIn("ALTER TABLE cd_deploy_logs ADD COLUMN lock_key", joined)

    def test_manual_sends_no_ddl_at_all(self):
        """manual:一条 CREATE / ALTER / DROP 都不许发(结构归 DBA,CD 只读)."""
        rec = self._run_conn(_SqlRecorder(contract_row={"enabled": 1, "provision": "manual"}))
        self.assertEqual(self._ddl(rec), [])
        self.assertTrue(Database._tables_ensured, "manual 下无需 DDL：标记完成，避免每请求重读契约")

    def test_row_missing_sends_no_ddl(self):
        rec = self._run_conn(_SqlRecorder(contract_row=None))
        self.assertEqual(self._ddl(rec), [])
        self.assertFalse(Database._tables_ensured, "未启用不得标记完成：CI 一启用就要能立即建表")

    def test_zero_enabled_sends_no_ddl(self):
        rec = self._run_conn(_SqlRecorder(contract_row={"enabled": 0, "provision": "auto"}))
        self.assertEqual(self._ddl(rec), [])
        self.assertFalse(Database._tables_ensured)

    def test_contract_endpoint_unreachable_raises_contract_unreachable(self):
        """契约端点不可达(网络/鉴权/未配 CI)= 契约不可达:conn() 直接 503 fail-closed.

        与旧行为的分界:直读回退时代"问不到 → 当未启用 → 零 DDL 静默放行失败";
        现在问不到就是环境故障,把 503 ci_contract_unreachable 抛给调用方,不误判成未启用.
        """
        db = _mysql_db(self._tmp.name)
        rec = _SqlRecorder(contract_endpoint_down=True)
        patches = _serve_recorder(rec) + [
            patch.object(database, "_fetch_contract_http", lambda service="cd": rec.contract_state()),
        ]
        for p in patches:
            p.start()
        try:
            with self.assertRaises(ContractUnavailableError) as ctx:
                with db.conn() as conn:
                    conn.execute("SELECT 1")
        finally:
            for p in patches:
                p.stop()
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.error_key, "errors.ci_contract_unreachable")
        self.assertEqual(self._ddl(rec), [], "问不到契约就不许下发任何 DDL")
        self.assertFalse(Database._tables_ensured)


class TestDeployHintMatchesProvision(unittest.TestCase):
    """补表动作必须按 provision 说实话:auto 说'重启自愈',manual 才说'导入脚本'."""

    class _FakeDb:
        """最小 fake:闸门只用到 driver / cd_integration_state / cd_missing_tables."""

        def __init__(self, driver: str, missing: list[str], provision: str = "auto", enabled: bool = True):
            self.driver = driver
            self._missing = missing
            self._state = CdIntegrationState(enabled, provision, enabled)

        def cd_integration_state(self) -> CdIntegrationState:
            return self._state

        def cd_missing_tables(self) -> list[str]:
            return list(self._missing)

    def test_auto_hint_says_restart(self):
        hint = cd_deploy_hint("mysql", "auto")
        self.assertIn("重启", hint)
        self.assertNotIn("init_mysql.sql", hint)

    def test_manual_hint_points_to_script_for_mysql(self):
        self.assertIn("init_mysql.sql", cd_deploy_hint("mysql", "manual"))

    def test_manual_hint_never_tells_sqlite_to_import_mysql_script(self):
        """manual + SQLite 是本地库文件:不能叫用户去导入 MySQL 脚本(错误运维指令)."""
        hint = cd_deploy_hint("sqlite", "manual")
        self.assertIn("manual", hint)
        self.assertNotIn("init_mysql.sql", hint)

    def test_legacy_call_without_provision_keeps_driver_wording(self):
        self.assertIn("重启", cd_deploy_hint("sqlite"))
        self.assertIn("init_mysql.sql", cd_deploy_hint("mysql"))

    def test_not_enabled_hint_points_to_ci_console(self):
        self.assertIn("集成服务", cd_not_enabled_hint())

    def test_gate_message_follows_provision(self):
        """闸门消息 = 中性描述 + 缺失表 + 按 provision 的动作;error_params 仍只给表名(前端 i18n 插值)."""
        for provision in ("auto", "manual"):
            with self.assertRaises(AppException) as ctx:
                cd_deploy_gate(cast(Database, self._FakeDb("mysql", ["cd_webhooks", "cd_sessions"], provision)))
            self.assertEqual(ctx.exception.status_code, 503)
            self.assertEqual(ctx.exception.error_key, "errors.cd_not_deployed")
            self.assertIn("cd_webhooks", ctx.exception.message)
            self.assertIn(cd_deploy_hint("mysql", provision), ctx.exception.message)
            self.assertEqual(ctx.exception.error_params["tables"], "cd_webhooks、cd_sessions")

    def test_gate_message_when_not_enabled(self):
        """未启用:用户可见提示只有 CD_NOT_ENABLED_MSG 这一句,动作细节走 detail.hint.

        提示框(登录页 error 框 / toast)只渲染 error_key 或字符串型 detail,
        dict 型的 detail 不会被展示 -- 所以长文案既进不了提示框,也没丢给排查的人.
        """
        with self.assertRaises(AppException) as ctx:
            cd_deploy_gate(cast(Database, self._FakeDb("mysql", [], "auto", enabled=False)))
        self.assertEqual(ctx.exception.error_key, "errors.cd_not_enabled")
        self.assertEqual(ctx.exception.message, CD_NOT_ENABLED_MSG)
        self.assertEqual(ctx.exception.detail["hint"], cd_not_enabled_hint())
        self.assertNotIn(cd_not_enabled_hint(), ctx.exception.message)


class TestCdMissingTablesCompleteness(unittest.TestCase):
    """表必须建全:少任何一张 = 未部署(半套 schema 不放行),探测不顺手补建."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "test_gate.db")
        _write_contract(self.db_path, enabled=1, provision="auto")
        Database._tables_ensured = False

    def tearDown(self):
        Database._tables_ensured = False
        self._tmp.cleanup()

    def test_autocreated_db_is_deployed(self):
        db = Database(self.db_path)
        with db.conn():
            pass  # 触发 auto 自动建表
        self.assertEqual(db.cd_missing_tables(), [])
        self.assertTrue(db.cd_deployed())
        with closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='cd_sessions'").fetchone()
        self.assertIsNotNone(row)

    def test_one_missing_table_means_not_deployed(self):
        """只缺一张也当作未部署:精确报出缺失表,且探测本身不补建."""
        db = Database(self.db_path)
        with db.conn() as conn:
            conn.execute("DROP TABLE cd_approvals")
        self.assertEqual(db.cd_missing_tables(), ["cd_approvals"])
        self.assertFalse(db.cd_deployed())
        with closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='cd_approvals'").fetchone()
        self.assertIsNone(row, "探测未部署状态时绝不允许顺手建表")

    def test_all_tables_missing_reports_full_list(self):
        """CD 表从未建过(等价 manual 未导入)→ 清单内全部报缺(张数取 len 清单,不写死)."""
        db = Database(self.db_path)
        with patch.object(Database, "_ensure_cd_tables", lambda self, conn: None):
            self.assertEqual(db.cd_missing_tables(), list(CD_REQUIRED_TABLES))
            self.assertFalse(db.cd_deployed())
        with closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='cd_sessions'").fetchone()
        self.assertIsNone(row, "探测未部署状态时绝不允许顺手建表")

    def test_runtime_drop_blocks_then_restart_heals(self):
        """运行期被人为 DROP:闸门立刻拒绝(不静默补救);重启(_tables_ensured 复位)后自愈."""
        db = Database(self.db_path)
        with db.conn():
            pass
        with db.conn() as conn:
            conn.execute("DROP TABLE cd_bots")
        self.assertEqual(db.cd_missing_tables(), ["cd_bots"])
        Database._tables_ensured = False  # 等价于重启服务
        with db.conn():
            pass
        self.assertEqual(db.cd_missing_tables(), [])

    def test_db_failure_not_reported_as_not_deployed(self):
        """连接故障(非缺表)原样抛出:不能误导运维去重跑建表脚本,掩盖数据库宕机."""
        db = Database(self.db_path)
        with patch.object(Database, "conn", side_effect=RuntimeError("db down")):
            with self.assertRaises(RuntimeError):
                db.cd_missing_tables()


class TestRequestGateBlocksOperations(unittest.TestCase):
    """即使能登录,也禁止做任何操作:未建全时所有带身份的操作都 503(而非缺表 500)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "test_gate_req.db")
        # 已启用 + auto,但打掉自动建表:模拟"表还没建出来"的窗口(auto 重启前 / manual 未导入)
        _write_contract(self.db_path, enabled=1, provision="auto")
        Database._tables_ensured = False
        self._no_autocreate = patch.object(Database, "_ensure_cd_tables", lambda self, conn: None)
        self._no_autocreate.start()

    def tearDown(self):
        self._no_autocreate.stop()
        Database._tables_ensured = False
        self._tmp.cleanup()

    def _db(self) -> Database:
        return Database(self.db_path)

    def test_verify_token_returns_503_for_every_model(self):
        """已持有 token 的请求(verify_token)也一律拒绝,并报出全部缺失表."""
        db = self._db()
        with self.assertRaises(AppException) as ctx:
            verify_token(_req(), _creds(), db)
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.error_key, "errors.cd_not_deployed")
        self.assertEqual(len(ctx.exception.error_params["tables"].split("、")), len(CD_REQUIRED_TABLES))

    def test_get_current_user_returns_503(self):
        """get_current_user(含 require_perm 链路)同样被闸门拦住."""
        db = self._db()
        with self.assertRaises(AppException) as ctx:
            get_current_user(_req(), _creds(), db)
        self.assertEqual(ctx.exception.status_code, 503)

    def test_public_webhook_receive_returns_503(self):
        """公开 Webhook 接收端点(无登录,靠 token 鉴权)也拒绝:写入必然缺表."""
        db = self._db()
        with self.assertRaises(AppException) as ctx:
            asyncio.run(receive_webhook("tok", cast(Request, types.SimpleNamespace()), db))
        self.assertEqual(ctx.exception.status_code, 503)

    def test_webshell_verify_returns_503(self):
        """WebShell(最高危操作)走独立鉴权路径,同样被闸门拒绝:转 503 供 WS 干净关闭."""
        from backend.config import settings as cfg

        original = cfg.db_path
        cfg.db_path = self.db_path
        try:
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(_ws_verify("any-token"))
        finally:
            cfg.db_path = original
        self.assertEqual(ctx.exception.status_code, 503)
        # auto 模式的补表动作是"重启自动建表",不能告诉用户去导入 MySQL 脚本
        self.assertIn("重启", str(ctx.exception.detail))
        self.assertNotIn("init_mysql.sql", str(ctx.exception.detail))

    def test_webshell_hint_follows_manual_provision(self):
        """manual 模式下 WebShell 提示必须改口:SQLite 是本地库,不能叫用户去导 MySQL 脚本."""
        from backend.config import settings as cfg

        _write_contract(self.db_path, enabled=1, provision="manual")
        original = cfg.db_path
        cfg.db_path = self.db_path
        try:
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(_ws_verify("any-token"))
        finally:
            cfg.db_path = original
        self.assertEqual(ctx.exception.status_code, 503)
        detail = str(ctx.exception.detail)
        self.assertIn("manual", detail)
        self.assertIn("Devops-Glue", detail)
        self.assertNotIn("init_mysql.sql", detail, "SQLite 用户不该被指去导入 MySQL 脚本")

    def test_missing_token_still_401(self):
        """没带 token 依旧是'请先登录'401:部署问题不该掩盖未认证语义."""
        db = self._db()
        with self.assertRaises(HTTPException) as ctx:
            verify_token(_req(), None, db)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_not_enabled_blocks_webhook_with_its_own_error_key(self):
        """未启用(而非未建全)时公开 Webhook 报 cd_not_enabled -- 两种 503 不许混."""
        _write_contract(self.db_path, with_row=False)  # 清空契约行 = 回到"从未启用"
        db = self._db()
        with self.assertRaises(AppException) as ctx:
            asyncio.run(receive_webhook("tok", cast(Request, types.SimpleNamespace()), db))
        self.assertEqual(ctx.exception.error_key, "errors.cd_not_enabled")


class TestDatabaseUnavailableIs503(unittest.TestCase):
    """库不可用(建不了表 / 打不开库)必须 503 errors.db_unavailable,而不是裸 500.

    与'未启用 / 未部署'严格区分:前者要查环境(连接/权限/磁盘),后者去 CI 后台点开关或补表;
    混为一谈会把运维指向错误动作.清单自检抛的 RuntimeError 属代码缺陷,必须原样冒泡.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "test_gate_dbunavail.db")
        _write_contract(self.db_path, enabled=1, provision="auto")  # 已启用:只有这样才会走建表路径
        Database._tables_ensured = False

    def tearDown(self):
        Database._tables_ensured = False
        self._tmp.cleanup()

    def test_sqlite_create_table_failure_is_503(self):
        """SQLite 建表失败(库只读 / 磁盘满 / 被占用)→ 503 errors.db_unavailable(原先会漏成裸 500)."""
        db = Database(self.db_path)
        boom = sqlite3.OperationalError("attempt to write a readonly database")
        with patch.object(Database, "_ensure_cd_tables", side_effect=boom):
            with self.assertRaises(AppException) as ctx:
                cd_deploy_gate(db)
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.error_key, "errors.db_unavailable")
        self.assertEqual(ctx.exception.error_params["driver"], "sqlite")
        self.assertIn("readonly", ctx.exception.error_params["reason"])
        # 不能报成"未启用 / 未部署":那会把运维指向"点开关 / 补表脚本",而真凶是文件权限或磁盘
        self.assertNotIn(ctx.exception.error_key, ("errors.cd_not_enabled", "errors.cd_not_deployed"))

    def test_sqlite_open_failure_is_503(self):
        """库文件打不开(目录不存在 / 不可写)→ 503 errors.db_unavailable."""
        db = Database(self.db_path)
        boom = sqlite3.OperationalError("unable to open database file")
        with patch.object(Database, "_connect_sqlite", side_effect=boom):
            with self.assertRaises(AppException) as ctx:
                cd_deploy_gate(db)
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.error_key, "errors.db_unavailable")

    def test_inventory_selfcheck_error_is_not_masked(self):
        """清单自检的 RuntimeError(清单与 DDL 漂移 = 代码/配置缺陷)必须原样冒泡,不许被伪装成 503."""
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
        # 其它错误(如连接失败)不得被误判成"未部署"
        self.assertFalse(_is_missing_table_error(ConnectionError("connection refused")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
