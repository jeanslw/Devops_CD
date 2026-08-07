-- CD Service 建表脚本（兼容 MySQL 5.7+ / MariaDB 10.4+）
-- 使用前请确保已创建数据库，例如: CREATE DATABASE devops_glue CHARACTER SET utf8mb4;
-- 执行: mysql -u root -p devops_glue < database/init_mysql.sql

-- ⚠️ admin_users 由 php_api 管理，这里仅补充 role 列（cd_service 权限系统）
-- ALTER TABLE admin_users ADD COLUMN role VARCHAR(32) DEFAULT 'admin';

CREATE TABLE IF NOT EXISTS cd_servers (
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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS cd_deploy_logs (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    deploy_id   INT          DEFAULT 0,
    project     VARCHAR(255),
    tag         VARCHAR(255),
    image       VARCHAR(512),
    deploy_type VARCHAR(32),
    target      VARCHAR(255),
    status      VARCHAR(32),
    output      TEXT,
    triggered_by VARCHAR(64) DEFAULT '',
    created_at  DATETIME     DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS cd_bots (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    name        VARCHAR(255) UNIQUE,
    type        VARCHAR(32)  DEFAULT 'custom',
    webhook_url TEXT         NOT NULL,
    template    VARCHAR(2000) DEFAULT '',
    created_at  DATETIME     DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 镜像仓库缓存表
CREATE TABLE IF NOT EXISTS cd_registry_repositories (
    id           INT AUTO_INCREMENT PRIMARY KEY,
    project_name VARCHAR(255) NOT NULL,
    repo_name    VARCHAR(512) NOT NULL,
    UNIQUE KEY uk_project_repo (project_name, repo_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS cd_registry_artifacts (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    repo_id       INT,
    tag           VARCHAR(255),
    digest        VARCHAR(128),
    size_bytes    BIGINT        DEFAULT 0,
    push_time     DATETIME      DEFAULT NULL,
    pull_time     DATETIME      DEFAULT NULL,
    scan_status   VARCHAR(32)   DEFAULT '',
    scan_severity VARCHAR(16)   DEFAULT '',
    vuln_critical INT           DEFAULT 0,
    vuln_high     INT           DEFAULT 0,
    vuln_medium   INT           DEFAULT 0,
    vuln_low      INT           DEFAULT 0,
    vuln_fixable  INT           DEFAULT 0,
    last_sync     DATETIME      DEFAULT NULL,
    UNIQUE KEY uk_repo_tag_digest (repo_id, tag, digest),
    FOREIGN KEY (repo_id) REFERENCES cd_registry_repositories(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ============================================================================
-- 索引（幂等创建，避免重复执行报错）
-- CI 相关表（ci_pipeline_tags / ci_job_git_map）由 PHP API 管理，仅在该表已存在时建索引
-- 使用 INFORMATION_SCHEMA 检查实现跨引擎幂等（MySQL 不支持 CREATE INDEX IF NOT EXISTS）
-- ============================================================================
DELIMITER $$
DROP PROCEDURE IF EXISTS __add_index$$
CREATE PROCEDURE __add_index(IN tbl VARCHAR(64), IN idx VARCHAR(64), IN cols VARCHAR(256))
BEGIN
    DECLARE _tbl INT DEFAULT 0;
    DECLARE _idx INT DEFAULT 0;
    SELECT COUNT(*) INTO _tbl FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = tbl;
    IF _tbl = 1 THEN
        SELECT COUNT(*) INTO _idx FROM INFORMATION_SCHEMA.STATISTICS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = tbl AND INDEX_NAME = idx;
        IF _idx = 0 THEN
            SET @_ddl = CONCAT('CREATE INDEX ', idx, ' ON ', tbl, '(', cols, ')');
            PREPARE _stmt FROM @_ddl; EXECUTE _stmt; DEALLOCATE PREPARE _stmt;
        END IF;
    END IF;
END $$
DELIMITER ;

-- cd_servers 索引（表在本脚本中已创建）
CALL __add_index('cd_servers', 'idx_cds_type', 'type');

-- cd_deploy_logs 索引（表在本脚本中已创建）
CALL __add_index('cd_deploy_logs', 'idx_cdl_project', 'project');
CALL __add_index('cd_deploy_logs', 'idx_cdl_created', 'created_at');
CALL __add_index('cd_deploy_logs', 'idx_cdl_deploy_id', 'deploy_id');
CALL __add_index('cd_deploy_logs', 'idx_cdl_project_tag_status', 'project, tag, status');

-- CI 相关表索引（表可能不存在，存储过程内部会检查并跳过）
CALL __add_index('ci_pipeline_tags', 'idx_pt_project', 'project');
CALL __add_index('ci_pipeline_tags', 'idx_pt_created', 'created_at');
CALL __add_index('ci_job_git_map', 'idx_jgm_path', 'current_path(255)');

-- cd_registry_artifacts 索引（表在本脚本中已创建）
CALL __add_index('cd_registry_artifacts', 'idx_cdr_repo_id', 'repo_id');

-- cd_alert_rules 索引（表在本脚本中已创建）
CALL __add_index('cd_alert_rules', 'idx_cdr_enabled', 'enabled');
CALL __add_index('cd_alert_rules', 'idx_cdr_created', 'created_at');

-- cd_custom_monitors 索引（表在本脚本中已创建）
CALL __add_index('cd_custom_monitors', 'idx_cdm_enabled', 'enabled');
CALL __add_index('cd_custom_monitors', 'idx_cdm_created', 'created_at');

CALL __add_index('cd_webhooks', 'idx_cwh_enabled', 'enabled');

DROP PROCEDURE IF EXISTS __add_index;

-- 系统配置表（键值对）
CREATE TABLE IF NOT EXISTS cd_config (
    key_name VARCHAR(128) PRIMARY KEY,
    value    TEXT NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 告警规则表
CREATE TABLE IF NOT EXISTS cd_alert_rules (
    id               INT AUTO_INCREMENT PRIMARY KEY,
    name             VARCHAR(255),
    target_type      VARCHAR(32)   DEFAULT 'system',   -- system: CPU/内存/硬盘, app: Pod/Docker/进程
    resource_type    VARCHAR(32)   NOT NULL,            -- cpu/memory/disk/pod_cpu/pod_memory/docker_cpu/docker_memory/process_cpu/process_memory
    server_ids       VARCHAR(2048) DEFAULT '',           -- 逗号分隔的 server id，空=全部
    threshold        INT           DEFAULT 80,          -- 阈值百分比
    bot_id           INT           DEFAULT 0,           -- 通知 Bot ID
    template         VARCHAR(2000) DEFAULT '',           -- 自定义通知模板
    enabled          TINYINT(1)    DEFAULT 1,
    cooldown_minutes INT           DEFAULT 10,          -- 同一规则同一服务器告警冷却（分钟）
    duration_minutes INT           DEFAULT 0,           -- 持续超标 N 分钟后才报警（0=立即）
    last_alert_at    DATETIME      DEFAULT NULL,
    created_at       DATETIME      DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 自定义监控项（采集器）表
CREATE TABLE IF NOT EXISTS cd_custom_monitors (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    name          VARCHAR(255) NOT NULL,
    command       TEXT         NOT NULL,
    output_format VARCHAR(32)  DEFAULT 'auto',     -- auto/csv/kv/json：输出解析格式
    description   TEXT,
    server_ids    VARCHAR(2048) DEFAULT '',
    enabled       TINYINT(1)   DEFAULT 1,
    created_at    DATETIME     DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 自定义监控指标定义表（一条采集器可配多个指标）
CREATE TABLE IF NOT EXISTS cd_custom_monitor_metrics (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    monitor_id  INT          NOT NULL,
    name        VARCHAR(255) NOT NULL,              -- 指标显示名
    field_key   VARCHAR(255) NOT NULL,              -- CSV 列名 / KV key / JSON path
    unit        VARCHAR(32)  DEFAULT '',             -- 单位（% / MB / °C …）
    sort_order  INT          DEFAULT 0,
    FOREIGN KEY (monitor_id) REFERENCES cd_custom_monitors(id) ON DELETE CASCADE,
    INDEX idx_metrics_monitor (monitor_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Webhook 接收配置表（CI 构建完成等外部事件推送到 CD）
CREATE TABLE IF NOT EXISTS cd_webhooks (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    name        VARCHAR(255) UNIQUE,
    token       VARCHAR(64)  UNIQUE NOT NULL,       -- 随机生成的 URL token
    bot_id      INT          DEFAULT 0,             -- 关联 Bot，0 = 不自动转发
    enabled     TINYINT(1)   DEFAULT 1,
    created_at  DATETIME     DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Webhook 收到的事件记录
CREATE TABLE IF NOT EXISTS cd_webhook_events (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    webhook_id    INT          NOT NULL,
    payload       TEXT,                               -- 原始 JSON
    received_at   DATETIME     DEFAULT CURRENT_TIMESTAMP,
    forwarded     TINYINT(1)   DEFAULT 0,             -- 是否已转发到 Bot
    forwarded_at  DATETIME     DEFAULT NULL,
    INDEX idx_we_webhook (webhook_id),
    INDEX idx_we_received (received_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
