"""数据库访问层 - SQLite / MySQL 双驱动.
- MySQL: DBUtils 连接池,事务自动提交(成功)/ 回滚(异常)
- SQLite: 写入自动 commit,读取自动 close,WAL 模式
"""

import logging
import re
import sqlite3
import threading
import time
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, NamedTuple

from backend.config import settings
from backend.exceptions import AppException
from backend.services.ci_client import CiClientError, get_ci_client

logger = logging.getLogger(__name__)


class DatabaseUnavailableError(AppException):
    """数据库当前不可用(连不上 / 建不了表 / 库被锁或只读 / 磁盘满)→ 503 errors.db_unavailable."""

    def __init__(self, message: str, driver: str = "", reason: str = ""):
        super().__init__(
            message,
            status_code=503,
            error_key="errors.db_unavailable",
            error_params={"driver": driver, "reason": (reason or message)[:300]},
        )
        self.driver = driver


class ContractUnavailableError(AppException):
    """集成服务契约端点问不到(未配置 / Glue 过旧 / 网络 / 鉴权失败 / 响应形状不符)→ 503 errors.ci_contract_unreachable."""

    def __init__(self, message: str):
        super().__init__(
            message,
            status_code=503,
            error_key="errors.ci_contract_unreachable",
            error_params={"reason": message[:300]},
        )


_Q_MARK_RE = re.compile(r"(?<!')\?(?!')")  # 匹配不在引号内的 ?

# CD 自有表清单(唯一真相源):闸门判定、错误文案、张数均由此派生.
# 增删表需同步:CD_REQUIRED_TABLES / database/init_mysql.sql / _MYSQL_TABLE_DDL / _ensure_cd_tables.
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

# 列出现存基础表:MySQL 走 information_schema,SQLite 走 sqlite_master;视图不参与闸门.
_CD_TABLE_LIST_SQL = {
    "mysql": (
        "SELECT table_name FROM information_schema.tables WHERE table_schema = DATABASE() AND table_type = 'BASE TABLE'"
    ),
    "sqlite": "SELECT name FROM sqlite_master WHERE type = 'table'",
}

# ── 集成服务契约(Glue 侧开关;CD 只经 HTTP 契约端点消费)────────────
CD_SERVICE_NAME = "cd"
PROVISION_AUTO = "auto"  # 授权 CD 自动建表 / 补列
PROVISION_MANUAL = "manual"  # CD 不建表,只认运维预建的 CD 表


class CdIntegrationState(NamedTuple):
    """契约答复的启用状态(enabled / provision / 契约行是否存在)."""

    enabled: bool
    provision: str
    row_exists: bool


# ── 契约读:仅 HTTP 通道 + 窗口缓存(窗口 = CI_CONTRACT_TTL;问不到统一按「契约不可达」fail-closed)──
_contract_lock = threading.Lock()
_contract_cache: dict[str, tuple[CdIntegrationState, float]] = {}  # {service_name: (状态, 取到时的单调时钟)}
_contract_http_down_until = 0.0  # 契约端点「已知不可用」的截止时间戳(单调时钟),窗口内直接快速失败
_contract_last_warn = 0.0
_CONTRACT_WARN_INTERVAL = 60.0  # 不可用 WARN 的限流窗口(秒)


def _contract_now() -> float:
    """单调时钟(单测可打桩 backend.database._contract_now)."""
    return time.monotonic()


def invalidate_contract_cache() -> None:
    """立刻作废契约读缓存:下一个请求重新问 Glue(排障与单测用)."""
    global _contract_http_down_until
    with _contract_lock:
        _contract_cache.clear()
        _contract_http_down_until = 0.0


def _warn_contract_unreachable(reason: str) -> None:
    """契约端点问不到时按窗口限流打 WARN."""
    global _contract_last_warn
    with _contract_lock:
        now = _contract_now()
        if now - _contract_last_warn < _CONTRACT_WARN_INTERVAL:
            return
        _contract_last_warn = now
    logger.warning("集成服务契约端点不可用，本次请求按「契约不可达」处理（503 fail-closed）：%s", reason)


def _mark_contract_http_down() -> None:
    """记下契约端点不可用:窗口(CI_CONTRACT_TTL,为 0 时用 WARN 窗口)内直接按「契约不可达」快速失败."""
    global _contract_http_down_until
    ttl = max(0, settings.ci_contract_ttl) or _CONTRACT_WARN_INTERVAL
    with _contract_lock:
        _contract_http_down_until = _contract_now() + ttl


def _normalize_contract(raw_enabled: Any, raw_provision: Any) -> tuple[bool, str]:
    """归一化契约答复:enabled 兼容 int / 字符串 / JSON bool;provision 只认 'manual',其余按 auto."""
    enabled = str(raw_enabled).strip().lower() not in ("", "0", "false", "none")
    provision = PROVISION_MANUAL if str(raw_provision or "").strip().lower() == PROVISION_MANUAL else PROVISION_AUTO
    return enabled, provision


def _fetch_contract_http(service: str = CD_SERVICE_NAME) -> CdIntegrationState:
    """(内部)经 Glue 的契约端点读启用状态;问不到(未配置 / 窗口内 / HTTP 失败 / 响应形状不符)抛 ContractUnavailableError.

    窗口缓存由 _resolve_contract_state 负责,本函数只管这一次问.
    """
    if not settings.ci_api_url or not settings.ci_api_token:
        raise ContractUnavailableError(
            "未配置 CI_API_URL / CI_API_TOKEN，无法读取集成服务契约（CD 只经契约端点读启用状态）",
        )
    if _contract_now() < _contract_http_down_until:
        raise ContractUnavailableError("集成服务契约端点暂不可用（不可用窗口内快速失败）")
    try:
        raw = get_ci_client().get_service_contract(service)
    except CiClientError as e:
        _mark_contract_http_down()
        _warn_contract_unreachable(f"HTTP {e.status_code or 'n/a'}: {e}")
        raise ContractUnavailableError(f"契约端点请求失败：{e}") from e
    except Exception as e:  # noqa: BLE001 - 网络 / 解析等任何异常都只说明"问不到",不能改变启用判定
        _mark_contract_http_down()
        _warn_contract_unreachable(f"{type(e).__name__}: {e}")
        raise ContractUnavailableError(f"契约端点请求失败：{type(e).__name__}: {e}") from e
    if not isinstance(raw, dict) or "enabled" not in raw:
        _warn_contract_unreachable(f"响应形状不符：{str(raw)[:120]}")
        raise ContractUnavailableError("契约端点响应形状不符（缺 enabled 字段）")
    enabled, provision = _normalize_contract(raw.get("enabled"), raw.get("provision"))
    # token_active 仅供展示,不作判据;HTTP 200 即契约行存在(row_exists=True).
    return CdIntegrationState(enabled, provision, True)


# ── auto 模式的 MySQL 建表 DDL(与 database/init_mysql.sql 同构,容器自带)──
# 只建 CD 自有表(cd_*).
_MYSQL_TABLE_DDL: dict[str, str] = {
    "cd_servers": """CREATE TABLE IF NOT EXISTS cd_servers (
        id         INT AUTO_INCREMENT PRIMARY KEY,
        name       VARCHAR(255) UNIQUE,
        host       VARCHAR(255),
        port       INT          DEFAULT 22,
        user       VARCHAR(64)  DEFAULT 'root',
        type       VARCHAR(32)  DEFAULT 'ssh',
        auth_type  VARCHAR(20)  DEFAULT 'password',
        password   VARCHAR(255) DEFAULT '',
        ssh_key    VARCHAR(8000) DEFAULT '',
        tags       VARCHAR(255) DEFAULT '',
        created_at DATETIME     DEFAULT CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    "cd_deploy_logs": """CREATE TABLE IF NOT EXISTS cd_deploy_logs (
        id           INT AUTO_INCREMENT PRIMARY KEY,
        project      VARCHAR(255),
        tag          VARCHAR(255),
        image        VARCHAR(512),
        deploy_type  VARCHAR(32),
        target       VARCHAR(255),
        status       VARCHAR(32),
        output       TEXT,
        triggered_by VARCHAR(64)  DEFAULT '',
        deploy_note  VARCHAR(512) DEFAULT '',
        duration_ms  INT          DEFAULT 0,
        stage_times  TEXT,
        runner       VARCHAR(128) DEFAULT '',
        heartbeat_at VARCHAR(32)  DEFAULT '',
        created_at   DATETIME     DEFAULT CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    "cd_bots": """CREATE TABLE IF NOT EXISTS cd_bots (
        id          INT AUTO_INCREMENT PRIMARY KEY,
        name        VARCHAR(255) UNIQUE,
        type        VARCHAR(32)  DEFAULT 'custom',
        webhook_url TEXT         NOT NULL,
        template    VARCHAR(2000) DEFAULT '',
        created_at  DATETIME     DEFAULT CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    "cd_registry_repositories": """CREATE TABLE IF NOT EXISTS cd_registry_repositories (
        id           INT AUTO_INCREMENT PRIMARY KEY,
        project_name VARCHAR(255) NOT NULL,
        repo_name    VARCHAR(512) NOT NULL,
        UNIQUE KEY uk_project_repo (project_name, repo_name)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    "cd_registry_artifacts": """CREATE TABLE IF NOT EXISTS cd_registry_artifacts (
        id              INT AUTO_INCREMENT PRIMARY KEY,
        repo_id         INT,
        tag             VARCHAR(255),
        digest          VARCHAR(128),
        artifact_id     INT           DEFAULT 0,
        artifact_digest VARCHAR(128)  DEFAULT '',
        size_bytes      BIGINT        DEFAULT 0,
        push_time       DATETIME      DEFAULT NULL,
        pull_time       DATETIME      DEFAULT NULL,
        scan_status     VARCHAR(32)   DEFAULT '',
        scan_severity   VARCHAR(16)   DEFAULT '',
        vuln_critical   INT           DEFAULT 0,
        vuln_high       INT           DEFAULT 0,
        vuln_medium     INT           DEFAULT 0,
        vuln_low        INT           DEFAULT 0,
        vuln_fixable    INT           DEFAULT 0,
        last_sync       DATETIME      DEFAULT NULL,
        UNIQUE KEY uk_repo_tag_digest (repo_id, tag, digest),
        FOREIGN KEY (repo_id) REFERENCES cd_registry_repositories(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    "cd_config": """CREATE TABLE IF NOT EXISTS cd_config (
        key_name VARCHAR(128) PRIMARY KEY,
        value    TEXT NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    "cd_alert_rules": """CREATE TABLE IF NOT EXISTS cd_alert_rules (
        id               INT AUTO_INCREMENT PRIMARY KEY,
        name             VARCHAR(255),
        target_type      VARCHAR(32)   DEFAULT 'system',
        resource_type    VARCHAR(32)   NOT NULL,
        server_ids       VARCHAR(2048) DEFAULT '',
        threshold        INT           DEFAULT 80,
        bot_id           INT           DEFAULT 0,
        template         VARCHAR(2000) DEFAULT '',
        enabled          TINYINT(1)    DEFAULT 1,
        cooldown_minutes INT           DEFAULT 10,
        duration_minutes INT           DEFAULT 0,
        last_alert_at    DATETIME      DEFAULT NULL,
        created_at       DATETIME      DEFAULT CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    "cd_custom_monitors": """CREATE TABLE IF NOT EXISTS cd_custom_monitors (
        id            INT AUTO_INCREMENT PRIMARY KEY,
        name          VARCHAR(255) NOT NULL,
        command       TEXT         NOT NULL,
        output_format VARCHAR(32)  DEFAULT 'auto',
        description   TEXT,
        server_ids    VARCHAR(2048) DEFAULT '',
        enabled       TINYINT(1)   DEFAULT 1,
        created_at    DATETIME     DEFAULT CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    "cd_custom_monitor_metrics": """CREATE TABLE IF NOT EXISTS cd_custom_monitor_metrics (
        id         INT AUTO_INCREMENT PRIMARY KEY,
        monitor_id INT          NOT NULL,
        name       VARCHAR(255) NOT NULL,
        field_key  VARCHAR(255) NOT NULL,
        unit       VARCHAR(32)  DEFAULT '',
        sort_order INT          DEFAULT 0,
        FOREIGN KEY (monitor_id) REFERENCES cd_custom_monitors(id) ON DELETE CASCADE,
        INDEX idx_metrics_monitor (monitor_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    "cd_webhooks": """CREATE TABLE IF NOT EXISTS cd_webhooks (
        id         INT AUTO_INCREMENT PRIMARY KEY,
        name       VARCHAR(255) UNIQUE,
        token      VARCHAR(255) UNIQUE NOT NULL,
        bot_id     INT          DEFAULT 0,
        enabled    TINYINT(1)   DEFAULT 1,
        created_at DATETIME     DEFAULT CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    "cd_webhook_events": """CREATE TABLE IF NOT EXISTS cd_webhook_events (
        id           INT AUTO_INCREMENT PRIMARY KEY,
        webhook_id   INT          NOT NULL,
        payload      TEXT,
        received_at  DATETIME     DEFAULT CURRENT_TIMESTAMP,
        forwarded    TINYINT(1)   DEFAULT 0,
        forwarded_at DATETIME     DEFAULT NULL,
        INDEX idx_we_webhook (webhook_id),
        INDEX idx_we_received (received_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    "cd_approvals": """CREATE TABLE IF NOT EXISTS cd_approvals (
        id           INT AUTO_INCREMENT PRIMARY KEY,
        project      VARCHAR(255) NOT NULL,
        tag          VARCHAR(255) DEFAULT '',
        image        VARCHAR(512) DEFAULT '',
        deploy_type  VARCHAR(32)  DEFAULT '',
        envs         VARCHAR(255) DEFAULT '',
        params_json  TEXT,
        status       VARCHAR(16)  DEFAULT 'pending',
        requester    VARCHAR(64)  DEFAULT '',
        approver     VARCHAR(64)  DEFAULT '',
        scheduled_at VARCHAR(32)  DEFAULT '',
        approve_note VARCHAR(512) DEFAULT '',
        deploy_id    INT          DEFAULT 0,
        created_at   DATETIME     DEFAULT CURRENT_TIMESTAMP,
        approved_at  DATETIME     DEFAULT NULL,
        updated_at   DATETIME     DEFAULT NULL,
        INDEX idx_appr_status (status),
        INDEX idx_appr_project (project),
        INDEX idx_appr_created (created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    "cd_approval_rules": """CREATE TABLE IF NOT EXISTS cd_approval_rules (
        id                        INT AUTO_INCREMENT PRIMARY KEY,
        project                   VARCHAR(255)  NOT NULL,
        enabled                   TINYINT(1)    DEFAULT 0,
        require_envs              VARCHAR(255)  DEFAULT '',
        approver_role             VARCHAR(32)   DEFAULT '',
        approvers                 VARCHAR(1024) DEFAULT '',
        notify_bot_id             INT           DEFAULT 0,
        require_rollback_approval TINYINT(1)    DEFAULT 1,
        created_at                DATETIME      DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uk_appr_project (project)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    "cd_sessions": """CREATE TABLE IF NOT EXISTS cd_sessions (
        token_hash VARCHAR(64) PRIMARY KEY,
        username   VARCHAR(64) NOT NULL,
        expires_at INT          NOT NULL,
        created_at DATETIME     DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_cds_username (username)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
}

# auto 模式下的 MySQL 索引清单,元组含义 = (索引名, 表名, 列清单, 是否唯一);重复建报 1061 由 suppress 吞掉.
_MYSQL_INDEXES: tuple[tuple[str, str, str, bool], ...] = (
    ("idx_cds_type", "cd_servers", "type", False),
    ("idx_cdl_project", "cd_deploy_logs", "project", False),
    ("idx_cdl_created", "cd_deploy_logs", "created_at", False),
    ("idx_cdl_project_tag_status", "cd_deploy_logs", "project, tag, status", False),
    ("idx_cdl_status", "cd_deploy_logs", "status", False),
    ("idx_cdl_approval_id", "cd_deploy_logs", "approval_id", False),
    ("idx_cdl_lock_key", "cd_deploy_logs", "lock_key", True),
    ("idx_cdr_repo_id", "cd_registry_artifacts", "repo_id", False),
    ("idx_cdr_enabled", "cd_alert_rules", "enabled", False),
    ("idx_cdr_created", "cd_alert_rules", "created_at", False),
    ("idx_cdm_enabled", "cd_custom_monitors", "enabled", False),
    ("idx_cdm_created", "cd_custom_monitors", "created_at", False),
    ("idx_cwh_enabled", "cd_webhooks", "enabled", False),
)

# auto 模式下的 MySQL 缺列迁移(CREATE TABLE IF NOT EXISTS 不为已存在的表补列);TEXT 列不支持字面量 DEFAULT,统一 VARCHAR.
_MYSQL_COLUMN_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("cd_deploy_logs", "approval_id", "INT DEFAULT 0"),
    ("cd_deploy_logs", "runner", "VARCHAR(128) DEFAULT ''"),
    ("cd_deploy_logs", "heartbeat_at", "VARCHAR(32) DEFAULT ''"),
    ("cd_deploy_logs", "lock_key", "VARCHAR(255) DEFAULT NULL"),
    ("cd_deploy_logs", "params_json", "TEXT"),
    ("cd_deploy_logs", "rollback_type", "VARCHAR(32) DEFAULT 'manual'"),
    ("cd_deploy_logs", "artifact_id", "INT DEFAULT 0"),
    ("cd_deploy_logs", "artifact_digest", "VARCHAR(128) DEFAULT ''"),
    ("cd_approvals", "deploy_id", "INT DEFAULT 0"),
    ("cd_approvals", "approved_at", "VARCHAR(32) DEFAULT ''"),
    ("cd_approvals", "scheduled_at", "VARCHAR(32) DEFAULT ''"),
    ("cd_approvals", "updated_at", "VARCHAR(32) DEFAULT ''"),
    ("cd_custom_monitors", "output_format", "VARCHAR(32) DEFAULT 'auto'"),
    ("cd_registry_artifacts", "artifact_id", "INT DEFAULT 0"),
    ("cd_registry_artifacts", "artifact_digest", "VARCHAR(128) DEFAULT ''"),
)


def cd_deploy_hint(driver: str, provision: str = "") -> str:
    """按驱动 + provision 给出「怎么把清单建全」的动作提示(auto:重启自愈;manual:导入建表脚本)."""
    if provision == PROVISION_MANUAL:
        if driver == "sqlite":
            return (
                "当前为 manual（CI 手动建库模式）：CD 不会自动建表，SQLite 单机库请由运维补建这些表，"
                "或在 Devops-Glue 侧改回自动迁移模式后重新启用 CD"
            )
        return (
            "当前为 manual（CI 手动建库模式）：CD 不会自动建表，请管理员执行 database/init_mysql.sql 建全清单中的 CD 表"
        )
    if provision == PROVISION_AUTO:
        return "当前为 auto（CI 自动迁移模式）：CD 会自动建全这些表，重启 CD 服务即可"
    if driver == "sqlite":
        return "SQLite 模式：CD 表随库自动创建，重启服务即可补齐（或删除该 .db 文件重新生成）"
    return "MySQL 模式：请管理员执行 database/init_mysql.sql 建全清单中的 CD 表"


def cd_not_enabled_hint() -> str:
    """未启用时的动作提示:启用动作只在 Devops-Glue 后台完成,CD 不提供自开通入口."""
    return (
        "请到 Devops-Glue 后台「API集成服务 → 启用」开启 Devops-CD"
        "（启用动作由 CI 写集成服务契约并按 DB_AUTO_MIGRATE 决定 provision：CD 无需改配置、无需重启，"
        "契约读有缓存窗口，若仍提示未启用请稍候数秒）"
    )


def _dict_row_factory(cursor, row):
    """SQLite row_factory:返回普通 dict(支持 .get()),兼容 pymysql DictCursor"""
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}


def _first_col_value(row) -> str:
    """取结果行第一列值(SQLite 自定义 row_factory 与 pymysql DictCursor 都是 dict)"""
    if isinstance(row, dict):
        return str(next(iter(row.values())))
    return str(row[0])


def _is_missing_table_error(exc: BaseException) -> bool:
    """判断异常是否为「表不存在」(SQLite: no such table;MySQL: 1146)."""
    if "no such table" in str(exc).lower():  # SQLite
        return True
    return getattr(exc, "args", (None,))[0] == 1146  # MySQL ER_NO_SUCH_TABLE


class _MysqlWrapper:
    """pymysql 包装--提供 sqlite3 风格的 execute/commit + context manager.
    退出时自动 commit(成功)或 rollback(异常),然后归还连接到池.
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
        # 只替换 SQL 占位符中的 ?(引号内的 ? 不替换)
        cur.execute(_Q_MARK_RE.sub("%s", sql), params or ())
        return cur

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


class _SqliteWrapper:
    """sqlite3 包装--提供与 _MysqlWrapper 一致的 execute/commit/rollback/close 接口.
    __enter__ 返回 self(与 _MysqlWrapper 一致),由 conn() 的 except/else/finally 统一管理生命周期.
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
    """统一数据库连接.

    建表规则:未启用 / manual 零 DDL;enabled + auto 建全 CD_REQUIRED_TABLES + 补缺失列 +
    索引(MySQL / SQLite 各自实现,全部幂等).CD 只读写自有 cd_* 表,CI 侧数据一律经 HTTP 获取.
    """

    DRIVERS = ("sqlite", "mysql")
    _tables_ensured = False  # 类变量:建表只执行一次
    _pool = None  # MySQL 连接池实例(延迟初始化)
    _pool_lock = threading.Lock()

    @property
    def driver(self) -> str:
        """当前驱动(sqlite / mysql)."""
        return self._driver

    def __init__(self, db_path: str = ""):
        self._driver = settings.db_driver
        if self._driver not in self.DRIVERS:
            raise RuntimeError(f"DB_DRIVER 必须设为 sqlite 或 mysql，当前: {self._driver or '未设置'}")
        self._path = Path(db_path or settings.db_path)

    def _init_pool(self):
        """延迟初始化 MySQL 连接池(首次使用时创建)."""
        if self._driver != "mysql":
            return
        try:
            import pymysql
            from dbutils.pooled_db import PooledDB

            # 并发首次调用可能各建一个池,后者覆盖会让前者的连接泄漏,故串行化
            with Database._pool_lock:
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

    def probe(self) -> bool:
        """仅探测数据库连接是否可达(不触发契约闸门 / 建表),供公开探活(/api/status)用."""
        try:
            if self._driver == "mysql":
                self._init_pool()
                if Database._pool is None:
                    return False
                raw = Database._pool.connection()
            else:
                raw = self._connect_sqlite()
        except Exception:
            return False
        try:
            raw.close()
        except Exception:
            pass
        return True

    # ── 集成服务契约(闸门第一道判据)──

    def _resolve_contract_state(self) -> CdIntegrationState:
        """契约状态(闸门第一道判据):问 Glue 的契约端点,带窗口缓存(CI_CONTRACT_TTL;0 = 每请求都问).

        问不到抛 ContractUnavailableError(503),不当「未启用」.
        """
        ttl = max(0, settings.ci_contract_ttl)
        if ttl:
            with _contract_lock:
                hit = _contract_cache.get(CD_SERVICE_NAME)
            if hit is not None and _contract_now() - hit[1] < ttl:
                return hit[0]
        state = _fetch_contract_http()
        with _contract_lock:
            _contract_cache[CD_SERVICE_NAME] = (state, _contract_now())
        return state

    def cd_integration_state(self) -> CdIntegrationState:
        """读 CD 的启用状态(闸门的第一道判据):唯一通道是 Glue 的契约端点 GET /api/integration/services/cd."""
        return self._resolve_contract_state()

    def cd_missing_tables(self) -> list[str]:
        """返回当前库中缺失的 CD 表名(空列表 = 表建全 = CD 已部署;清单 = CD_REQUIRED_TABLES).

        连接不可用一律抛 DatabaseUnavailableError(503).
        """
        try:
            with self.conn() as conn:
                rows = conn.execute(_CD_TABLE_LIST_SQL[self._driver]).fetchall()
        except Exception as e:
            if _is_missing_table_error(e):
                # 连元数据表都查不到:无法证明 CD 已部署,按未部署处理
                return list(CD_REQUIRED_TABLES)
            raise
        existing = {_first_col_value(r) for r in rows}
        return [t for t in CD_REQUIRED_TABLES if t not in existing]

    def cd_deployed(self) -> bool:
        """CD 是否已部署(= CD_REQUIRED_TABLES 清单里的表全部存在)."""
        return not self.cd_missing_tables()

    @contextmanager
    def conn(self):
        """获取数据库连接(context manager,退出时自动 commit/rollback + close/归还池)
        始终返回 wrapper 对象(统一 .execute/.commit/.rollback/.close 接口)."""
        if self._driver == "mysql":
            self._init_pool()
            pool = Database._pool
            if pool is None:
                raise DatabaseUnavailableError("MySQL 连接池初始化失败", "mysql")
            try:
                raw = pool.connection()
            except Exception as e:
                # 取不到连接(网络/账号/连接数打满)→ 503 库不可用
                raise DatabaseUnavailableError(f"MySQL 取连接失败：{e}", "mysql", str(e)) from e
            wrapper = _MysqlWrapper(raw)
        else:
            try:
                raw = self._connect_sqlite()
            except sqlite3.Error as e:
                # 库文件打不开(目录不存在/不可写/被占用)→ 503 库不可用
                raise DatabaseUnavailableError(
                    f"SQLite 打开数据库文件失败（{self._path}）：{e}", "sqlite", str(e)
                ) from e
            wrapper = _SqliteWrapper(raw)

        if not Database._tables_ensured:
            # 建表授权来自契约:未启用 / manual 零 DDL,auto 建全清单 + 缺列 + 索引
            # 未启用时不置 _tables_ensured,下一请求重新判定
            try:
                state = self._resolve_contract_state()
                if state.enabled:
                    if state.provision == PROVISION_AUTO:
                        if self._driver == "mysql":
                            self._ensure_mysql_tables(wrapper)
                        else:
                            self._ensure_cd_tables(wrapper)
                    Database._tables_ensured = True
            except sqlite3.Error as e:
                # 只吞 sqlite3.Error;自检抛的 RuntimeError(清单与 DDL 漂移)必须冒泡
                wrapper.close()  # 失败也必须释放连接
                raise DatabaseUnavailableError(f"自动建表失败（{self._driver}）：{e}", self._driver, str(e)) from e
            except Exception:
                wrapper.close()  # 异常路径不得泄漏连接
                raise

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

    # ── MySQL 自动建表 / 迁移(仅 auto 模式)──

    def _ensure_mysql_tables(self, conn):
        """MySQL auto 模式自动建表:建表 → 索引 → 缺列迁移,全幂等;只操作 cd_* 表;结尾做清单自检."""
        for table in CD_REQUIRED_TABLES:
            ddl = _MYSQL_TABLE_DDL.get(table)
            if ddl is None:
                continue  # 漏表由下方自检统一报出
            conn.execute(ddl)
        conn.commit()
        self._ensure_mysql_indexes(conn)
        self._ensure_mysql_migrations(conn)

        existing = {_first_col_value(row) for row in conn.execute(_CD_TABLE_LIST_SQL["mysql"]).fetchall()}
        missing = [t for t in CD_REQUIRED_TABLES if t not in existing]
        if missing:
            raise RuntimeError(
                "MySQL 自动建表 DDL 与 CD_REQUIRED_TABLES 清单不一致，缺少："
                + "、".join(missing)
                + "。新增/删除 CD 表时须同步 backend/database.py::_MYSQL_TABLE_DDL 与 database/init_mysql.sql"
            )

    def _ensure_mysql_indexes(self, conn):
        """MySQL 索引(幂等):重复建由 suppress 吞掉."""
        for name, table, cols, unique in _MYSQL_INDEXES:
            with suppress(Exception):
                conn.execute(f"CREATE {'UNIQUE ' if unique else ''}INDEX {name} ON {table}({cols})")
        conn.commit()

    def _ensure_mysql_migrations(self, conn):
        """MySQL 缺列迁移:只对已存在的表 ADD COLUMN;重复加列报 1060 由 suppress 吞掉."""
        for tbl, col, col_def in _MYSQL_COLUMN_MIGRATIONS:
            with suppress(Exception):
                conn.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {col_def}")
        conn.commit()

    # ── SQLite 自动建表(仅 auto 模式)──

    def _ensure_cd_tables(self, conn):
        """SQLite 模式自动建表 + 索引(仅 enabled=1 且 provision='auto' 时调用);结尾做清单自检."""
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
        # 并发锁唯一索引:running 记录 lock_key=project,同项目至多一条 running(NULL 可重复)
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_cdl_lock_key ON cd_deploy_logs(lock_key)")
        # 删除 deploy_id 列及旧索引
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

        # 登录会话表:token_hash 存 SHA-256 摘要,logout 删行即时吊销;expires_at 为 epoch 秒,过期拒绝并惰性清理
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

        # 审批单表
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

        # 旧库缺列幂等迁移
        for col, col_def in (
            ("deploy_id", "INTEGER DEFAULT 0"),
            ("approved_at", "TEXT DEFAULT ''"),
            ("scheduled_at", "TEXT DEFAULT ''"),
            ("updated_at", "TEXT DEFAULT ''"),
        ):
            with suppress(Exception):
                conn.execute(f"ALTER TABLE cd_approvals ADD COLUMN {col} {col_def}")

        # 审批规则表
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

        # ── 自动迁移:补充已有表缺失的列 ──
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
        # 回填 approval_id(幂等):仅处理 approval_id=0 的行.
        with suppress(Exception):
            conn.execute(
                "UPDATE cd_deploy_logs SET approval_id=("
                "SELECT a.id FROM cd_approvals a WHERE a.deploy_id=cd_deploy_logs.id"
                ") WHERE approval_id=0 AND EXISTS ("
                "SELECT 1 FROM cd_approvals a WHERE a.deploy_id=cd_deploy_logs.id AND a.deploy_id>0)"
            )
        conn.commit()

        # ── 清单自检(CD_REQUIRED_TABLES 是唯一真相源;漏表当场报错)──
        existing = {_first_col_value(row) for row in conn.execute(_CD_TABLE_LIST_SQL["sqlite"]).fetchall()}
        missing = [t for t in CD_REQUIRED_TABLES if t not in existing]
        if missing:
            raise RuntimeError(
                "SQLite 建表逻辑与 CD_REQUIRED_TABLES 清单不一致，缺少："
                + "、".join(missing)
                + "。新增/删除 CD 表时须同步 backend/database.py::_ensure_cd_tables 与 database/init_mysql.sql"
            )

    # ── 索引(SQLite / MySQL 共用)──

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
