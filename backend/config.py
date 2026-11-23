"""应用配置 - 分两层文件

- 部署层(随环境变化):项目根 `.env` - 数据库连接,SECRET_KEY,镜像仓库 / CI 凭据,监听地址,TZ.
- 业务层(随版本发布):`config/app.env` - 超时 / 缓存 TTL / 间隔 / 截断 / 命名空间 / 角色等.
- 优先级:进程环境变量 > 根 .env > config/app.env > 字段默认值.
"""

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    db_driver: str = ""
    db_path: str = ""
    db_host: str = ""
    db_port: int = 3306
    db_name: str = ""
    db_user: str = ""
    db_pass: str = ""
    db_pool_max: int = 10
    db_pool_min: int = 2

    # ── 加密密钥(容器部署必填;裸机可留空自动生成)──
    secret_key: str = ""

    # ── 会话与访问控制 ──
    auth_token_ttl_hours: int = 24
    cookie_secure: bool = False
    allow_empty_systems: bool = False

    # ── OIDC 登录(唯一登录方式;三项必填)──
    oidc_issuer: str = ""
    oidc_client_id: str = "cd"
    oidc_client_secret: str = Field(default="", validation_alias="CD_OIDC_SECRET")
    oidc_redirect_uri: str = Field(default="", validation_alias="CD_OIDC_REDIRECT_URI")
    oidc_scopes: str = "openid profile email"
    oidc_revalidate_seconds: int = 60
    oidc_stale_grace_seconds: int = 600
    oidc_id_token_max_skew: int = 60

    # ── 部署登录凭据(仅用于部署时 docker login 拉取私有镜像)──
    registry_user: str = ""
    registry_password: str = ""

    # ── OIDC 自检──

    @field_validator("oidc_issuer", mode="after")
    @classmethod
    def _strip_issuer_trailing_slash(cls, v: str) -> str:
        """去掉尾部斜杠(issuer 与 discovery 的 issuer 精确比对)"""
        return (v or "").strip().rstrip("/")

    def oidc_missing_settings(self) -> list[str]:
        """返回 oidc 模式必需但为空的环境变量名"""
        missing = []
        if not self.oidc_issuer:
            missing.append("OIDC_ISSUER")
        if not self.oidc_client_secret:
            missing.append("CD_OIDC_SECRET")
        if not self.oidc_redirect_uri:
            missing.append("CD_OIDC_REDIRECT_URI")
        return missing

    def oidc_ready(self) -> bool:
        """三项必填(OIDC_ISSUER / CD_OIDC_SECRET / CD_OIDC_REDIRECT_URI)是否齐备"""
        return not self.oidc_missing_settings()

    # ── 监听(可选)──
    host: str = "0.0.0.0"  # nosec
    port: int = 8081

    # ── 反向代理可信跳数 ──
    trusted_proxy_hops: int = 0

    # ── 角色 ──
    super_admin_role: str = "super_admin"

    # ── SSH(可选)──
    ssh_timeout: int = 30
    ssh_keepalive: int = 30
    ssh_auto_trust: bool = False

    # ── Docker 部署(可选)──
    container_restart_policy: str = "always"
    registry_insecure_auto: bool = False

    # ── 部署重试(可选)──
    deploy_retry_attempts: int = 3
    deploy_retry_delay: float = 2.0

    # ── K8s 部署(可选)──
    flux_namespace: str = "flux-system"
    k8s_helm_timeout: int = 300
    k8s_rollout_timeout: int = 120
    argocd_verify_tls: bool = False

    # ── 通知(可选)──
    dingtalk_secret: str = ""
    log_truncate_chars: int = 20000
    notify_truncate_chars: int = 200

    # ── 镜像仓库同步(可选)──
    registry_sync_interval: int = 30  # 分钟,0 关闭

    # ── CI 集成(必填:身份 / 权限 / 契约闸门全走 Glue API)──
    ci_api_url: str = ""
    ci_admin_user: str = ""
    ci_admin_pass: str = ""
    ci_api_token: str = ""
    cd_api_token: str = ""
    ci_timeout: int = 30
    ci_contract_ttl: int = 30

    # ── 出站地址族优先级(可选)──
    http_ip_family: str = "ipv4"

    @field_validator("http_ip_family", mode="after")
    @classmethod
    def _validate_ip_family(cls, v: str) -> str:
        """HTTP_IP_FAMILY 白名单校验:忽略大小写与首尾空格,取值非法即报错"""
        family = (v or "").strip().lower() or "ipv4"
        if family not in ("ipv4", "ipv6", "auto"):
            raise ValueError(f"HTTP_IP_FAMILY 只支持 ipv4 / ipv6 / auto，当前为 {v!r} / must be ipv4, ipv6 or auto")
        return family

    # ── 监控(可选)──
    monitoring_enabled: bool = True
    monitor_cache_servers: int = 60
    monitor_cache_system: int = 30
    monitor_cache_nodes: int = 30
    monitor_cache_pods: int = 30
    monitor_cache_docker: int = 30
    monitor_cache_pod_detail: int = 15
    alert_check_interval: int = 60

    # ── 时区(影响前端展示与定时输入换算)──
    display_timezone: str = ""

    model_config = SettingsConfigDict(
        env_file=(BASE_DIR / "config" / "app.env", BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
