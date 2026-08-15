"""数据库访问层 — SQLite / MySQL 双驱动，PHP 和 CD 共用。
- MySQL: DBUtils 连接池，事务自动提交（成功）/ 回滚（异常）
- SQLite: 写入自动 commit，读取自动 close，WAL 模式
"""

import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from backend.config import settings

_Q_MARK_RE = re.compile(r"(?<!')\?(?!')")  # 匹配不在引号内的 ?


def _dict_row_factory(cursor, row):
    """SQLite row_factory：返回普通 dict（支持 .get()），兼容 pymysql DictCursor"""
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}


class _MysqlWrapper:
    """pymysql 包装——提供 sqlite3 风格的 execute/commit + context manager。
    退出时自动 commit（成功）或 rollback（异常），然后归还连接到池。
    """

    def __init__(self, conn, pool=None):
        self._conn = conn
        self._pool = pool
        self._exc_type = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
                self._exc_type = exc_type
        finally:
            self._conn.close()
        return False

    def execute(self, sql, params=None):
        cur = self._conn.cursor()
        # 只替换 SQL 占位符中的 ?，避免字符串字面量中的 ? 被误替换
        cur.execute(_Q_MARK_RE.sub("%s", sql), params or ())
        return cur

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


class _SqliteWrapper:
    """sqlite3 包装——提供与 _MysqlWrapper 一致的 execute/commit/rollback/close 接口。
    __enter__ 返回 self（与 _MysqlWrapper 一致），由 conn() 的 except/else/finally 统一管理生命周期。
    """

    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        if exc_type is None:
            self._conn.commit()
        self._conn.close()
        return False

    def execute(self, sql, params=None):
        return self._conn.execute(sql, params or ())

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


class Database:
    """统一数据库连接 — 无独立数据库，完全跟随 Devops-Glue API。
    SQLite 模式：自动建表。
    MySQL  模式：请先执行 database/init_mysql.sql 建表，应用只建索引。使用 DBUtils 连接池。
    启动时校验 ci_pipeline_tags 表是否存在，不存在则报错（数据库指向错误）。
    """

    DRIVERS = ("sqlite", "mysql")
    _tables_ensured = False  # 类变量：建表只执行一次
    _pool = None  # MySQL 连接池实例（延迟初始化）

    def __init__(self, db_path: str = ""):
        self._driver = settings.db_driver
        if self._driver not in self.DRIVERS:
            raise RuntimeError(
                f"DB_DRIVER 必须设为 sqlite 或 mysql，当前: {self._driver or '未设置'}"
            )
        self._path = Path(db_path or settings.db_path)
        self._validate_shared_db()

    def _init_pool(self):
        """延迟初始化 MySQL 连接池（避免 import 时连接失败阻塞启动）"""
        if self._driver != "mysql":
            return
        try:
            import pymysql
            from dbutils.pooled_db import PooledDB

            if Database._pool is None:
                Database._pool = PooledDB(
                    creator=pymysql,
                    maxconnections=settings.db_pool_max,
                    mincached=settings.db_pool_min,
                    blocking=True,
                    ping=1,  # 取出连接前先 ping 检测有效性
                    host=settings.db_host,
                    port=settings.db_port,
                    user=settings.db_user,
                    password=settings.db_pass,
                    database=settings.db_name,
                    charset="utf8mb4",
                    cursorclass=pymysql.cursors.DictCursor,
                )
        except ImportError:
            raise RuntimeError(
                "MySQL 模式需要安装 dbutils: pip install dbutils"
            )

    def _validate_shared_db(self):
        """校验数据库是否为 php_api 的共享数据库。"""
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
                f"数据库连接失败，请确保和 Devops-Glue API使用同一数据库实例。"
                f"当前驱动: {self._driver}，错误: {e}"
            )
        if not exists:
            raise RuntimeError(
                f"未找到 ci_pipeline_tags 表。cd_service 无独立数据库，必须和 Devops-Glue共用同一数据库实例。"
                f"请检查 DB_DRIVER（当前: {self._driver}）和连接配置是否与 Devops-Glue 一致。"
            )

    @contextmanager
    def conn(self):
        """获取数据库连接（context manager，退出时自动 commit/rollback + close/归还池）
        始终返回 wrapper 对象（统一 .execute/.commit/.rollback/.close 接口）。"""
        if self._driver == "mysql":
            self._init_pool()
            pool = Database._pool
            if pool is None:
                raise RuntimeError("MySQL 连接池初始化失败")
            raw = pool.connection()
            wrapper = _MysqlWrapper(raw)
        else:
            raw = self._connect_sqlite()
            wrapper = _SqliteWrapper(raw)

        if not Database._tables_ensured and self._driver == "sqlite":
            self._ensure_cd_tables(raw)
            try:
                raw.execute("ALTER TABLE admin_users ADD COLUMN role VARCHAR(32) DEFAULT 'admin'")
            except Exception:
                pass
            raw.commit()
            Database._tables_ensured = True

        try:
            yield wrapper
        except Exception:
            wrapper.rollback()
            raise
        else:
            wrapper.commit()
        finally:
            wrapper.close()

    # ── SQLite ──

    def _connect_sqlite(self):
        conn = sqlite3.connect(str(self._path))
        conn.row_factory = _dict_row_factory
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
            auth_type VARCHAR(20) DEFAULT 'password',
            password VARCHAR(255) DEFAULT '',
            ssh_key TEXT DEFAULT '',
            tags VARCHAR(255) DEFAULT '',
            created_at TEXT DEFAULT ({NOW})
        )""")
        try: conn.execute("ALTER TABLE cd_servers ADD COLUMN password VARCHAR(255) DEFAULT ''")
        except: pass
        try: conn.execute("ALTER TABLE cd_servers ADD COLUMN ssh_key TEXT DEFAULT ''")
        except: pass
        try: conn.execute("ALTER TABLE cd_servers ADD COLUMN tags VARCHAR(255) DEFAULT ''")
        except: pass
        try: conn.execute("ALTER TABLE cd_servers ADD COLUMN auth_type VARCHAR(20) DEFAULT 'password'")
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
            triggered_by VARCHAR(64) DEFAULT '',
            created_at TEXT DEFAULT ({NOW})
        )""")
        try: conn.execute("ALTER TABLE cd_deploy_logs ADD COLUMN deploy_id INTEGER DEFAULT 0")
        except: pass
        try: conn.execute("ALTER TABLE cd_deploy_logs ADD COLUMN triggered_by VARCHAR(64) DEFAULT ''")
        except: pass

        conn.execute(f"""CREATE TABLE IF NOT EXISTS cd_bots (
            id {PK},
            name VARCHAR(255) UNIQUE,
            type VARCHAR(32) DEFAULT 'custom',
            webhook_url TEXT NOT NULL,
            template TEXT DEFAULT '',
            created_at TEXT DEFAULT ({NOW})
        )""")
        try: conn.execute("ALTER TABLE cd_bots ADD COLUMN template TEXT DEFAULT ''")
        except: pass

        # 镜像仓库缓存表
        conn.execute(f"""CREATE TABLE IF NOT EXISTS cd_registry_repositories (
            id {PK},
            project_name VARCHAR(255) NOT NULL,
            repo_name VARCHAR(512) NOT NULL,
            UNIQUE(project_name, repo_name)
        )""")
        conn.execute(f"""CREATE TABLE IF NOT EXISTS cd_registry_artifacts (
            id {PK},
            repo_id INTEGER,
            tag VARCHAR(255),
            digest VARCHAR(128),
            size_bytes BIGINT DEFAULT 0,
            push_time TEXT DEFAULT '',
            pull_time TEXT DEFAULT '',
            scan_status VARCHAR(32) DEFAULT '',
            scan_severity VARCHAR(16) DEFAULT '',
            vuln_critical INTEGER DEFAULT 0,
            vuln_high INTEGER DEFAULT 0,
            vuln_medium INTEGER DEFAULT 0,
            vuln_low INTEGER DEFAULT 0,
            vuln_fixable INTEGER DEFAULT 0,
            last_sync TEXT DEFAULT '',
            UNIQUE(repo_id, tag, digest),
            FOREIGN KEY(repo_id) REFERENCES cd_registry_repositories(id) ON DELETE CASCADE
        )""")

        # 系统配置表
        conn.execute("""CREATE TABLE IF NOT EXISTS cd_config (
            key_name VARCHAR(128) PRIMARY KEY,
            value TEXT NOT NULL
        )""")

        # 告警规则表
        conn.execute(f"""CREATE TABLE IF NOT EXISTS cd_alert_rules (
            id {PK},
            name VARCHAR(255),
            target_type VARCHAR(32) DEFAULT 'system',
            resource_type VARCHAR(32) NOT NULL,
            server_ids VARCHAR(2048) DEFAULT '',
            threshold INTEGER DEFAULT 80,
            bot_id INTEGER DEFAULT 0,
            template TEXT DEFAULT '',
            enabled INTEGER DEFAULT 1,
            cooldown_minutes INTEGER DEFAULT 10,
            duration_minutes INTEGER DEFAULT 0,
            last_alert_at TEXT DEFAULT '',
            created_at TEXT DEFAULT ({NOW})
        )""")

        # 自定义监控项表
        conn.execute(f"""CREATE TABLE IF NOT EXISTS cd_custom_monitors (
            id {PK},
            name VARCHAR(255) NOT NULL,
            command TEXT NOT NULL,
            output_format VARCHAR(32) DEFAULT 'auto',
            description TEXT DEFAULT '',
            server_ids VARCHAR(2048) DEFAULT '',
            enabled INTEGER DEFAULT 1,
            created_at TEXT DEFAULT ({NOW})
        )""")

        # 自定义监控指标定义表
        conn.execute(f"""CREATE TABLE IF NOT EXISTS cd_custom_monitor_metrics (
            id {PK},
            monitor_id INTEGER NOT NULL,
            name VARCHAR(255) NOT NULL,
            field_key VARCHAR(255) NOT NULL,
            unit VARCHAR(32) DEFAULT '',
            sort_order INTEGER DEFAULT 0,
            FOREIGN KEY(monitor_id) REFERENCES cd_custom_monitors(id) ON DELETE CASCADE
        )""")
        try: conn.execute("CREATE INDEX IF NOT EXISTS idx_metrics_monitor ON cd_custom_monitor_metrics(monitor_id)")
        except: pass

        # Webhook 接收配置表
        conn.execute(f"""CREATE TABLE IF NOT EXISTS cd_webhooks (
            id {PK},
            name VARCHAR(255) UNIQUE,
            token VARCHAR(64) UNIQUE NOT NULL,
            bot_id INTEGER DEFAULT 0,
            enabled INTEGER DEFAULT 1,
            created_at TEXT DEFAULT ({NOW})
        )""")

        # Webhook 事件记录表
        conn.execute(f"""CREATE TABLE IF NOT EXISTS cd_webhook_events (
            id {PK},
            webhook_id INTEGER NOT NULL,
            payload TEXT,
            received_at TEXT DEFAULT ({NOW}),
            forwarded INTEGER DEFAULT 0,
            forwarded_at TEXT DEFAULT ''
        )""")

        # ── 自动迁移：补充已有表缺失的列 ──
        migrations = [
            ("cd_custom_monitors", "output_format", "VARCHAR(32) DEFAULT 'auto'"),
        ]
        for tbl, col, col_def in migrations:
            try:
                conn.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {col_def}")
            except Exception:
                pass  # 列已存在

        self._ensure_indexes(conn)
        conn.commit()

    # ── 索引（SQLite / MySQL 共用）──

    def _ensure_indexes(self, conn):
        for name, tbl, col in [
            ("idx_cdl_project",       "cd_deploy_logs",        "project"),
            ("idx_cdl_created",       "cd_deploy_logs",        "created_at"),
            ("idx_cdl_deploy_id",     "cd_deploy_logs",        "deploy_id"),
            ("idx_cdl_project_tag_status", "cd_deploy_logs",   "project, tag, status"),
            ("idx_pt_project",        "ci_pipeline_tags",      "project"),
            ("idx_pt_created",        "ci_pipeline_tags",      "created_at"),
            ("idx_jgm_path",          "ci_job_git_map",        "current_path"),
            ("idx_cdr_repo_id",       "cd_registry_artifacts", "repo_id"),
            ("idx_cds_type",          "cd_servers",            "type"),
            ("idx_cdr_enabled",       "cd_alert_rules",        "enabled"),
            ("idx_cdr_created",       "cd_alert_rules",        "created_at"),
            ("idx_cdm_enabled",       "cd_custom_monitors",    "enabled"),
            ("idx_cdm_created",       "cd_custom_monitors",    "created_at"),
            ("idx_cwh_enabled",       "cd_webhooks",           "enabled"),
            ("idx_we_webhook",        "cd_webhook_events",     "webhook_id"),
            ("idx_we_received",       "cd_webhook_events",     "received_at"),
        ]:
            try: conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {tbl}({col})")
            except: pass
        conn.commit()
