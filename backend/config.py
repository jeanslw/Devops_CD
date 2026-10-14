"""应用配置 - 分两层文件,不要直接修改此文件

- 部署层(随环境变化):项目根 `.env` - 数据库连接,SECRET_KEY,Harbor / CI 凭据,监听地址,TZ.
  Docker 部署由 docker-compose.yml / Devops-Glue 的 env_file 注入(镜像内不含 .env).
- 业务层(随版本发布):`config/app.env` - 超时 / 缓存 TTL / 间隔 / 截断 / 命名空间 / 角色等.
- 优先级:进程环境变量 > 根 .env > config/app.env > 字段默认值.
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
    # 加密 cd_servers 的 password / ssh_key;留空则自动生成 .cd_secret_key 文件.
    # 仅裸机可留空:compose 是 read_only 容器,密钥文件写不出来,由 env-check 强制必填.
    secret_key: str = ""

    # ── 认证 token(可选)──
    # 会话 token 有效期(小时),默认 24.token 是不透明随机串,库内只存 SHA-256 摘要,
    # 退出登录(/api/logout)删行即时吊销.
    auth_token_ttl_hours: int = 24

    # HttpOnly 会话 cookie 是否加 Secure(仅 HTTPS 下浏览器才回传).
    # 服务在 nginx 之后以 HTTPS 对外时设 true;直连 HTTP 暴露时保持 false.
    cookie_secure: bool = False

    # ── 认证访问控制 ──
    # 身份 systems 为空时是否放行 CD 访问:true(默认)兼容历史数据,false 即 deny-by-default.
    allow_empty_systems: bool = True

    # ── OIDC 登录(Devops-Glue 单点登录,唯一登录方式)──
    # 必填项缺失时启动日志记 ERROR,compose 预检中止启动,登录页点 SSO 提示缺哪些
    # (见 oidc_missing_settings).
    # Glue 对外地址,须与其 discovery 的 issuer 完全一致(不一致即拒绝登录并打印两侧取值).
    oidc_issuer: str = ""
    # OAuth 客户端标识:Glue oauth_clients['cd'] 的键名,v1 固定为 cd.
    oidc_client_id: str = "cd"
    # 机密客户端密钥,与 Glue oauth_clients['cd']['secret'] 完全一致(根 .env,不进镜像).
    oidc_client_secret: str = Field(default="", validation_alias="CD_OIDC_SECRET")
    # 回调地址,与 Glue 侧登记值逐字符一致(Glue 用 hash_equals 比对,含 scheme/host/port/path).
    oidc_redirect_uri: str = Field(default="", validation_alias="CD_OIDC_REDIRECT_URI")
    # scope 须含 openid:漏掉不报错,只是响应没有 id_token(身份拿不到),见 §8.7 第 3 条.
    oidc_scopes: str = "openid profile email"
    # 惰性复核窗口(秒):距上次 Glue 复核超过该值才重查一次,即停用 / 降权的生效上限,
    # 不必等会话 TTL(见 §3.2 / §4.3).
    oidc_revalidate_seconds: int = 60
    # Glue 不可达时的软失败窗口(秒):窗口内按 last-known-good 身份放行,超过即
    # fail-closed;0 = 立即 fail-closed.
    oidc_stale_grace_seconds: int = 600
    # 时钟偏移容忍(秒):校验 id_token 的 iat / exp 按此放宽,容双方机器时钟微差.
    oidc_id_token_max_skew: int = 60

    # ── Harbor 镜像仓库(必填)──
    # 统一读 HARBOR_BASE_URL(带 scheme 的完整地址,如 https://hub.example.com):CI(Glue)
    # 直接使用;CD 剥掉 scheme 供 Docker 镜像引用,HarborClient 自动探测 https → http.
    harbor_registry: str = Field(default="", validation_alias="HARBOR_BASE_URL")
    harbor_user: str = ""
    harbor_password: str = ""
    # Harbor HTTPS 证书校验开关;自签名证书时设 HARBOR_VERIFY_SSL=false.
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

    # ── OIDC 自检──

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
        """三项必填(OIDC_ISSUER / CD_OIDC_SECRET / CD_OIDC_REDIRECT_URI)是否齐备.

        缺任一项即 False:compose 的 env-check 据此中止启动,应用侧记 ERROR(半配的 OIDC
        会用公开默认密钥带病运行).
        """
        return not self.oidc_missing_settings()

    # ── 服务(可选)──
    host: str = "0.0.0.0"  # nosec
    port: int = 8081

    # ── 反向代理可信跳数(可选,默认 0)──
    # 0 = 不信任 X-Forwarded-For,客户端 IP 取 TCP 对端(直连暴露时保持 0);位于 nginx
    # 等反代之后设 1(多层按实际层数增大),登录失败锁定据此取真实 IP(与 CI 共享计数).
    trusted_proxy_hops: int = 0

    # ── 角色 ──
    # 超级管理员角色名:隐含全部权限(权限经 Glue API 读取).
    super_admin_role: str = "super_admin"

    # ── SSH(可选)──
    ssh_timeout: int = 30
    ssh_keepalive: int = 30  # 0 关闭 keepalive
    ssh_auto_trust: bool = False  # 已废弃,仅为兼容旧配置保留;代码始终使用 RejectPolicy + known_hosts
    # 用户须先调用 trust_ssh_host 把 host key 存入 ~/.cd_service/known_hosts

    # ── Docker 部署(可选)──
    container_restart_policy: str = "always"

    # ── 部署重试(可选)──
    # 幂等命令(docker compose up -d / kubectl apply)失败时的重试入口,消化瞬时
    # SSH / 网络抖动.attempts = 含首次在内的最大执行次数(=1 关闭重试),delay = 间隔秒数.
    deploy_retry_attempts: int = 3
    deploy_retry_delay: float = 2.0

    # ── K8s 部署(可选)──
    flux_namespace: str = "flux-system"
    k8s_helm_timeout: int = 300  # helm upgrade --install --timeout(秒)
    k8s_rollout_timeout: int = 120  # kubectl rollout status --timeout(秒)
    # ArgoCD API 是否校验 TLS 证书:ArgoCD 始终走 HTTPS,自签名环境保持 false,
    # 证书受信时设 true(防中间人截获 Bearer token).
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
    # CI→CD 入站静态 token:校验 /api/integration/deploy-logs 的 Bearer,留空即该端点拒绝所有请求
    cd_api_token: str = ""
    ci_timeout: int = 30
    # 集成服务契约(CD 启用状态 / provision)只经 Glue 的窄只读端点
    # GET /api/integration/services/{service} 读取,CD 不读共享库的任何 CI 表 --
    # 契约仍归 CI 写入,消费方只读一个 HTTP 接口,Glue 改库结构 / 将来拆库独立部署都不影响 CD.
    # 端点问不到(Glue 版本过旧 404/405、网络或鉴权失败、CI_API_URL/CI_API_TOKEN 未配置)按
    # "库不可用"处理(503 fail-closed),见 backend/database.py 顶部注释.
    # 契约读的缓存窗口(秒):窗口内复用上次结果 -- CD 每请求都过闸门,不能每请求都打一次 Glue;
    # 0 = 每次请求都问. CI 后台改开关后,CD 最迟一个窗口(默认 30s)后生效.
    # 想立刻复核(排障 / 改完开关马上生效)见 database.invalidate_contract_cache().
    ci_contract_ttl: int = 30

    # ── 出站地址族优先级(可选)──
    # ipv4(默认)= IPv4 候选排最前,失败回退 IPv6;ipv6 = 反过来;auto = 不重排(getaddrinfo 系统顺序).
    # 覆盖进程内所有经 socket 解析的出站连接(requests/urllib3、http.client、SSH),见 backend/net.py.
    http_ip_family: str = "ipv4"

    @field_validator("http_ip_family", mode="after")
    @classmethod
    def _validate_ip_family(cls, v: str) -> str:
        """HTTP_IP_FAMILY 白名单校验:忽略大小写与首尾空格,取值非法即报错."""
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

    # 两个 env 文件都允许缺失(缺失即跳过):镜像内没有 .env,配置由 compose 注入进程环境变量.
    # 同名变量以元组中靠后的文件生效,故 .env 放最后 → 根 .env 总能覆盖 config/app.env.
    # extra="ignore":pydantic-settings 默认 extra="forbid",env 文件里与本服务无关的键(根
    # .env 的 TZ,Glue 注入的 DB_ROOT_PASS)会被当额外字段直接报错.
    model_config = SettingsConfigDict(
        env_file=(BASE_DIR / "config" / "app.env", BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
