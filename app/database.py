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
    """统一数据库连接，CD 表自动建"""

    DRIVERS = ("sqlite", "mysql")

    def __init__(self, db_path: str = ""):
        self._driver = settings.db_driver
        if self._driver not in self.DRIVERS:
            raise RuntimeError(
                f"DB_DRIVER 必须设为 sqlite 或 mysql，当前: {self._driver or '未设置'}"
            )
        self._path = Path(db_path or settings.db_path)

    def conn(self):
        """获取连接（统一 execute/commit/close 接口）"""
        if self._driver == "mysql":
            raw = self._connect_mysql()
            conn = _MysqlWrapper(raw)
        else:
            conn = self._connect_sqlite()
        self._ensure_cd_tables(conn)
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

    # ── 建表 ──

    def _ensure_cd_tables(self, conn):
        is_mysql = self._driver == "mysql"
        PK   = "INT AUTO_INCREMENT PRIMARY KEY" if is_mysql else "INTEGER PRIMARY KEY AUTOINCREMENT"
        NOW  = "NOW()" if is_mysql else "datetime('now','localtime')"
        ENG  = " ENGINE=InnoDB DEFAULT CHARSET=utf8mb4" if is_mysql else ""

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
        ){ENG}""")
        try: conn.execute("ALTER TABLE cd_servers ADD COLUMN password VARCHAR(255) DEFAULT ''")
        except: pass
        try: conn.execute("ALTER TABLE cd_servers ADD COLUMN tags VARCHAR(255) DEFAULT ''")
        except: pass

        conn.execute(f"""CREATE TABLE IF NOT EXISTS cd_deploy_logs (
            id {PK},
            project VARCHAR(255),
            tag VARCHAR(255),
            image VARCHAR(512),
            deploy_type VARCHAR(32),
            target VARCHAR(255),
            status VARCHAR(32),
            output TEXT,
            created_at TEXT DEFAULT ({NOW})
        ){ENG}""")

        conn.execute(f"""CREATE TABLE IF NOT EXISTS cd_bots (
            id {PK},
            name VARCHAR(255) UNIQUE,
            type VARCHAR(32) DEFAULT 'custom',
            webhook_url TEXT NOT NULL,
            created_at TEXT DEFAULT ({NOW})
        ){ENG}""")

        # ── 索引 ──
        for name, tbl, col in [
            ("idx_cdl_project", "cd_deploy_logs", "project"),
            ("idx_cdl_created", "cd_deploy_logs", "created_at"),
            ("idx_pt_project", "ci_pipeline_tags", "project"),
            ("idx_pt_created", "ci_pipeline_tags", "created_at"),
            ("idx_jgm_path", "ci_job_git_map", "current_path"),
        ]:
            try: conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {tbl}({col})")
            except: pass

        conn.commit()
