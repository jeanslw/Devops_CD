"""数据库访问层 — SQLite / MySQL 双驱动，PHP 和 CD 共用"""

import sqlite3
from pathlib import Path
from app.config import settings


class _MysqlWrapper:
    """pymysql 包装——提供 sqlite3 风格的 execute/commit/close"""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=None):
        cur = self._conn.cursor()
        cur.execute(sql.replace("?", "%s"), params or ())
        return cur

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


class Database:
    """统一数据库连接 — 无独立数据库，完全跟随 Devops-Glue API（php_api）。
    SQLite 模式：自动建表。
    MySQL  模式：请先执行 database/init_mysql.sql 建表，应用只建索引。
    启动时校验 ci_pipeline_tags 表是否存在，不存在则报错（数据库指向错误）。
    """

    DRIVERS = ("sqlite", "mysql")
    _tables_ensured = False  # 类变量：建表只执行一次

    def __init__(self, db_path: str = ""):
        self._driver = settings.db_driver
        if self._driver not in self.DRIVERS:
            raise RuntimeError(
                f"DB_DRIVER 必须设为 sqlite 或 mysql，当前: {self._driver or '未设置'}"
            )
        self._path = Path(db_path or settings.db_path)
        self._validate_shared_db()

    def _validate_shared_db(self):
        """校验数据库是否为 php_api 的共享数据库。
        ci_pipeline_tags 是 php_api 维护的核心表，不存在说明数据库指向错误。
        """
        try:
            if self._driver == "mysql":
                import pymysql
                raw = pymysql.connect(
                    host=settings.db_host, port=settings.db_port,
                    user=settings.db_user, password=settings.db_pass,
                    database=settings.db_name, charset="utf8mb4",
                )
                cur = raw.cursor()
                cur.execute("SHOW TABLES LIKE 'ci_pipeline_tags'")
                exists = cur.fetchone() is not None
                cur.close()
                raw.close()
            else:
                conn = sqlite3.connect(str(self._path))
                cur = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='ci_pipeline_tags'"
                )
                exists = cur.fetchone() is not None
                conn.close()
        except Exception as e:
            raise RuntimeError(
                f"数据库连接失败，请确保和 Devops-Glue API（php_api）使用同一数据库实例。"
                f"当前驱动: {self._driver}，错误: {e}"
            )
        if not exists:
            raise RuntimeError(
                f"未找到 ci_pipeline_tags 表。cd_service 无独立数据库，必须和 php_api 共用同一数据库实例。"
                f"请检查 DB_DRIVER（当前: {self._driver}）和连接配置是否与 php_api 一致。"
            )

    def conn(self):
        """获取连接（统一 execute/commit/close 接口）"""
        if self._driver == "mysql":
            raw = self._connect_mysql()
            conn = _MysqlWrapper(raw)
        else:
            conn = self._connect_sqlite()
        if not Database._tables_ensured and self._driver == "sqlite":
            self._ensure_cd_tables(conn)
            Database._tables_ensured = True
        return conn

    # ── SQLite ──

    def _connect_sqlite(self):
        conn = sqlite3.connect(str(self._path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    # ── MySQL ──

    def _connect_mysql(self):
        import pymysql
        conn = pymysql.connect(
            host=settings.db_host,
            port=settings.db_port,
            user=settings.db_user,
            password=settings.db_pass,
            database=settings.db_name,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
        )
        return conn

    # ── SQLite 自动建表 ──

    def _ensure_cd_tables(self, conn):
        """SQLite 模式：自动创建 CD 表 + 索引"""
        PK  = "INTEGER PRIMARY KEY AUTOINCREMENT"
        NOW = "datetime('now','localtime')"

        conn.execute(f"""CREATE TABLE IF NOT EXISTS cd_servers (
            id {PK},
            name VARCHAR(255) UNIQUE,
            host VARCHAR(255),
            port INTEGER DEFAULT 22,
            user VARCHAR(64) DEFAULT 'root',
            type VARCHAR(32) DEFAULT 'ssh',
            password VARCHAR(255) DEFAULT '',
            tags VARCHAR(255) DEFAULT '',
            created_at TEXT DEFAULT ({NOW})
        )""")
        try: conn.execute("ALTER TABLE cd_servers ADD COLUMN password VARCHAR(255) DEFAULT ''")
        except: pass
        try: conn.execute("ALTER TABLE cd_servers ADD COLUMN tags VARCHAR(255) DEFAULT ''")
        except: pass

        conn.execute(f"""CREATE TABLE IF NOT EXISTS cd_deploy_logs (
            id {PK},
            deploy_id INTEGER DEFAULT 0,
            project VARCHAR(255),
            tag VARCHAR(255),
            image VARCHAR(512),
            deploy_type VARCHAR(32),
            target VARCHAR(255),
            status VARCHAR(32),
            output TEXT,
            created_at TEXT DEFAULT ({NOW})
        )""")
        try: conn.execute("ALTER TABLE cd_deploy_logs ADD COLUMN deploy_id INTEGER DEFAULT 0")
        except: pass

        conn.execute(f"""CREATE TABLE IF NOT EXISTS cd_bots (
            id {PK},
            name VARCHAR(255) UNIQUE,
            type VARCHAR(32) DEFAULT 'custom',
            webhook_url TEXT NOT NULL,
            created_at TEXT DEFAULT ({NOW})
        )""")

        self._ensure_indexes(conn)
        conn.commit()

    # ── 索引（SQLite / MySQL 共用）──

    def _ensure_indexes(self, conn):
        for name, tbl, col in [
            ("idx_cdl_project", "cd_deploy_logs", "project"),
            ("idx_cdl_created", "cd_deploy_logs", "created_at"),
            ("idx_pt_project", "ci_pipeline_tags", "project"),
            ("idx_pt_created", "ci_pipeline_tags", "created_at"),
            ("idx_jgm_path",   "ci_job_git_map",  "current_path"),
        ]:
            try: conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {tbl}({col})")
            except: pass
        conn.commit()
