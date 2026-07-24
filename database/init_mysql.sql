-- CD Service MySQL 建表脚本
-- 使用前请确保已创建数据库，例如: CREATE DATABASE devops_glue CHARACTER SET utf8mb4;
-- 执行: mysql -u root -p devops_glue < database/init_mysql.sql

CREATE TABLE IF NOT EXISTS cd_servers (
    id         INT AUTO_INCREMENT PRIMARY KEY,
    name       VARCHAR(255) UNIQUE,
    host       VARCHAR(255),
    port       INT          DEFAULT 22,
    user       VARCHAR(64)  DEFAULT 'root',
    type       VARCHAR(32)  DEFAULT 'ssh',
    auth_type  VARCHAR(20)  DEFAULT 'password',
    password   VARCHAR(255) DEFAULT '',
    ssh_key    TEXT         DEFAULT '',
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
    created_at  DATETIME     DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS cd_bots (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    name        VARCHAR(255) UNIQUE,
    type        VARCHAR(32)  DEFAULT 'custom',
    webhook_url TEXT         NOT NULL,
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

-- 索引（CI 相关表由 PHP API 管理，这里只建索引）
CREATE INDEX IF NOT EXISTS idx_cdl_project ON cd_deploy_logs(project);
CREATE INDEX IF NOT EXISTS idx_cdl_created ON cd_deploy_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_pt_project ON ci_pipeline_tags(project);
CREATE INDEX IF NOT EXISTS idx_pt_created ON ci_pipeline_tags(created_at);
CREATE INDEX IF NOT EXISTS idx_jgm_path   ON ci_job_git_map(current_path);
CREATE INDEX IF NOT EXISTS idx_cdr_repo_id ON cd_registry_artifacts(repo_id);
