"""数据库访问层 — SQLite / MySQL 双驱动，PHP 和 CD 共用。
- MySQL: DBUtils 连接池，事务自动提交（成功）/ 回滚（异常）
- SQLite: 写入自动 commit，读取自动 close，WAL 模式
"""

import re
import sqlite3
from contextlib import contextmanager, suppress
from pathlib import Path

from backend.config import settings
from backend.exceptions import AppException


class DatabaseUnavailableError(AppException):
    """数据库**当前不可用**（连不上 / 建不了表 / 库被锁或只读 / 磁盘满）—— 与「CD 表未部署」严格区分。

    两种 503 必须分清（提示与排查方向完全不同）：
      - 「清单里的表没建全」= **部署状态**问题 → 503 errors.cd_not_deployed（让人去补齐建表）；
      - 「库现在连不上/写不进」= **运行环境**问题 → 本异常 → 503 errors.db_unavailable
        （让人去查连接、配置、文件权限（SQLite 目录可写）、磁盘空间）。
    都返回 503，但 error_key 不同：混为一谈会把运维指向错误动作（重跑建表脚本，问题照旧）。

    继承 AppException：由 main.py 的统一异常处理器输出带 error_key/error_params 的 JSON（503），
    因此无论抛在依赖注入（get_db）还是路由内部，客户端拿到的都是结构化错误而不是裸 500。
    """

    def __init__(self, message: str, driver: str = "", reason: str = ""):
        super().__init__(
            message,
            status_code=503,
            error_key="errors.db_unavailable",
            error_params={"driver": driver, "reason": (reason or message)[:300]},
        )
        self.driver = driver


_Q_MARK_RE = re.compile(r"(?<!')\?(?!')")  # 匹配不在引号内的 ?

# ── CD 部署闸门：唯一表清单 ────────────────────────────────────────────────────
# 「CD 到底启用了没有」只看一件事：下面这份清单里的表是否**全部存在**（与清单张数无关，
# 以后加减表也是同一套判定）。这张清单就是闸门的分水岭：
#   - MySQL 模式：管理员手动导入 database/init_mysql.sql 建全这些表 = 启用 CD（应用绝不代为建表）；
#   - SQLite 模式：CD 表随库自有，_ensure_cd_tables() 首次连库时自动建全（结尾有清单自检）。
# 少任何一张都当作未部署 —— 半套 schema 放行只会让登录后的功能到处 500、甚至写坏数据，
# 所以抢在入口整体拒绝（见 backend.auth.cd_deploy_gate：登录 + 所有已持有会话的请求统一 503）。
#
# 增删一张 CD 表 = 只改三处（清单是唯一真相源；闸门、错误文案、张数全都自动跟随，代码不写死数字）：
#   1. 下面的 CD_REQUIRED_TABLES（唯一清单，顺序 = 错误提示里的展示顺序）
#   2. database/init_mysql.sql（MySQL 建表脚本：生产启用 CD 的唯一手段）
#   3. backend/database.py::_ensure_cd_tables（SQLite 自带建表；结尾自检会当场拦截漏改）
# 1/2 由 backend/tests/test_cd_deploy_gate.py 双向校验，3 由运行时自检兜底。
CD_REQUIRED_TABLES: tuple[str, ...] = (
    "cd_servers",
    "cd_deploy_logs",
    "cd_bots",
    "cd_registry_repositories",
    "cd_registry_artifacts",
    "cd_config",
    "cd_alert_rules",
    "cd_custom_monitors",
    "cd_custom_monitor_metrics",
    "cd_webhooks",
    "cd_webhook_events",
    "cd_approvals",
    "cd_approval_rules",
    "cd_sessions",
)

# 列出现存基础表：MySQL 走 information_schema（DATABASE() = 当前共享库），
# SQLite 走 sqlite_master。视图 v_glue_deploy_logs 是给 Glue 的契约视图、非 CD 自身表，
# 不参与闸门（故 MySQL 侧限定 BASE TABLE，两驱动语义一致）。
_CD_TABLE_LIST_SQL = {
    "mysql": (
        "SELECT table_name FROM information_schema.tables WHERE table_schema = DATABASE() AND table_type = 'BASE TABLE'"
    ),
    "sqlite": "SELECT name FROM sqlite_master WHERE type = 'table'",
}


def cd_deploy_hint(driver: str) -> str:
    """按驱动给出「怎么把清单建全」的正确动作。

    两个驱动的处置方式完全不同，提示必须说实话：SQLite 的 CD 表由本服务随库自动创建
    （叫它去导入 MySQL 脚本是错误指令），MySQL 才需要管理员手动导入建表脚本。
    """
    if driver == "sqlite":
        return "SQLite 模式：CD 表随库自动创建，重启服务即可补齐（或删除该 .db 文件重新生成）"
    return "MySQL 模式：请管理员执行 database/init_mysql.sql 建全清单中的 CD 表"


def _dict_row_factory(cursor, row):
    """SQLite row_factory：返回普通 dict（支持 .get()），兼容 pymysql DictCursor"""
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}


def _first_col_value(row) -> str:
    """取结果行第一列值（SQLite 自定义 row_factory 与 pymysql DictCursor 都是 dict）"""
    if isinstance(row, dict):
        return str(next(iter(row.values())))
    return str(row[0])


def _is_missing_table_error(exc: BaseException) -> bool:
    """判断异常是否为「表不存在」（跨 SQLite / MySQL）。

    只用于把「CD 表未部署」与「数据库真的不可用」区分开：前者给出可操作提示，
    后者仍按原样抛出，避免把连接故障误报成「未部署」。
    """
    if "no such table" in str(exc).lower():  # SQLite
        return True
    return getattr(exc, "args", (None,))[0] == 1146  # MySQL ER_NO_SUCH_TABLE


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
    SQLite 模式：自动建表（开发/单机场景，随 DB 文件一起生成）。
    MySQL  模式：绝不建表 —— 全部 CD 表由管理员手动执行 database/init_mysql.sql 建立，
    该手动导入即「启用 CD」的部署闸门，且必须把 CD_REQUIRED_TABLES 清单里的表建全（少一张 = 未部署），
    详见 docs/ADMIN_MANUAL_ZH.md。应用只对已存在的表做缺失列幂等迁移，
    表是否建全一律由 cd_missing_tables() / cd_deployed() 判定（清单即唯一真相源，不建表、不修补）。
    使用 DBUtils 连接池。
    启动时校验 ci_pipeline_artifacts 表是否存在，不存在则报错（数据库指向错误）。
    """

    DRIVERS = ("sqlite", "mysql")
    _tables_ensured = False  # 类变量：建表只执行一次
    _pool = None  # MySQL 连接池实例（延迟初始化）

    @property
    def driver(self) -> str:
        """当前驱动（sqlite / mysql）—— 供上层按驱动给出正确的运维提示（见 cd_deploy_hint）"""
        return self._driver

    def __init__(self, db_path: str = ""):
        self._driver = settings.db_driver
        if self._driver not in self.DRIVERS:
            raise RuntimeError(f"DB_DRIVER 必须设为 sqlite 或 mysql，当前: {self._driver or '未设置'}")
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
        except ImportError as e:
            raise RuntimeError("MySQL 模式需要安装 dbutils: pip install dbutils") from e

    def _validate_shared_db(self):
        """校验数据库是否为 php_api 的共享数据库。"""
        try:
            if self._driver == "mysql":
                import pymysql

                raw = pymysql.connect(
                    host=settings.db_host,
                    port=settings.db_port,
                    user=settings.db_user,
                    password=settings.db_pass,
                    database=settings.db_name,
                    charset="utf8mb4",
                )
                cur = raw.cursor()
                cur.execute("SHOW TABLES LIKE 'ci_pipeline_artifacts'")
                exists = cur.fetchone() is not None
                cur.close()
                raw.close()
            else:
                conn = sqlite3.connect(str(self._path))
                cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='ci_pipeline_artifacts'")
                exists = cur.fetchone() is not None
                conn.close()
        except Exception as e:
            # 连不上/取不到元数据 = 库当前不可用（503 errors.db_unavailable），不是 500 程序错误
            raise DatabaseUnavailableError(
                f"数据库连接失败，请确保和 Devops-Glue API使用同一数据库实例。当前驱动: {self._driver}，错误: {e}",
                self._driver,
                str(e),
            ) from e
        if not exists:
            raise DatabaseUnavailableError(
                f"未找到 ci_pipeline_artifacts 表。cd_service 无独立数据库，必须和 Devops-Glue共用同一数据库实例。"
                f"请检查 DB_DRIVER（当前: {self._driver}）和连接配置是否与 Devops-Glue 一致。",
                self._driver,
            )

    def cd_missing_tables(self) -> list[str]:
        """返回共享库中缺失的 CD 表名（空列表 = 表建全 = CD 已部署）。

        「建全」以 CD_REQUIRED_TABLES 为准（与 database/init_mysql.sql、SQLite 建表逻辑一致）：
        少任何一张都算未部署 —— 半套 schema 不该放行登录（登录后处处 500 更糟），
        判定由 backend.auth.cd_deploy_gate 统一消费（登录 + 已持有会话的请求都拒绝）。

        缺失表属于「部署状态」，不是「数据库故障」：连接不可用/建表失败等一律抛
        DatabaseUnavailableError（503 errors.db_unavailable），避免把库宕机误报成「未部署」而
        误导运维去重跑建表脚本 —— 两种 503 的 error_key 不同，客户端提示的排查动作也不同。
        """
        try:
            with self.conn() as conn:
                rows = conn.execute(_CD_TABLE_LIST_SQL[self._driver]).fetchall()
        except Exception as e:
            if _is_missing_table_error(e):
                # 连元数据表都查不到（如 MySQL 1146）：无法证明 CD 已部署，按未部署处理
                return list(CD_REQUIRED_TABLES)
            raise
        existing = {_first_col_value(r) for r in rows}
        return [t for t in CD_REQUIRED_TABLES if t not in existing]

    def cd_deployed(self) -> bool:
        """CD 是否已部署（= CD_REQUIRED_TABLES 清单里的表全部存在，与清单张数无关）。

        SQLite 模式随库自动建全（_ensure_cd_tables 建表 + 结尾自检），因此正常恒为 True；
        但运行期若被人为 DROP 掉某张表，闸门立刻拒绝（不静默补救），重启服务后自动建全。
        MySQL 模式未导入（或导入不全）时为 False，此时登录与任何已持有会话的请求都会被拒
        （503 errors.cd_not_deployed）。
        """
        return not self.cd_missing_tables()

    @contextmanager
    def conn(self):
        """获取数据库连接（context manager，退出时自动 commit/rollback + close/归还池）
        始终返回 wrapper 对象（统一 .execute/.commit/.rollback/.close 接口）。"""
        if self._driver == "mysql":
            self._init_pool()
            pool = Database._pool
            if pool is None:
                raise DatabaseUnavailableError("MySQL 连接池初始化失败", "mysql")
            try:
                raw = pool.connection()
            except Exception as e:
                # 取不到连接（网络/账号/连接数打满）= 库当前不可用 → 503，而不是裸 500
                raise DatabaseUnavailableError(f"MySQL 取连接失败：{e}", "mysql", str(e)) from e
            wrapper = _MysqlWrapper(raw)
        else:
            try:
                raw = self._connect_sqlite()
            except sqlite3.Error as e:
                # 库文件打不开（目录不存在/不可写/被占用）= 环境故障 → 503 库不可用（不是 500）
                raise DatabaseUnavailableError(
                    f"SQLite 打开数据库文件失败（{self._path}）：{e}", "sqlite", str(e)
                ) from e
            wrapper = _SqliteWrapper(raw)

        if not Database._tables_ensured:
            # 维护 CD 自有表：SQLite 走完整建表+迁移（开发/单机模式）；MySQL **绝不建表** ——
            # CD 表由管理员手动导入 database/init_mysql.sql 建立，该导入即「启用 CD」的部署闸门，
            # 这里只对已存在的表做缺失列幂等迁移（否则已有库新增列如 cd_approvals.scheduled_at
            # 的迁移永远跑不到）；表不存在时迁移被 suppress 跳过，即零副作用。
            # admin_users/roles 等共享表 schema 由 Devops-Glue 统一管理，CD 不得改动。
            # 必须传 wrapper（统一 .execute/.commit 接口）：MySQL 池化 raw 连接无 execute。
            try:
                if self._driver == "mysql":
                    self._ensure_mysql_migrations(wrapper)
                else:
                    self._ensure_cd_tables(wrapper)
            except sqlite3.Error as e:
                # 建表写不进去（只读 / 磁盘满 / 被占用）：CD 表当下不可用 → 503 errors.db_unavailable。
                # 只吞 sqlite3.Error：_ensure_cd_tables 末尾的清单自检抛 RuntimeError（清单与 DDL 漂移，
                # 属代码/配置缺陷）必须原样冒泡，不能被伪装成「服务暂时不可用」而掩盖。
                wrapper.close()  # 失败也必须释放连接（Windows 上未关闭的句柄会锁住库文件）
                raise DatabaseUnavailableError(f"自动建表失败（{self._driver}）：{e}", self._driver, str(e)) from e
            except Exception:
                wrapper.close()  # 异常路径一律不得泄漏连接：否则库文件被锁、下次启动更糟
                raise
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

    # ── MySQL 幂等迁移 ──

    def _ensure_mysql_migrations(self, conn):
        """MySQL 模式：绝不建表/建索引 —— 所有 CD 表由管理员手动执行
        database/init_mysql.sql 建立，该手动导入就是「启用 CD」的部署闸门。

        这里只对**已存在**的表做缺失列幂等迁移（否则已有库新增列如
        cd_approvals.scheduled_at 的迁移永远跑不到；此前迁移只在 SQLite 模式执行）。
        表不存在时 ALTER 会报错，由 suppress 吞掉 —— 即「未启用 CD」时本方法零副作用。
        重复加列报 MySQL 1060 duplicate column，同样 suppress 掉即可。
        共享表（admin_users/roles 等）schema 由 Devops-Glue 统一管理，CD 不得改动。
        注意：MySQL TEXT 列不支持字面 DEFAULT，统一用 VARCHAR。
        """
        migrations = [
            ("cd_deploy_logs", "approval_id", "INT DEFAULT 0"),
            ("cd_deploy_logs", "runner", "VARCHAR(128) DEFAULT ''"),
            ("cd_deploy_logs", "heartbeat_at", "VARCHAR(32) DEFAULT ''"),
            ("cd_approvals", "deploy_id", "INT DEFAULT 0"),
            ("cd_approvals", "approved_at", "VARCHAR(32) DEFAULT ''"),
            ("cd_approvals", "scheduled_at", "VARCHAR(32) DEFAULT ''"),
            ("cd_approvals", "updated_at", "VARCHAR(32) DEFAULT ''"),
            ("cd_custom_monitors", "output_format", "VARCHAR(32) DEFAULT 'auto'"),
            ("cd_registry_artifacts", "artifact_id", "INT DEFAULT 0"),
            ("cd_registry_artifacts", "artifact_digest", "VARCHAR(128) DEFAULT ''"),
        ]
        for tbl, col, col_def in migrations:
            with suppress(Exception):
                conn.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {col_def}")
        conn.commit()

    # ── SQLite 自动建表 ──

    def _ensure_cd_tables(self, conn):
        """SQLite 模式：自动创建 CD 表 + 索引"""
        PK = "INTEGER PRIMARY KEY AUTOINCREMENT"
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
        with suppress(Exception):
            conn.execute("ALTER TABLE cd_servers ADD COLUMN password VARCHAR(255) DEFAULT ''")
        with suppress(Exception):
            conn.execute("ALTER TABLE cd_servers ADD COLUMN ssh_key TEXT DEFAULT ''")
        with suppress(Exception):
            conn.execute("ALTER TABLE cd_servers ADD COLUMN tags VARCHAR(255) DEFAULT ''")
        with suppress(Exception):
            conn.execute("ALTER TABLE cd_servers ADD COLUMN auth_type VARCHAR(20) DEFAULT 'password'")

        conn.execute(f"""CREATE TABLE IF NOT EXISTS cd_deploy_logs (
            id {PK},
            project VARCHAR(255),
            tag VARCHAR(255),
            image VARCHAR(512),
            deploy_type VARCHAR(32),
            target VARCHAR(255),
            status VARCHAR(32),
            output TEXT,
            triggered_by VARCHAR(64) DEFAULT '',
            deploy_note VARCHAR(512) DEFAULT '',
            duration_ms INTEGER DEFAULT 0,
            stage_times TEXT DEFAULT '',
            approval_id INTEGER DEFAULT 0,
            created_at TEXT DEFAULT ({NOW})
        )""")
        with suppress(Exception):
            conn.execute("ALTER TABLE cd_deploy_logs ADD COLUMN triggered_by VARCHAR(64) DEFAULT ''")
        with suppress(Exception):
            conn.execute("ALTER TABLE cd_deploy_logs ADD COLUMN deploy_note VARCHAR(512) DEFAULT ''")
        with suppress(Exception):
            conn.execute("ALTER TABLE cd_deploy_logs ADD COLUMN duration_ms INTEGER DEFAULT 0")
        with suppress(Exception):
            conn.execute("ALTER TABLE cd_deploy_logs ADD COLUMN stage_times TEXT DEFAULT ''")
        with suppress(Exception):
            conn.execute("ALTER TABLE cd_deploy_logs ADD COLUMN lock_key VARCHAR(255)")
        with suppress(Exception):
            conn.execute("ALTER TABLE cd_deploy_logs ADD COLUMN params_json TEXT DEFAULT ''")
        with suppress(Exception):
            conn.execute("ALTER TABLE cd_deploy_logs ADD COLUMN rollback_type VARCHAR(32) DEFAULT 'manual'")
        with suppress(Exception):
            conn.execute("ALTER TABLE cd_deploy_logs ADD COLUMN artifact_id INTEGER DEFAULT 0")
        with suppress(Exception):
            conn.execute("ALTER TABLE cd_deploy_logs ADD COLUMN artifact_digest VARCHAR(128) DEFAULT ''")
        with suppress(Exception):
            conn.execute("ALTER TABLE cd_deploy_logs ADD COLUMN approval_id INTEGER DEFAULT 0")
        with suppress(Exception):
            conn.execute("ALTER TABLE cd_deploy_logs ADD COLUMN runner VARCHAR(128) DEFAULT ''")
        with suppress(Exception):
            conn.execute("ALTER TABLE cd_deploy_logs ADD COLUMN heartbeat_at VARCHAR(32) DEFAULT ''")
        # 并发锁唯一索引：running 记录 lock_key=project，同项目至多一条 running（NULL 可重复）
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_cdl_lock_key ON cd_deploy_logs(lock_key)")
        # 清理废弃的 deploy_id 列（原自增部署序号，已改用主键 id）
        with suppress(Exception):
            conn.execute("DROP INDEX IF EXISTS idx_cdl_deploy_id")
        with suppress(Exception):
            conn.execute("ALTER TABLE cd_deploy_logs DROP COLUMN deploy_id")

        conn.execute(f"""CREATE TABLE IF NOT EXISTS cd_bots (
            id {PK},
            name VARCHAR(255) UNIQUE,
            type VARCHAR(32) DEFAULT 'custom',
            webhook_url TEXT NOT NULL,
            template TEXT DEFAULT '',
            created_at TEXT DEFAULT ({NOW})
        )""")
        with suppress(Exception):
            conn.execute("ALTER TABLE cd_bots ADD COLUMN template TEXT DEFAULT ''")

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
            artifact_id INTEGER DEFAULT 0,
            artifact_digest VARCHAR(128) DEFAULT '',
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

        # 登录会话表（不透明 token + 服务端会话）：token_hash 存 SHA-256 摘要，
        # logout 删除对应行即时吊销；expires_at 为 epoch 秒，过期后拒绝并惰性清理。
        conn.execute(f"""CREATE TABLE IF NOT EXISTS cd_sessions (
            token_hash VARCHAR(64) PRIMARY KEY,
            username VARCHAR(64) NOT NULL,
            expires_at INTEGER NOT NULL,
            created_at TEXT DEFAULT ({NOW})
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
        with suppress(Exception):
            conn.execute("CREATE INDEX IF NOT EXISTS idx_metrics_monitor ON cd_custom_monitor_metrics(monitor_id)")

        # Webhook 接收配置表
        conn.execute(f"""CREATE TABLE IF NOT EXISTS cd_webhooks (
            id {PK},
            name VARCHAR(255) UNIQUE,
            token VARCHAR(255) UNIQUE NOT NULL,
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

        # 审批单表（v1.5.0）
        conn.execute(f"""CREATE TABLE IF NOT EXISTS cd_approvals (
            id {PK},
            project VARCHAR(255) NOT NULL,
            tag VARCHAR(255) DEFAULT '',
            image VARCHAR(512) DEFAULT '',
            deploy_type VARCHAR(32) DEFAULT '',
            envs VARCHAR(255) DEFAULT '',
            params_json TEXT DEFAULT '',
            status VARCHAR(16) DEFAULT 'pending',
            requester VARCHAR(64) DEFAULT '',
            approver VARCHAR(64) DEFAULT '',
            approve_note VARCHAR(512) DEFAULT '',
            deploy_id INTEGER DEFAULT 0,
            created_at TEXT DEFAULT ({NOW}),
            approved_at TEXT DEFAULT '',
            scheduled_at TEXT DEFAULT '',
            updated_at TEXT DEFAULT ''
        )""")

        # 旧库缺列幂等迁移（CREATE TABLE IF NOT EXISTS 不会为已存在的表补列）
        for col, col_def in (
            ("deploy_id", "INTEGER DEFAULT 0"),
            ("approved_at", "TEXT DEFAULT ''"),
            ("scheduled_at", "TEXT DEFAULT ''"),
            ("updated_at", "TEXT DEFAULT ''"),
        ):
            with suppress(Exception):
                conn.execute(f"ALTER TABLE cd_approvals ADD COLUMN {col} {col_def}")

        # 审批规则表（v1.5.0）
        conn.execute(f"""CREATE TABLE IF NOT EXISTS cd_approval_rules (
            id {PK},
            project VARCHAR(255) NOT NULL UNIQUE,
            enabled INTEGER DEFAULT 0,
            require_envs VARCHAR(255) DEFAULT '',
            approver_role VARCHAR(32) DEFAULT '',
            approvers VARCHAR(1024) DEFAULT '',
            notify_bot_id INTEGER DEFAULT 0,
            require_rollback_approval INTEGER DEFAULT 1,
            created_at TEXT DEFAULT ({NOW})
        )""")

        # ── 自动迁移：补充已有表缺失的列 ──
        migrations = [
            ("cd_custom_monitors", "output_format", "VARCHAR(32) DEFAULT 'auto'"),
            ("cd_registry_artifacts", "artifact_id", "INTEGER DEFAULT 0"),
            ("cd_registry_artifacts", "artifact_digest", "VARCHAR(128) DEFAULT ''"),
            ("cd_approvals", "scheduled_at", "TEXT DEFAULT ''"),
        ]
        for tbl, col, col_def in migrations:
            with suppress(Exception):
                conn.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {col_def}")

        self._ensure_indexes(conn)
        # 历史回填（幂等）：旧审批单执行后已回填 cd_approvals.deploy_id，据此反填部署记录 approval_id。
        # 必须在 cd_approvals 建表之后执行；仅处理 approval_id=0 的行，不覆盖已有值。
        with suppress(Exception):
            conn.execute(
                "UPDATE cd_deploy_logs SET approval_id=("
                "SELECT a.id FROM cd_approvals a WHERE a.deploy_id=cd_deploy_logs.id"
                ") WHERE approval_id=0 AND EXISTS ("
                "SELECT 1 FROM cd_approvals a WHERE a.deploy_id=cd_deploy_logs.id AND a.deploy_id>0)"
            )
        # v_glue_deploy_logs：供 Devops-Glue 只读的部署记录契约视图（审计字段，不含 output 等大字段）。
        # SQLite 不支持 OR REPLACE VIEW，用 DROP+CREATE 自愈，保证契约列变化时老库也能刷新。
        with suppress(Exception):
            conn.execute("DROP VIEW IF EXISTS v_glue_deploy_logs")
            conn.execute(
                "CREATE VIEW v_glue_deploy_logs AS "
                "SELECT id, project, tag, image, deploy_type, target, status, triggered_by, "
                "deploy_note, created_at FROM cd_deploy_logs"
            )
        conn.commit()

        # ── 清单自检（CD_REQUIRED_TABLES 是唯一真相源）──
        # 建表跑完必须覆盖清单里的每一张：清单加了表却忘了在上面补 DDL 时，开发机第一次
        # 连库就当场报错（宁可启动失败，也不要半套 schema 跑起来等闸门 503 才发现）。
        existing = {_first_col_value(row) for row in conn.execute(_CD_TABLE_LIST_SQL["sqlite"]).fetchall()}
        missing = [t for t in CD_REQUIRED_TABLES if t not in existing]
        if missing:
            raise RuntimeError(
                "SQLite 建表逻辑与 CD_REQUIRED_TABLES 清单不一致，缺少："
                + "、".join(missing)
                + "。新增/删除 CD 表时须同步 backend/database.py::_ensure_cd_tables 与 database/init_mysql.sql"
            )

    # ── 索引（SQLite / MySQL 共用）──

    def _ensure_indexes(self, conn):
        for name, tbl, col in [
            ("idx_cdl_project", "cd_deploy_logs", "project"),
            ("idx_cdl_created", "cd_deploy_logs", "created_at"),
            ("idx_cdl_project_tag_status", "cd_deploy_logs", "project, tag, status"),
            ("idx_cdl_status", "cd_deploy_logs", "status"),
            ("idx_cdl_approval_id", "cd_deploy_logs", "approval_id"),
            ("idx_cdr_repo_id", "cd_registry_artifacts", "repo_id"),
            ("idx_cds_type", "cd_servers", "type"),
            ("idx_cdr_enabled", "cd_alert_rules", "enabled"),
            ("idx_cdr_created", "cd_alert_rules", "created_at"),
            ("idx_cdm_enabled", "cd_custom_monitors", "enabled"),
            ("idx_cdm_created", "cd_custom_monitors", "created_at"),
            ("idx_cwh_enabled", "cd_webhooks", "enabled"),
            ("idx_we_webhook", "cd_webhook_events", "webhook_id"),
            ("idx_we_received", "cd_webhook_events", "received_at"),
            ("idx_appr_status", "cd_approvals", "status"),
            ("idx_appr_project", "cd_approvals", "project"),
            ("idx_appr_created", "cd_approvals", "created_at"),
            ("idx_cds_username", "cd_sessions", "username"),
        ]:
            with suppress(Exception):
                conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {tbl}({col})")
        conn.commit()
