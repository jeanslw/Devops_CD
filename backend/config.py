"""应用配置 - 分两层文件

- 部署层(随环境变化):项目根 `.env` - 数据库连接,SECRET_KEY,镜像仓库 / CI 凭据,监听地址,TZ.
- 业务层(随版本发布):`config/app.env` - 超时 / 缓存 TTL / 间隔 / 截断 / 命名空间 / 角色等.
- 优先级:进程环境变量 > 根 .env > config/app.env > 字段默认值.
"""

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录(本文件位于 <root>/backend/config.py)
BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    # ── 数据库(必填;sqlite | mysql)──
    db_driver: str = ""
    db_path: str = ""  # SQLite 库文件路径(sqlite 必填,mysql 忽略)
    db_host: str = ""
    db_port: int = 3306
    db_name: str = ""
    db_user: str = ""
    db_pass: str = ""
    db_pool_max: int = 10  # MySQL 连接池上限
    db_pool_min: int = 2  # MySQL 连接池常开连接数

    # ── 加密密钥(容器部署必填;裸机可留空自动生成)──
    # 加密 cd_servers 的 password / ssh_key;留空时首次运行生成 .cd_secret_key 文件(需可写目录)
    secret_key: str = ""

    # ── 会话与访问控制 ──
    # 会话 token 有效期(小时);库内只存 SHA-256 摘要,logout 删行即时吊销
    auth_token_ttl_hours: int = 24
    # 会话 cookie 是否加 Secure
    cookie_secure: bool = False
    # Glue 侧 systems 为空时是否放行 CD 访问;默认 false(deny-by-default:空 systems 一律拒绝)
    allow_empty_systems: bool = False

    # ── OIDC 登录(Devops-Glue 单点登录,唯一登录方式;三项必填)──
    # Glue 对外地址,须与其 discovery 的 issuer 完全一致
    oidc_issuer: str = ""
    # OAuth 客户端标识 = Glue oauth_clients['cd'] 的键名
    oidc_client_id: str = "cd"
    # 客户端密钥,与 Glue oauth_clients['cd']['secret'] 一致
    oidc_client_secret: str = Field(default="", validation_alias="CD_OIDC_SECRET")
    # 回调地址,与 Glue 侧登记值逐字符一致(含 scheme/host/port/path)
    oidc_redirect_uri: str = Field(default="", validation_alias="CD_OIDC_REDIRECT_URI")
    # scope 须含 openid
    oidc_scopes: str = "openid profile email"
    # 惰性复核窗口(秒):停用 / 降权的生效上限
    oidc_revalidate_seconds: int = 60
    # Glue 不可达软失败窗口(秒):窗口内按 last-known-good 放行,超过 fail-closed;0 = 立即
    oidc_stale_grace_seconds: int = 600
    # id_token iat / exp 校验的时钟偏移容忍(秒)
    oidc_id_token_max_skew: int = 60

    # ── 部署登录凭据(仅用于部署时 docker login 拉取私有镜像)──
    # 镜像仓库「查询」已改走 Glue API(见 services/registry_provider.py),不再直连,
    # 故本地不再保存仓库地址 / 类型 / TLS 校验;仅保留部署登录所需的账号密码
    # (适用于 Harbor 与标准 OCI registry,二者认证字段通用)。
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
    super_admin_role: str = "super_admin"  # 超级管理员角色名,隐含全部权限(权限经 Glue API 读取)

    # ── SSH(可选)──
    ssh_timeout: int = 30
    ssh_keepalive: int = 30  # 0 关闭 keepalive
    ssh_auto_trust: bool = False  # 已废弃,仅为兼容旧配置保留

    # ── Docker 部署(可选)──
    container_restart_policy: str = "always"
    # 明文 HTTP 仓库(契约 registry_url 为 http://)的目标机 dockerd 放行策略:
    #   false(默认)= 部署前只检测;未放行则中止并输出可复制的修复命令,绝不修改目标机任何文件.
    #   true        = 未放行时由 CD 备份/合并目标机 /etc/docker/daemon.json 的 insecure-registries,
    #                 再 reload(失败回退 restart)dockerd.注意 restart 会重启该机所有容器.
    # docker CLI 无法强制明文(--tls-verify 是 podman/skopeo 的),故只能落在目标机配置上.
    registry_insecure_auto: bool = False

    # ── 部署重试(可选)──
    # 幂等命令(docker compose up -d / kubectl apply)失败时重试:attempts = 最大执行次数(=1 关闭),delay = 间隔秒
    deploy_retry_attempts: int = 3
    deploy_retry_delay: float = 2.0

    # ── K8s 部署(可选)──
    flux_namespace: str = "flux-system"
    k8s_helm_timeout: int = 300  # helm upgrade --install --timeout(秒)
    k8s_rollout_timeout: int = 120  # kubectl rollout status --timeout(秒)
    # ArgoCD API 是否校验 TLS 证书
    argocd_verify_tls: bool = False

    # ── 通知(可选)──
    dingtalk_secret: str = ""  # 钉钉加签密钥
    log_truncate_chars: int = 20000
    notify_truncate_chars: int = 200

    # ── 镜像仓库同步(可选)──
    registry_sync_interval: int = 30  # 分钟,0 关闭

    # ── CI 集成(必填:身份 / 权限 / 契约闸门全走 Glue API)──
    # Glue API 地址(如 http://localhost:8080)
    ci_api_url: str = ""
    ci_admin_user: str = ""
    ci_admin_pass: str = ""
    # API token(dg_ 前缀),优先于账号密码
    ci_api_token: str = ""
    # CI→CD 入站静态 token:校验 /api/integration/deploy-logs 的 Bearer,留空则该端点拒绝所有请求
    cd_api_token: str = ""
    ci_timeout: int = 30  # Glue API 调用超时(秒)
    # 契约读缓存窗口(秒,0 = 每请求都问):改开关最迟一个窗口生效;端点问不到按「契约不可达」fail-closed
    ci_contract_ttl: int = 30

    # ── 出站地址族优先级(可选)──
    # ipv4(默认)= IPv4 候选排最前,失败回退 IPv6;ipv6 = 反过来;auto = 不重排(getaddrinfo 系统顺序)
    # 覆盖进程内所有经 socket 解析的出站连接(requests/urllib3、http.client、SSH)
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
    alert_check_interval: int = 60  # 告警检测间隔(秒)

    model_config = SettingsConfigDict(
        env_file=(BASE_DIR / "config" / "app.env", BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
