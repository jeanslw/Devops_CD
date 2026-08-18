"""CI HTTP 客户端 — 对照 CI OpenAPI (/api/openapi.json) 实现"""

import logging
import threading
import time
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger("ci_client")

# ── CI API 路径（来源：CI 的 /api/openapi.json）──
API = {
    "login": "/api/admin/login",  # POST {user, password} → {token}
    "projects": "/api/build/jobs/list",  # GET → [{job_name, ci_provider, project_id, current_path}]
    "builds": "/api/build/{path}/pipelines",  # GET → {build_provider, project_id, pipelines: [...]}
    "trigger": "/api/build/{path}/trigger",  # POST {ref, variables} → trigger result
    "log": "/api/build/{path}/logs/{id}",  # GET → text/plain
    "variables": "/api/build/{path}/variables",  # GET → {key: options}
    "branches": "/api/build/{path}/branches",  # GET → ["main", "master", ...]
    "retry": "/api/build/{path}/pipelines/{id}/retry",  # POST → 重试 Pipeline（仅 GitLab CI）
    "cancel": "/api/build/{path}/pipelines/{id}/cancel",  # POST → 取消 Pipeline（仅 GitLab CI）
}


class CiClientError(Exception):
    """CI API 调用异常"""


class CiClient:
    """CI HTTP 客户端，线程安全。

    两种认证模式：
      - API token 模式（推荐）：构造时传入 `token`（dg_ 前缀），直接作为 Bearer token 使用，
        不登录、不刷新，token 由 CI 管理界面的「API 管理」创建，适合服务账号 / 第三方。
      - 账号模式（回退）：未传 token 时用 username/password 登录，自动管理 24h 登录 token。
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
        # API token 模式：token 已在构造时确定，无需登录/刷新
        self._token: str | None = token or None
        self._static_token: bool = bool(token)
        self._token_expiry: float = 0
        self._lock = threading.Lock()

        retry = Retry(total=2, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
        self._session = requests.Session()
        self._session.mount("http://", HTTPAdapter(max_retries=retry))
        self._session.mount("https://", HTTPAdapter(max_retries=retry))

    # ── URL 构造（处理含 / 的 path 参数）──

    def _url(self, template: str, **kwargs) -> str:
        """构造完整 URL — 直接替换，不二次编码（前端/上游已 encodeURIComponent）"""
        for k, v in kwargs.items():
            template = template.replace(f"{{{k}}}", str(v))
        return f"{self._base}{template}"

    # ── 认证 ──

    def _login(self) -> str:
        """POST /api/admin/login — CI 使用 user 字段（非 username）"""
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
        """确保有有效 token。

        API token 模式：token 固定，直接返回。
        账号模式：过期前 60 秒自动刷新。
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

    def _get(self, url: str, params: dict | None = None) -> Any:
        self._ensure_token()
        try:
            resp = self._session.get(url, params=params, headers=self._auth_headers(), timeout=self._timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 401:
                self._force_relogin()
                resp = self._session.get(url, params=params, headers=self._auth_headers(), timeout=self._timeout)
                resp.raise_for_status()
                return resp.json()
            raise CiClientError(f"CI API GET 失败 [{url}]: {e}") from e

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
        # API token 模式无账号可重登：401 即 token 失效（撤销/过期），直接报错
        if self._static_token:
            raise CiClientError("CI API token 无效（已撤销或过期），请在 CI「API 管理」重新生成")
        with self._lock:
            self._token = None
        self._ensure_token()

    # ── 业务方法（一一对应 CI OpenAPI）──

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

    def trigger_build(self, project: str, ref: str, variables: dict | None = None) -> Any:
        """POST /api/build/{path}/trigger — body: {ref, variables}"""
        body: dict[str, Any] = {"ref": ref}
        if variables:
            body["variables"] = variables
        return self._post(self._url(API["trigger"], path=project), body)

    def get_build_log(self, project: str, build_id: int | str) -> str:
        """GET /api/build/{path}/logs/{id} → text/plain"""
        return self._get_text(self._url(API["log"], path=project, id=build_id))

    def get_variables(self, project: str) -> Any:
        """GET /api/build/{path}/variables?format=json → 完整含 build_provider"""
        result = self._get(self._url(API["variables"], path=project), params={"format": "json"})
        if isinstance(result, dict) and "data" in result:
            return result["data"]
        return result

    def get_branches(self, project: str) -> Any:
        """GET /api/build/{path}/branches → ["main", "master", ...]"""
        result = self._get(self._url(API["branches"], path=project))
        if isinstance(result, dict) and "data" in result:
            return result["data"]
        return result

    def retry_pipeline(self, project: str, build_id: int | str) -> Any:
        """POST /api/build/{path}/pipelines/{id}/retry — 重试 Pipeline（仅 GitLab CI）"""
        return self._post(self._url(API["retry"], path=project, id=build_id), {})

    def cancel_pipeline(self, project: str, build_id: int | str) -> Any:
        """POST /api/build/{path}/pipelines/{id}/cancel — 取消 Pipeline（仅 GitLab CI）"""
        return self._post(self._url(API["cancel"], path=project, id=build_id), {})


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
        # 优先 API token；未配置时回退账号密码登录
        _client_instance = CiClient(
            base_url=settings.ci_api_url,
            username=settings.ci_admin_user,
            password=settings.ci_admin_pass,
            token=settings.ci_api_token or None,
            timeout=settings.ci_timeout,
        )
        return _client_instance
