"""数据库层单元测试 — 验证 conn() context manager 事务行为"""

import sqlite3
import pytest
from backend.database import Database, _SqliteWrapper, _MysqlWrapper


class TestDatabaseInit:
    """Database 初始化（绕过 _validate_shared_db，seed_db fixture 已做）"""

    def test_driver_sqlite(self, seed_db):
        """确认 SQLite 驱动模式能正常初始化"""
        from backend.database import Database
        # seed_db fixture 已创建 ci_pipeline_tags 表
        db = Database()
        assert db._driver == "sqlite"

    def test_driver_invalid(self, monkeypatch):
        """无效驱动应抛异常"""
        import os
        from backend.config import settings
        monkeypatch.setattr(settings, "db_driver", "postgres")
        with pytest.raises(RuntimeError, match="DB_DRIVER"):
            Database(db_path="/tmp/test.db")


class TestSqliteConnContext:
    """SQLite conn() context manager"""

    def test_conn_yields_connection(self, seed_db):
        """conn() 应返回可用的 SQLite 连接"""
        db = Database()
        with db.conn() as conn:
            assert isinstance(conn, sqlite3.Connection)
            # 可执行 SQL
            conn.execute("SELECT 1").fetchall()

    def test_auto_commit_on_success(self, seed_db):
        """正常退出时自动 commit"""
        db = Database()
        with db.conn() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS _test_commit (id INTEGER)")
            conn.execute("INSERT INTO _test_commit VALUES (1)")

        # 重新打开确认写入持久化
        with db.conn() as conn2:
            rows = conn2.execute("SELECT * FROM _test_commit").fetchall()
            assert len(rows) == 1
            assert rows[0]["id"] == 1

    def test_auto_close_on_exception(self, seed_db):
        """异常时不应 commit，连接应关闭"""
        db = Database()
        with db.conn() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS _test_rollback (id INTEGER)")

        try:
            with db.conn() as conn:
                conn.execute("INSERT INTO _test_rollback VALUES (1)")
                raise ValueError("模拟业务异常")
        except ValueError:
            pass

        # 确认未写入
        with db.conn() as conn:
            rows = conn.execute("SELECT * FROM _test_rollback").fetchall()
            assert len(rows) == 0

    def test_cd_tables_created(self, seed_db):
        """SQLite 模式下自动创建所有 CD 表"""
        db = Database()
        expected_tables = [
            "cd_servers", "cd_deploy_logs", "cd_bots",
            "cd_registry_repositories", "cd_registry_artifacts",
            "cd_config", "cd_alert_rules",
            "cd_custom_monitors", "cd_custom_monitor_metrics",
        ]
        with db.conn() as conn:
            result = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            table_names = [r["name"] for r in result]
            for t in expected_tables:
                assert t in table_names, f"表 {t} 未创建"


class TestSqliteWrapper:
    """_SqliteWrapper 直接测试"""

    def test_enter_returns_conn(self, tmp_db_path):
        conn = sqlite3.connect(tmp_db_path)
        wrapper = _SqliteWrapper(conn)
        with wrapper as c:
            assert c is conn

    def test_exit_commits_and_closes(self, tmp_db_path):
        """验证 __exit__ 自动 commit 然后 close"""
        conn = sqlite3.connect(tmp_db_path)
        wrapper = _SqliteWrapper(conn)
        with wrapper as c:
            c.execute("CREATE TABLE _test (id INTEGER)")

        # __exit__ 已自动 commit + close
        # 重新连接验证写入
        conn2 = sqlite3.connect(tmp_db_path)
        conn2.row_factory = sqlite3.Row
        rows = conn2.execute("SELECT * FROM _test").fetchall()
        assert len(rows) == 0  # 表存在，没有插入数据
        conn2.close()


class TestTableMigration:
    """自动迁移测试"""

    def test_add_missing_columns(self, seed_db):
        db = Database()
        # _ensure_cd_tables 应该执行 ALTER TABLE ADD COLUMN...
        with db.conn() as conn:
            # 确认 cd_custom_monitors 有 output_format 列
            cols = conn.execute("PRAGMA table_info(cd_custom_monitors)").fetchall()
            col_names = [c["name"] for c in cols]
            assert "output_format" in col_names
