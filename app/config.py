"""应用配置 — Pydantic BaseSettings，从 .env / 环境变量读取"""

from pathlib import Path
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── 数据库（必填: sqlite | mysql）──
    db_driver: str = ""
    db_path: str = str(Path(__file__).parent.parent.parent / "php_api" / "config" / "data" / "data.db")
    db_host: str = "127.0.0.1"
    db_port: int = 3306
    db_name: str = "devops_glue"
    db_user: str = "root"
    db_pass: str = ""

    # ── Harbor 镜像仓库 ──
    harbor_registry: str = "192.168.137.5"
    harbor_user: str = "admin"
    harbor_password: str = ""

    # ── 服务 ──
    host: str = "0.0.0.0"
    port: int = 8081
    reload: bool = False

    # ── SSH 默认值 ──
    ssh_timeout: int = 30
    ssh_default_user: str = "root"

    # ── Docker 部署 ──
    container_restart_policy: str = "always"

    # ── 日志 ──
    log_truncate_chars: int = 500
    notify_truncate_chars: int = 200

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
