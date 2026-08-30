"""应用配置 — 所有配置通过 .env 文件设置，不要直接修改此文件"""

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # ── 数据库（必填: sqlite | mysql）──
    db_driver: str = ""
    db_path: str = ""  # SQLite 模式必填，MySQL 模式忽略
    db_host: str = ""
    db_port: int = 3306
    db_name: str = ""
    db_user: str = ""
    db_pass: str = ""
    db_pool_max: int = 10  # MySQL 连接池最大值
    db_pool_min: int = 2  # MySQL 连接池最小值（常开连接数）

    # ── 加密密钥（可选）──
    # 用于加密 cd_servers 的 password / ssh_key；留空则自动生成 .cd_secret_key 文件
    secret_key: str = ""

    # ── Harbor 镜像仓库（必填）──
    # 统一读 HARBOR_BASE_URL（带 scheme 的完整地址，如 https://hub.example.com）：
    # CI(Glue) 直接使用；CD 内部剥掉 scheme 供 Docker 镜像引用，HarborClient 自动探测 https→http。
    harbor_registry: str = Field(default="", validation_alias="HARBOR_BASE_URL")
    harbor_user: str = ""
    harbor_password: str = ""

    @field_validator("harbor_registry", mode="after")
    @classmethod
    def _strip_harbor_scheme(cls, v: str) -> str:
        """剥掉 http(s):// 与尾部斜杠，得到 scheme-less 的 registry host"""
        v = (v or "").strip().rstrip("/")
        for prefix in ("https://", "http://"):
            if v.startswith(prefix):
                return v[len(prefix) :]
        return v

    # ── 服务（可选）──
    host: str = "0.0.0.0"
    port: int = 8081

    # ── 角色（仅用于 admin_users.role 字段校验，权限判断已迁移至 roles/permissions/role_permissions 表）──
    # 超级管理员角色名：唯一可以创建/删除/修改管理员账号的角色
    super_admin_role: str = "super_admin"
    # CD 管理员角色名（对应 Glue roles 表里真实存在的 cd_admin 角色）
    admin_role: str = "cd_admin"
    # 部署者角色名
    deployer_role: str = "deployer"
    # 只读角色名
    viewer_role: str = "viewer"

    # ── SSH（可选）──
    ssh_timeout: int = 30
    ssh_keepalive: int = 30  # 0 关闭 keepalive
    ssh_auto_trust: bool = False  # (已废弃) 保留字段仅为兼容旧配置，代码中始终使用 RejectPolicy + known_hosts
    # 用户必须先调用 trust_ssh_host 把 host key 存入 ~/.cd_service/known_hosts

    # ── Docker 部署（可选）──
    container_restart_policy: str = "always"

    # ── K8s 部署（可选）──
    flux_namespace: str = "flux-system"
    k8s_helm_timeout: int = 300  # helm upgrade --install --timeout（秒）
    k8s_rollout_timeout: int = 120  # kubectl rollout status --timeout（秒）

    # ── 通知（可选）──
    dingtalk_secret: str = ""  # 钉钉加签密钥
    log_truncate_chars: int = 20000
    notify_truncate_chars: int = 200

    # ── 镜像仓库同步（可选）──
    registry_sync_interval: int = 30  # 分钟，0 关闭

    # ── CI 集成（可选）──
    # CI 服务地址（如 http://localhost:8080），留空则不启用构建管理功能
    ci_api_url: str = ""
    ci_admin_user: str = ""
    ci_admin_pass: str = ""
    # API token（dg_ 前缀，服务账号 / 第三方调用），配置后优先于账号密码登录
    ci_api_token: str = ""
    ci_timeout: int = 30

    # ── 监控（可选）──
    monitoring_enabled: bool = True
    monitor_cache_servers: int = 60
    monitor_cache_system: int = 30
    monitor_cache_nodes: int = 30
    monitor_cache_pods: int = 30
    monitor_cache_docker: int = 30
    monitor_cache_pod_detail: int = 15
    alert_check_interval: int = 60  # 告警检测间隔（秒）

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()
