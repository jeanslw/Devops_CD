"""应用配置 — 所有配置通过 .env 文件设置，不要直接修改此文件"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── 数据库（必填: sqlite | mysql）──
    db_driver: str = ""
    db_path: str = ""           # SQLite 模式必填，MySQL 模式忽略
    db_host: str = ""
    db_port: int = 3306
    db_name: str = ""
    db_user: str = ""
    db_pass: str = ""

    # ── Harbor 镜像仓库（必填）──
    harbor_registry: str = ""
    harbor_user: str = ""
    harbor_password: str = ""

    # ── 服务（可选）──
    host: str = "0.0.0.0"
    port: int = 8081

    # ── SSH（可选）──
    ssh_timeout: int = 30

    # ── Docker 部署（可选）──
    container_restart_policy: str = "always"

    # ── 通知（可选）──
    dingtalk_secret: str = ""    # 钉钉加签密钥
    log_truncate_chars: int = 2000
    notify_truncate_chars: int = 200

    # ── 监控（可选）──
    monitoring_enabled: bool = True
    monitor_cache_servers: int = 60
    monitor_cache_system: int = 30
    monitor_cache_nodes: int = 30
    monitor_cache_pods: int = 30
    monitor_cache_docker: int = 30
    monitor_cache_pod_detail: int = 15

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
