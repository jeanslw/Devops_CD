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
    password   VARCHAR(255) DEFAULT '',
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

-- 索引（CI 相关表由 PHP API 管理，这里只建索引）
CREATE INDEX IF NOT EXISTS idx_cdl_project ON cd_deploy_logs(project);
CREATE INDEX IF NOT EXISTS idx_cdl_created ON cd_deploy_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_pt_project ON ci_pipeline_tags(project);
CREATE INDEX IF NOT EXISTS idx_pt_created ON ci_pipeline_tags(created_at);
CREATE INDEX IF NOT EXISTS idx_jgm_path   ON ci_job_git_map(current_path);
