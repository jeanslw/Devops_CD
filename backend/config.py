"""应用配置 - 分两层文件,不要直接修改此文件

- 部署层(随环境变化):项目根 `.env` - 数据库连接,SECRET_KEY,Harbor / CI 凭据,监听地址,TZ.
  Docker 部署时由 docker-compose.yml / Devops-Glue 的 env_file 注入容器(镜像内不含 .env).
- 业务层(随版本发布):`config/app.env` - 超时 / 缓存 TTL / 间隔 / 截断 / 命名空间 / 角色等.
- 优先级:进程环境变量 > 项目根 .env > config/app.env > 下方字段默认值.
"""

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录(本文件位于 <root>/backend/config.py)- 用绝对路径,裸机 / 容器 / 任意工作目录一致
BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    # ── 数据库(必填: sqlite | mysql)──
    db_driver: str = ""
    db_path: str = ""  # SQLite 模式必填,MySQL 模式忽略
    db_host: str = ""
    db_port: int = 3306
    db_name: str = ""
    db_user: str = ""
    db_pass: str = ""
    db_pool_max: int = 10  # MySQL 连接池最大值
    db_pool_min: int = 2  # MySQL 连接池最小值(常开连接数)

    # ── 加密密钥(可选)──
    # 用于加密 cd_servers 的 password / ssh_key;留空则自动生成 .cd_secret_key 文件.
    # 仅裸机可留空:compose 为 read_only 容器,密钥文件写不出来,由 env-check 强制必填.
    secret_key: str = ""

    # ── 认证 token(可选)──
    # 会话 token 有效期(小时),默认 24.token 为不透明随机串,服务端 cd_sessions 表存
    # SHA-256 摘要,退出登录(/api/logout)删行即时吊销.
    auth_token_ttl_hours: int = 24

    # HttpOnly 会话 cookie 是否加 Secure(仅 HTTPS 下浏览器才回传 cookie).
    # 服务在 nginx 之后以 HTTPS 对外时设为 true;直连 HTTP 暴露时保持 false.
    cookie_secure: bool = False

    # ── 认证访问控制 ──
    # admin_users.systems 为空时是否放行 CD 访问.
    # true(默认)兼容历史数据;严格环境设 false,即 deny-by-default,空 systems 无 CD 权限.
    allow_empty_systems: bool = True

    # ── 认证模式:唯一总开关(见 docs/SSO_OIDC_ZH.md §6)──
    # oidc(默认)= Devops-Glue 单点登录(BFF 机密客户端):身份与权限经 Glue HTTP API 获取,
    #               请求路径上不读共享表.
    # local = 本地密码登录 + 每请求直读共享表(admin_users / roles / role_permissions).
    # 切档 / 回退均为改这一行 + 重启,无需回滚镜像与 DDL.
    # oidc 模式的必填项(见 oidc_missing_settings):缺任一项时启动日志记 ERROR,
    # 登录页点击 SSO 会提示缺少的项目(见 docs/SSO_OIDC_ZH.md §6.2).
    auth_mode: str = "oidc"  # oidc | local(白名单校验,见下方 validator)
    # Glue 对外地址,必须与其 discovery 的 issuer 完全一致(不一致即拒绝登录并打印两侧取值).
    oidc_issuer: str = ""
    # OAuth 客户端标识:Glue oauth_clients['cd'] 的键名,v1 固定为 cd.
    oidc_client_id: str = "cd"
    # 机密客户端密钥,与 Glue oauth_clients['cd']['secret'] 完全一致(根 .env,不进镜像).
    oidc_client_secret: str = Field(default="", validation_alias="CD_OIDC_SECRET")
    # 回调地址,与 Glue 侧登记值逐字符一致(Glue 用 hash_equals 比对,含 scheme/host/port/path).
    oidc_redirect_uri: str = Field(default="", validation_alias="CD_OIDC_REDIRECT_URI")
    # scope 必须含 openid:漏掉不报错,只是响应里没有 id_token(身份拿不到),见 §8.7 第 3 条.
    oidc_scopes: str = "openid profile email"
    # 惰性复核窗口(秒):已登录会话距上次 Glue 复核超过该值才重新查询一次,
    # 即停用 / 降权的生效上限,不必等会话 TTL(见 §3.2 / §4.3).
    oidc_revalidate_seconds: int = 60
    # Glue 不可达时的软失败窗口(秒):窗口内按 last-known-good 身份放行,超过即 fail-closed;0 = 立即 fail-closed.
    oidc_stale_grace_seconds: int = 600
    # 时钟偏移容忍(秒):校验 id_token 的 iat / exp 时按此放宽,避免双方机器时钟微差导致登录失败.
    oidc_id_token_max_skew: int = 60

    # ── Harbor 镜像仓库(必填)──
    # 统一读 HARBOR_BASE_URL(带 scheme 的完整地址,如 https://hub.example.com):CI(Glue) 直接使用;
    # CD 内部剥掉 scheme 供 Docker 镜像引用,HarborClient 自动探测 https → http.
    harbor_registry: str = Field(default="", validation_alias="HARBOR_BASE_URL")
    harbor_user: str = ""
    harbor_password: str = ""
    # Harbor HTTPS 证书校验开关;Harbor 使用自签名证书时设 HARBOR_VERIFY_SSL=false.
    harbor_verify_ssl: bool = True

    @field_validator("harbor_registry", mode="after")
    @classmethod
    def _strip_harbor_scheme(cls, v: str) -> str:
        """剥掉 http(s):// 与尾部斜杠,得到 scheme-less 的 registry host"""
        v = (v or "").strip().rstrip("/")
        for prefix in ("https://", "http://"):
            if v.startswith(prefix):
                return v[len(prefix) :]
        return v

    # ── 认证模式校验与自检(fail-closed,见 docs/SSO_OIDC_ZH.md §6.3)──

    @field_validator("auth_mode", mode="after")
    @classmethod
    def _validate_auth_mode(cls, v: str) -> str:
        """AUTH_MODE 白名单校验:拼错的模式名立刻报错,不静默退化成 local.

        静默退化比启动失败危险:运维以为切到了 SSO,实际每个请求仍在读共享表.
        带空格的写法('oidc ')按可用处理,故先 strip + lower.
        """
        mode = (v or "").strip().lower()
        if mode not in ("local", "oidc"):
            raise ValueError(f"AUTH_MODE 只支持 local / oidc，当前为 {v!r} / AUTH_MODE must be local or oidc")
        return mode

    @field_validator("oidc_issuer", mode="after")
    @classmethod
    def _strip_issuer_trailing_slash(cls, v: str) -> str:
        """去掉尾部斜杠:issuer 要与 discovery 的 issuer 精确比对,末尾多一个 / 会误判为不一致."""
        return (v or "").strip().rstrip("/")

    def oidc_missing_settings(self) -> list[str]:
        """返回 oidc 模式必需但为空的环境变量名(供启动日志 / 预检脚本 / 登录页提示)."""
        missing = []
        if not self.oidc_issuer:
            missing.append("OIDC_ISSUER")
        if not self.oidc_client_secret:
            missing.append("CD_OIDC_SECRET")
        if not self.oidc_redirect_uri:
            missing.append("CD_OIDC_REDIRECT_URI")
        return missing

    def oidc_ready(self) -> bool:
        """AUTH_MODE=oidc 时三项必填是否齐备.

        local 模式恒为 True(这些项与它无关).oidc 模式缺任一项即 False:compose 的 env-check
        据此中止启动,应用侧记 ERROR 日志(半配的 OIDC 会用公开默认密钥带病运行).
        """
        if self.auth_mode != "oidc":
            return True
        return not self.oidc_missing_settings()

    # ── 服务(可选)──
    host: str = "0.0.0.0"  # nosec
    port: int = 8081

    # ── 反向代理可信跳数(可选,默认 0)──
    # 0 = 不信任 X-Forwarded-For,客户端 IP 取 TCP 对端(直连暴露时保持 0);位于 nginx 等反向
    # 代理之后时设为 1(多层代理按实际层数增大),登录失败锁定据此取真实客户端 IP(与 CI 共享计数).
    trusted_proxy_hops: int = 0

    # ── 角色(仅用于登录身份的角色判断;具体权限以 roles / permissions / role_permissions 表为准)──
    # 超级管理员角色名:隐含全部权限.
    super_admin_role: str = "super_admin"

    # ── SSH(可选)──
    ssh_timeout: int = 30
    ssh_keepalive: int = 30  # 0 关闭 keepalive
    ssh_auto_trust: bool = False  # 已废弃,仅为兼容旧配置保留;代码始终使用 RejectPolicy + known_hosts
    # 用户须先调用 trust_ssh_host 把 host key 存入 ~/.cd_service/known_hosts

    # ── Docker 部署(可选)──
    container_restart_policy: str = "always"

    # ── 部署重试(可选)──
    # 幂等命令(docker compose up -d / kubectl apply)失败时的重试入口,用于消化瞬时 SSH / 网络抖动.
    # attempts 为含首次在内的最大执行次数(=1 关闭重试),delay 为两次尝试的间隔秒数.
    deploy_retry_attempts: int = 3
    deploy_retry_delay: float = 2.0

    # ── K8s 部署(可选)──
    flux_namespace: str = "flux-system"
    k8s_helm_timeout: int = 300  # helm upgrade --install --timeout(秒)
    k8s_rollout_timeout: int = 120  # kubectl rollout status --timeout(秒)
    # ArgoCD API 是否校验 TLS 证书:ArgoCD 始终走 HTTPS,自签名证书环境保持 false,
    # 证书受信时设 true,防中间人截获 Bearer token.
    argocd_verify_tls: bool = False

    # ── 通知(可选)──
    dingtalk_secret: str = ""  # 钉钉加签密钥
    log_truncate_chars: int = 20000
    notify_truncate_chars: int = 200

    # ── 镜像仓库同步(可选)──
    registry_sync_interval: int = 30  # 分钟,0 关闭

    # ── CI 集成(可选)──
    # CI 服务地址(如 http://localhost:8080);留空则不启用构建管理功能.
    ci_api_url: str = ""
    ci_admin_user: str = ""
    ci_admin_pass: str = ""
    # API token(dg_ 前缀,服务账号 / 第三方调用),配置后优先于账号密码登录
    ci_api_token: str = ""
    ci_timeout: int = 30

    # ── 监控(可选)──
    monitoring_enabled: bool = True
    monitor_cache_servers: int = 60
    monitor_cache_system: int = 30
    monitor_cache_nodes: int = 30
    monitor_cache_pods: int = 30
    monitor_cache_docker: int = 30
    monitor_cache_pod_detail: int = 15
    alert_check_interval: int = 60  # 告警检测间隔(秒)

    # 两个 env 文件都允许缺失(缺失即跳过):Docker 镜像内没有 .env,配置由 compose 注入进程环境变量.
    # 同名变量以元组中靠后的文件生效,因此 .env 放最后 → 根 .env 始终能覆盖 config/app.env.
    # extra="ignore":pydantic-settings 默认 extra="forbid",env 文件里与本服务无关的键(如根 .env 的
    # TZ,Devops-Glue 注入的 DB_ROOT_PASS)会被当作额外字段直接报错.
    model_config = SettingsConfigDict(
        env_file=(BASE_DIR / "config" / "app.env", BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
