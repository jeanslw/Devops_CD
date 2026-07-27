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

    # ── 加密密钥（可选）──
    # 用于加密 cd_servers 的 password / ssh_key；留空则自动生成 .cd_secret_key 文件
    secret_key: str = ""

    # ── Harbor 镜像仓库（必填）──
    harbor_registry: str = ""
    harbor_user: str = ""
    harbor_password: str = ""

    # ── 服务（可选）──
    host: str = "0.0.0.0"
    port: int = 8081

    # ── 角色（可选）──
    # 超级管理员角色名：唯一可以创建/删除/修改管理员账号的角色
    super_admin_role: str = "super_admin"
    # 普通管理员角色名
    admin_role: str = "admin"
    # 部署者角色名
    deployer_role: str = "deployer"
    # 只读角色名
    viewer_role: str = "viewer"

    # ── SSH（可选）──
    ssh_timeout: int = 30

    # ── Docker 部署（可选）──
    container_restart_policy: str = "always"

    # ── 通知（可选）──
    dingtalk_secret: str = ""    # 钉钉加签密钥
    log_truncate_chars: int = 2000
    notify_truncate_chars: int = 200

    # ── 镜像仓库同步（可选）──
    registry_sync_interval: int = 30  # 分钟，0 关闭

    # ── 监控（可选）──
    monitoring_enabled: bool = True
    monitor_cache_servers: int = 60
    monitor_cache_system: int = 30
    monitor_cache_nodes: int = 30
    monitor_cache_pods: int = 30
    monitor_cache_docker: int = 30
    monitor_cache_pod_detail: int = 15
    alert_check_interval: int = 60      # 告警检测间隔（秒）

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
