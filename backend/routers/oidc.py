"""OIDC 登录路由（BFF 机密客户端）— 见 docs/SSO_OIDC_ZH.md §3.1 / §7.1

   GET /api/auth/oidc/start     → 302 到 Glue /oauth/authorize（state/nonce 落单次 cookie）
   GET /api/auth/oidc/callback  → 校验 state → 兑 code → 验 id_token → 准入 → 建会话 → 302 /

失败状态码分四类（§11 测试矩阵第 3 行）：
  · 交互 / 协议不合法（缺 code、state 缺失或不匹配、Glue 拒绝授权）→ 400。state 不匹配可能是
    CSRF，不重定向；
  · Glue 不可用 / 协议不符（discovery、JWKS、token 端点 5xx、错 secret）→ 502；
  · token 端点返回 invalid_grant（授权码已用或超时，Glue 授权码只活 60s）→ 静默重走一次 start，
    带 retry=1 防无限跳转；
  · 业务性失败（账号停用 / 无 CD 权限 / 身份或角色悬空 / OIDC 必填项缺失）→
    302 /login?error=<error_key>，由登录页按 i18n key 展示。

失败的呈现方式按请求类型分流（统一出口 `_json_error`，见 §7.1）：
  · 浏览器导航（点登录按钮 / 跟随 302，`Accept` 含 `text/html`）→ 302 回 `/login?error=<key>`，
    由登录页在卡片下方提示，不让浏览器把 JSON 错误体当页面渲染；
  · XHR / fetch / 脚本（`Accept: */*` 或 application/json）→ 保持 400/502/503 + JSON
    （含 `error_key`，前端 useError 据此翻译）。

local 模式下这两个端点不存在（§6.2）：XHR / 脚本 404；整页导航 302 回 `/login`
（本地模式下登录页就是可用的密码表单）。
"""

import logging
import secrets
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from backend import oidc
from backend.auth import CD_SYSTEM, _create_session, _has_system, _identity_disabled, cd_deploy_gate, get_db
from backend.config import settings
from backend.database import Database
from backend.exceptions import AppException
from backend.services import glue_identity

logger = logging.getLogger("oidc")

router = APIRouter(tags=["auth"])

_STATE_COOKIE = "cd_oidc_state"
_STATE_TTL = 600  # 授权页停留时限（秒）：超时即让 state 失效，重新发起即可
_LABEL = "OIDC 登录"


def _require_oidc_mode(request: Request) -> RedirectResponse | None:
    """local 模式下这两条路由不存在：路由可用性由模式判定（§6.2）。

    · XHR / 脚本 → 404（FastAPI 默认 JSON，语义不变）；
    · 整页导航 → 302 `/login`（不带 error key：local 模式下登录页就是可用的密码表单，
      不能弹「本地登录已停用」这种与模式相反的文案）。旧书签 / 历史记录 / 回退按钮也不会
      把裸 JSON 404 呈现给用户。
    """
    if settings.auth_mode == "oidc":
        return None
    if _is_browser_navigation(request):
        logger.info("%s 在 AUTH_MODE=%s 下被整页访问：回登录页", _LABEL, settings.auth_mode)
        return RedirectResponse("/login", status_code=302)
    raise HTTPException(404, "Not Found")


def _login_redirect(error_key: str, missing: list[str] | None = None) -> RedirectResponse:
    """业务性失败：回登录页并带上 i18n key（并清掉一次性 state cookie）。

    missing 非空时附带缺失的配置项名，登录页把它渲染成 errors.oidc_not_configured 的 {items}。
    """
    query = f"error={error_key}"
    if missing:
        query += f"&missing={quote(','.join(missing), safe='')}"
    resp = RedirectResponse(f"/login?{query}", status_code=302)
    resp.delete_cookie(_STATE_COOKIE, path="/")
    return resp


# 浏览器导航下没有专属 key 时的兜底：400 = 这次请求已失效（重发即可）；502 = 上游不可用
_NAV_FALLBACK_KEY = {400: "errors.sso_state_invalid", 502: "errors.sso_unavailable"}


def _is_browser_navigation(request: Request) -> bool:
    """顶层跳转（点登录按钮 / 跟随 302 / 地址栏回车）还是 XHR / fetch / 脚本？

    浏览器导航必带 `Accept: text/html`（Chrome / Firefox / Safari 一致，302 跟随也保留该头）；
    fetch / XHR / curl 为 `*/*` 或 application/json。用 Accept 而非 Sec-Fetch-*：后者在老浏览器
    与部分代理链路下可能缺失，缺失即误判为非导航，退化成裸 JSON 页。
    """
    headers = getattr(request, "headers", None)
    accept = (headers.get("accept") if headers else "") or ""
    return "text/html" in accept


def _require_oidc_config(request: Request) -> RedirectResponse | None:
    """OIDC 必填项是否齐备：缺失时提示要配的项目（配置问题，不是上游故障）。

    导航下 302 回登录页并带上缺失清单（登录页渲染 errors.oidc_not_configured 的 {items}）；
    XHR / 脚本返回 None，由调用方抛 AppException（503 + error_key + error_params）。
    """
    missing = settings.oidc_missing_settings()
    if not missing:
        return None
    logger.error("%s 缺少必填配置：%s（配置位置见 docs/SSO_OIDC_ZH.md §6.1）", _LABEL, ", ".join(missing))
    if _is_browser_navigation(request):
        return _login_redirect("errors.oidc_not_configured", missing)
    return None


def _raise_not_configured() -> None:
    """必填项缺失（XHR / 脚本路径）：503 + error_key，与导航路径同一份缺失清单。"""
    missing = settings.oidc_missing_settings()
    raise AppException(
        f"OIDC 未配置完成，缺少 {' / '.join(missing)}",
        503,
        error_key="errors.oidc_not_configured",
        error_params={"items": ", ".join(missing)},
    )


def _nav_fail(request: Request, error_key: str) -> RedirectResponse | None:
    """浏览器导航下的失败统一收口：302 回登录页 + i18n key（登录页在卡片下方提示）。

    非导航返回 None：调用方按各自原语义继续（抛 AppException / 返回 JSON）。
    """
    if _is_browser_navigation(request):
        logger.info("%s 失败 → 回登录页提示 %s", _LABEL, error_key)
        return _login_redirect(error_key)
    return None


def _gate(request: Request, db: Database) -> RedirectResponse | None:
    """部署闸门：与 /api/login 同口径 — CD 未启用 / 未部署 / 库不可用时，不进入登录流程。

    导航下直接回登录页（errors.cd_not_enabled / cd_not_deployed / db_unavailable 都有中英文案，
    提示「去 Glue 后台启用」这类可操作动作）；XHR / 脚本保持抛 AppException，由全局 handler
    输出带 error_key 的 503 JSON。
    """
    try:
        cd_deploy_gate(db)
    except AppException as exc:
        nav = _nav_fail(request, exc.error_key or "errors.service_unavailable")
        if nav is not None:
            return nav
        raise
    return None


@router.get("/api/auth/oidc/start")
def start(request: Request, db: Database = Depends(get_db)):
    """发起 SSO：生成 state/nonce → 302 到 Glue 授权页。

    登录按钮的目标地址（落地页 / 登录页都是整页跳转），因此失败必须以「302 回登录页」收尾。
    """
    nav = _require_oidc_mode(request)
    if nav is not None:
        return nav
    # 必填项缺失先于部署闸门报出：这是配置问题，且要提示缺少的项目。
    nav = _require_oidc_config(request)
    if nav is not None:
        return nav
    if not settings.oidc_ready():
        _raise_not_configured()
    # 部署闸门：与 /api/login 同口径 — CD 未启用 / 表没建全 / 库不可用时，不进入登录流程。
    nav = _gate(request, db)
    if nav is not None:
        return nav

    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    try:
        url = oidc.authorize_url(state, nonce)
    except AppException as exc:
        # discovery 不可达 / issuer 口径不一致等（502）：日志里已有 issuer 双侧取值。
        logger.warning("%s 发起失败：%s", _LABEL, exc.message)
        nav = _nav_fail(request, exc.error_key or "errors.sso_unavailable")
        if nav is not None:
            return nav
        raise
    resp = RedirectResponse(url, status_code=302)
    # state + nonce 都放 HttpOnly cookie（JS 不可读），回调时逐字符比对后立即清除（单次使用）
    resp.set_cookie(
        _STATE_COOKIE,
        f"{state}.{nonce}",
        httponly=True,
        samesite="lax",  # 顶层跳转回跳要带 cookie（strict 会丢），lax 是 OAuth 回跳的常规选择
        path="/",
        secure=settings.cookie_secure,
        max_age=_STATE_TTL,
    )
    return resp


def _json_error(
    request: Request,
    status_code: int,
    detail: str,
    nav_key: str | None = None,
) -> JSONResponse | RedirectResponse:
    """协议 / 上游错误的唯一出口（两种呈现，见模块 docstring）：

    · 浏览器导航 → 302 `/login?error=<key>`：登录页渲染成卡片下方的提示，用户不会看到裸 JSON；
    · XHR / 脚本 → JSON 错误体（形状与全局 AppException handler 一致）+ 状态码语义不变。

    两条路径都会清掉一次性 state cookie（state 严格单次使用）。
    """
    nav = _nav_fail(request, nav_key or _NAV_FALLBACK_KEY.get(status_code, "errors.sso_unavailable"))
    if nav is not None:
        return nav
    resp = JSONResponse(
        status_code=status_code,
        content={"success": False, "error": detail, "detail": {}, "code": status_code},
    )
    resp.delete_cookie(_STATE_COOKIE, path="/")
    return resp


@router.get("/api/auth/oidc/callback")
def callback(
    request: Request,
    db: Database = Depends(get_db),
    code: str = "",
    state: str = "",
    error: str = "",
    retry: str = "",
):
    """Glue 回跳入口：校验 state → 兑 code → 验 id_token → 准入 → 建会话 → 302 /。"""
    nav = _require_oidc_mode(request)
    if nav is not None:
        return nav
    # 必填项缺失时的回调（历史书签 / 手工构造的回跳）：按配置问题提示，不报成上游故障。
    nav = _require_oidc_config(request)
    if nav is not None:
        return nav
    if not settings.oidc_ready():
        _raise_not_configured()
    # 部署闸门必须排在所有凭据处理之前：CD 未部署是部署状态问题，与凭据无关（§7.1）。
    nav = _gate(request, db)
    if nav is not None:
        return nav

    raw = request.cookies.get(_STATE_COOKIE) or ""
    expected_state, _, expected_nonce = raw.partition(".")

    if error:
        # Glue 授权页明确拒绝（如 access_denied / 会话失效）：交互失败，不重试
        logger.warning("%s 授权被拒绝：error=%s", _LABEL, error)
        return _json_error(request, 400, "SSO 授权未完成（Glue 返回 error），请重新发起登录", nav_key="errors.sso_denied")
    if not code or not state:
        return _json_error(request, 400, "OIDC 回调缺少 code / state 参数", nav_key="errors.sso_state_invalid")
    # state 逐字符比对（constant-time）：cookie 缺失 = 未发起 / 已消费；不匹配 = 可能 CSRF
    if not expected_state or not secrets.compare_digest(state.encode(), expected_state.encode()):
        logger.warning("%s state 校验失败（cookie 缺失或已消费），拒绝回调", _LABEL)
        return _json_error(request, 400, "OIDC state 校验失败，请重新发起登录", nav_key="errors.sso_state_invalid")

    try:
        tokens = oidc.exchange_code(code)
        username = oidc.verify_id_token(tokens["id_token"], expected_nonce)
    except oidc.OidcError as exc:
        if exc.invalid_grant and not retry:
            # 授权码已用/已超时（Glue 授权码只活 60s）：静默重走一次 start，用户看到的是继续登录
            logger.info("%s 授权码失效，静默重走授权流程", _LABEL)
            restart = RedirectResponse("/api/auth/oidc/start?retry=1", status_code=302)
            restart.delete_cookie(_STATE_COOKIE, path="/")
            return restart
        logger.warning("%s 校验失败：%s", _LABEL, exc.message)
        # 已重试过一次的 invalid_grant = 这次登录请求确实过期了；其余 502 = Glue 侧不可用/协议不符
        return _json_error(
            request,
            400 if exc.invalid_grant else 502,
            exc.message,
            nav_key="errors.sso_state_invalid" if exc.invalid_grant else "errors.sso_unavailable",
        )

    # ── 准入与身份（§3.1 第 ④⑤⑥ 步）──
    try:
        ident = glue_identity.get_identity(username)
    except AppException as exc:
        if exc.status_code in (401, 403):
            # 身份不可用 / 角色悬空 ⇒ fail-closed，回登录页展示可操作文案（errors.identity_unavailable）
            logger.warning("%s 身份不可用（%s）：%s", _LABEL, username, exc.message)
            return _login_redirect(exc.error_key or "errors.identity_unavailable")
        logger.warning("%s 身份查询失败（%s）：%s", _LABEL, username, exc.message)
        return _json_error(
            request, 502, exc.message, nav_key=exc.error_key or "errors.sso_unavailable"
        )

    if _identity_disabled(ident):
        return _login_redirect("errors.user_disabled")
    if not _has_system(ident.get("systems"), CD_SYSTEM):
        # systems 不含 cd（或为空且 ALLOW_EMPTY_SYSTEMS=false）：与本地登录同一语义与同一文案
        return _login_redirect("errors.no_cd_access")

    with db.conn() as conn:
        token = _create_session(conn, ident["username"])

    logger.info("%s 成功：%s", _LABEL, ident["username"])
    resp = RedirectResponse("/", status_code=302)
    # 会话 cookie 与 /api/login 完全一致（HttpOnly + SameSite=Lax + Path=/，§6.2）。
    # 注意：OIDC 是整页跳转回 SPA，sessionStorage 里的本地登录标志不会保留 ⇒ 前端启动需
    # 自举一次（useAuth.bootstrap → GET /api/me）来还原登录态，见 §7.2 末行。
    resp.set_cookie(
        "cd_token",
        token,
        httponly=True,
        samesite="lax",
        path="/",
        secure=settings.cookie_secure,
        max_age=settings.auth_token_ttl_hours * 3600,
    )
    resp.delete_cookie(_STATE_COOKIE, path="/")
    return resp
