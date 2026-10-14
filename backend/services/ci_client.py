"""CI HTTP 客户端 - 对照 CI OpenAPI (/api/openapi.json) 实现"""

import logging
import threading
import time
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger("ci_client")

# Glue 无 /refs 路由(版本过旧)时的负缓存窗口(秒):窗口内不再重试该端点,直接走 /branches.
# 404 无法区分"路由不存在"(老 Glue)与"仓库映射缺失"(新 Glue 的 git.query_failed),故一律按
# 前者处理:负缓存避免每次开弹窗都白吃一次 404;真故障时 /branches 会同样失败,错误照旧上抛.
_REFS_MISSING_TTL = 300.0
# 契约读(GET /api/integration/services/{service})的超时(秒):在请求路径上,问不到即快速失败.
_CONTRACT_TIMEOUT = 5.0

# ── CI API 路径(来源:CI 的 /api/openapi.json)──
API = {
    "login": "/api/admin/login",  # POST {user, password} → {token}
    "projects": "/api/build/jobs/list",  # GET → [{job_name, ci_provider, project_id, current_path}]
    "builds": "/api/build/{path}/pipelines",  # GET → {build_provider, project_id, pipelines: [...]}
    "projects_full": "/api/build/projects",  # GET → [{job_name, ..., latest_tag, latest_pipeline, tag_time}]
    "tags": "/api/build/{path}/tags",  # GET → {items, total, page, page_size, total_pages}
    "trigger": "/api/build/{path}/trigger",  # POST {ref, variables} → trigger result
    "log": "/api/build/{path}/logs/{id}",  # GET → text/plain(id=job id)
    "pipeline_log": "/api/build/{path}/pipelines/{id}/logs",  # GET → text/plain(Gitea 专属:id=run id)
    "variables": "/api/build/{path}/variables",  # GET → {key: options}
    "branches": "/api/build/{path}/branches",  # GET → ["main", "master", ...]
    "refs": "/api/git/{path}/refs",  # GET → {"branches": [...], "tags": [...]}(Glue ≥ 2.9.0)
    "service_contract": "/api/integration/services/{service}",  # GET → {service, enabled, provision, token_active, updated_at}
    "retry": "/api/build/{path}/pipelines/{id}/retry",  # POST → 重试 Pipeline(仅 GitLab CI)
    "cancel": "/api/build/{path}/pipelines/{id}/cancel",  # POST → 取消 Pipeline(仅 GitLab CI)
    "rbac_users": "/api/rbac/users",  # GET 用户列表(账号写操作在 CI 侧完成)
    "rbac_roles": "/api/rbac/roles",  # GET 角色目录 → {roles: [{name, description}]}
}


class CiClientError(Exception):
    """CI API 调用异常.

    status_code:   上游 HTTP 状态码(写接口错误映射用,如 400/403/404/409;读接口为 None)
    remote_message: 上游 jsonError 返回的 message(已翻译,仅诊断,非稳定标识)
    """

    def __init__(self, message: str, status_code: int | None = None, remote_message: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.remote_message = remote_message


class CiClient:
    """CI HTTP 客户端,线程安全.

    两种认证模式:
      - API token 模式(推荐):构造时传入 `token`(dg_ 前缀),直接作为 Bearer token 使用,
        不登录,不刷新,token 由 CI 管理界面的'API 管理'创建,适合服务账号 / 第三方.
      - 账号模式(回退):未传 token 时用 username/password 登录,自动管理 24h 登录 token.
    """

    def __init__(
        self,
        base_url: str,
        username: str = "",
        password: str = "",
        token: str | None = None,
        timeout: int = 30,
    ):
        self._base = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._timeout = timeout
        # API token 模式:token 已在构造时确定,无需登录/刷新
        self._token: str | None = token or None
        self._static_token: bool = bool(token)
        self._token_expiry: float = 0
        self._lock = threading.Lock()
        # 已知该 Glue 没有 /refs 路由时的截止时间戳(秒):窗口内直接走 /branches(见 get_refs).
        # 只读写一个 float,竞态无害(最坏情况是多发一次 404).
        self._refs_missing_until: float = 0.0

        retry = Retry(total=2, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
        self._session = requests.Session()
        self._session.mount("http://", HTTPAdapter(max_retries=retry))
        self._session.mount("https://", HTTPAdapter(max_retries=retry))

    # ── URL 构造(处理含 / 的 path 参数)──

    def _url(self, template: str, **kwargs) -> str:
        """构造完整 URL - 直接替换,不二次编码(前端/上游已 encodeURIComponent)"""
        for k, v in kwargs.items():
            template = template.replace(f"{{{k}}}", str(v))
        return f"{self._base}{template}"

    # ── 认证 ──

    def _login(self) -> str:
        """POST /api/admin/login - CI 使用 user 字段(非 username)"""
        resp = self._session.post(
            self._url(API["login"]),
            json={"user": self._username, "password": self._password},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        token = data.get("token")
        if not token:
            raise CiClientError("CI 登录响应缺少 token 字段")
        return token

    def _ensure_token(self):
        """确保有有效 token.

        API token 模式:token 固定,直接返回.
        账号模式:过期前 60 秒自动刷新.
        """
        if self._static_token:
            return
        with self._lock:
            if self._token and time.time() < self._token_expiry - 60:
                return
            try:
                self._token = self._login()
                self._token_expiry = time.time() + 86400  # CI token 24h
                logger.info("CI token 已刷新")
            except requests.RequestException as e:
                raise CiClientError(f"CI 登录失败: {e}") from e

    # ── 通用请求 ──

    def _get(self, url: str, params: dict | None = None, timeout: float | None = None) -> Any:
        """GET + 解析 JSON.timeout 只在个别'请求路径上'的调用点收紧(见 get_service_contract)."""
        self._ensure_token()
        budget = timeout or self._timeout
        try:
            resp = self._session.get(url, params=params, headers=self._auth_headers(), timeout=budget)
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 401:
                self._force_relogin()
                resp = self._session.get(url, params=params, headers=self._auth_headers(), timeout=budget)
                resp.raise_for_status()
                return resp.json()
            status = e.response.status_code if e.response is not None else None
            remote = self._extract_error_message(e.response)
            raise CiClientError(
                f"CI API GET 失败 [{url}]: {remote or e}", status_code=status, remote_message=remote
            ) from e

    def _post(self, url: str, body: dict) -> Any:
        self._ensure_token()
        try:
            resp = self._session.post(url, json=body, headers=self._auth_headers(), timeout=self._timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 401:
                self._force_relogin()
                resp = self._session.post(url, json=body, headers=self._auth_headers(), timeout=self._timeout)
                resp.raise_for_status()
                return resp.json()
            raise CiClientError(f"CI API POST 失败 [{url}]: {e}") from e

    @staticmethod
    def _extract_error_message(resp) -> str | None:
        """从错误响应体提取 message(CI jsonError 返回 {code, message})."""
        if resp is None:
            return None
        try:
            data = resp.json()
        except Exception:
            return None
        if isinstance(data, dict):
            return data.get("message") or data.get("detail")
        return None

    def _get_text(self, url: str) -> str:
        self._ensure_token()
        try:
            resp = self._session.get(url, headers=self._auth_headers(), timeout=self._timeout)
            resp.raise_for_status()
            return resp.text
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 401:
                self._force_relogin()
                resp = self._session.get(url, headers=self._auth_headers(), timeout=self._timeout)
                resp.raise_for_status()
                return resp.text
            raise CiClientError(f"CI API GET 失败 [{url}]: {e}") from e

    def _auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}"}

    def _force_relogin(self):
        # API token 模式无账号可重登:401 即 token 失效(撤销/过期),直接报错
        if self._static_token:
            raise CiClientError("CI API token 无效（已撤销或过期），请在 CI「API 管理」重新生成")
        with self._lock:
            self._token = None
        self._ensure_token()

    # ── 业务方法(一一对应 CI OpenAPI)──

    def list_projects(self) -> Any:
        """GET /api/build/jobs/list?format=json → [{job_name, ci_provider, project_id, current_path}]"""
        result = self._get(self._url(API["projects"]), params={"format": "json"})
        # CI 实际返回 {"data": [...]}
        if isinstance(result, dict) and "data" in result:
            return result["data"]
        return result

    def get_builds(self, project: str) -> Any:
        """GET /api/build/{path}/pipelines?format=json → {build_provider, project_id, pipelines: [...]}"""
        result = self._get(self._url(API["builds"], path=project), params={"format": "json"})
        # CI 可能返回 {"data": {...}} 包装
        if isinstance(result, dict) and "data" in result and isinstance(result["data"], dict):
            return result["data"]
        return result

    def get_projects(self) -> Any:
        """GET /api/build/projects?format=json → [{job_name, build_provider, current_path, harbor_repository, git_platform, latest_tag, latest_pipeline, tag_time}]"""
        result = self._get(self._url(API["projects_full"]), params={"format": "json"})
        if isinstance(result, dict) and "data" in result:
            return result["data"]
        return result

    def get_tags(self, project: str, page: int = 1, page_size: int = 50) -> Any:
        """GET /api/build/{path}/tags?format=json → {items, total, page, page_size, total_pages}"""
        result = self._get(
            self._url(API["tags"], path=project),
            params={"format": "json", "page": page, "page_size": page_size},
        )
        if isinstance(result, dict) and "data" in result:
            return result["data"]
        return result

    def trigger_build(self, project: str, ref: str, variables: dict | None = None) -> Any:
        """POST /api/build/{path}/trigger - body: {ref, variables}"""
        body: dict[str, Any] = {"ref": ref}
        if variables:
            body["variables"] = variables
        return self._post(self._url(API["trigger"], path=project), body)

    def get_build_log(self, project: str, build_id: int | str) -> str:
        """GET /api/build/{path}/logs/{id} → text/plain(id=job id)"""
        return self._get_text(self._url(API["log"], path=project, id=build_id))

    def get_pipeline_log(self, project: str, run_id: int | str) -> str:
        """GET /api/build/{path}/pipelines/{id}/logs → text/plain(Gitea 专属:id=run id → 全部 job 日志拼接)"""
        return self._get_text(self._url(API["pipeline_log"], path=project, id=run_id))

    def get_variables(self, project: str) -> Any:
        """GET /api/build/{path}/variables?format=json → 完整含 build_provider"""
        result = self._get(self._url(API["variables"], path=project), params={"format": "json"})
        if isinstance(result, dict) and "data" in result:
            return result["data"]
        return result

    def get_branches(self, project: str) -> Any:
        """GET /api/build/{path}/branches → ['main', 'master', ...]"""
        result = self._get(self._url(API["branches"], path=project))
        if isinstance(result, dict) and "data" in result:
            return result["data"]
        return result

    # ── Git ref 候选值(分支 + Tag)──

    def get_refs(self, project: str) -> dict:
        """GET /api/git/{path}/refs → {"branches": [...], "tags": [...]}(Glue ≥ 2.9.0).

        构建页的 ref 下拉要的是"分支 + Tag"全量候选值(只列分支时无法用 Tag 触发构建),故走 Glue 的
        合并端点:一次 job→仓库映射解析,两组值来自同一仓库快照. **不加 ?format=json**(Glue 的
        output() 只在该参数下才把响应包成 {"data": ...}),与 /api/build/* 的调用方式一致.

        回退:Glue 版本过旧(无该路由 → 404/405)时退回 GET /api/build/{path}/branches,响应形状对齐
        (tags 为空数组),调用方不必分叉. 真实故障(5xx / 鉴权失败 / 网络异常)照旧抛出,不伪装成空表.
        """
        if time.time() >= self._refs_missing_until:
            try:
                return self._normalize_refs(self._get(self._url(API["refs"], path=project)))
            except CiClientError as e:
                if e.status_code not in (404, 405):
                    raise
                self._refs_missing_until = time.time() + _REFS_MISSING_TTL
                logger.warning(
                    "CI /refs 不可用（HTTP %s），%d 秒内回退 /branches：分支可用但无 Tag 候选"
                    "（升级 Devops-Glue 后自动恢复）",
                    e.status_code,
                    int(_REFS_MISSING_TTL),
                )
        return {"branches": self._as_str_list(self.get_branches(project)), "tags": []}

    @staticmethod
    def _as_str_list(value: Any) -> list[str]:
        """把上游的分支 / Tag 清单收敛成字符串数组:非列表、空串、非标量一律丢弃(下拉框不留脏值)."""
        if not isinstance(value, list):
            return []
        return [str(v).strip() for v in value if isinstance(v, (str, int, float)) and str(v).strip()]

    @classmethod
    def _normalize_refs(cls, result: Any) -> dict:
        """归一化 refs 响应:容忍 {"data": {...}} 包装与字段缺失,恒返回 {"branches": [...], "tags": [...]}."""
        if isinstance(result, dict) and isinstance(result.get("data"), dict):
            result = result["data"]
        data = result if isinstance(result, dict) else {}
        return {
            "branches": cls._as_str_list(data.get("branches")),
            "tags": cls._as_str_list(data.get("tags")),
        }

    def get_service_contract(self, service: str) -> Any:
        """GET /api/integration/services/{service} → {service, enabled, provision, token_active, updated_at}.

        契约(CD 是否被授权、以哪种建表方式授权)的窄只读端点,Glue ≥ 2.9.0 提供. 硬约束与
        glue_identity._glue_get 一致:**不加 ?format=json**;**不解析 message 判错**(message 是已翻译
        文本,不是稳定标识),调用方只看状态码与字段.

        状态码语义:
          - 200     → enabled / provision 以响应为准;token_active 仅供展示,不作闸门条件;
          - 404     → 服务名不在 Glue 白名单;
          - 401/403 → token 被撤销 / scope 不足(Glue 停用集成服务时也会撤销 CD 的 token).
        本方法不吞异常;调用方(database._fetch_contract_http)把任何失败按"问不到"→ 503 处理.
        timeout 收紧到 _CONTRACT_TIMEOUT:本调用在请求路径上,快速失败.
        """
        result = self._get(self._url(API["service_contract"], service=service), timeout=_CONTRACT_TIMEOUT)
        if isinstance(result, dict) and isinstance(result.get("data"), dict):
            return result["data"]  # 防御:万一被包了一层
        return result

    def retry_pipeline(self, project: str, build_id: int | str) -> Any:
        """POST /api/build/{path}/pipelines/{id}/retry - 重试 Pipeline(仅 GitLab CI)"""
        return self._post(self._url(API["retry"], path=project, id=build_id), {})

    def cancel_pipeline(self, project: str, build_id: int | str) -> Any:
        """POST /api/build/{path}/pipelines/{id}/cancel - 取消 Pipeline(仅 GitLab CI)"""
        return self._post(self._url(API["cancel"], path=project, id=build_id), {})

    # ── RBAC 只读接口(账号写操作统一在 CI 侧管理,CD 不代理)──

    def list_users(self) -> list[dict]:
        """GET /api/rbac/users → {'users': [{username, role, systems, status}]}(无 password_hash)"""
        result = self._get(self._url(API["rbac_users"]))
        if isinstance(result, dict):
            users = result.get("users")
            return users if isinstance(users, list) else []
        return result if isinstance(result, list) else []

    def list_roles(self) -> list[dict]:
        """GET /api/rbac/roles → {'roles': [{name, description}]}"""
        result = self._get(self._url(API["rbac_roles"]))
        if isinstance(result, dict):
            roles = result.get("roles")
            return roles if isinstance(roles, list) else []
        return result if isinstance(result, list) else []


# ── 单例 ──

_client_instance: CiClient | None = None
_client_lock = threading.Lock()


def get_ci_client() -> CiClient:
    global _client_instance
    if _client_instance is not None:
        return _client_instance
    with _client_lock:
        if _client_instance is not None:
            return _client_instance
        from backend.config import settings

        if not settings.ci_api_url:
            raise CiClientError("CI_API_URL 未配置，请在 .env 中设置")
        # 优先 API token;未配置时回退账号密码登录
        _client_instance = CiClient(
            base_url=settings.ci_api_url,
            username=settings.ci_admin_user,
            password=settings.ci_admin_pass,
            token=settings.ci_api_token or None,
            timeout=settings.ci_timeout,
        )
        return _client_instance
