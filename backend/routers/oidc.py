"""OIDC 登录路由(BFF 机密客户端)

   GET /api/auth/oidc/start     → 302 到 Glue /oauth/authorize(state/nonce 落单次 cookie)
   GET /api/auth/oidc/callback  → 校验 state → 兑 code → 验 id_token → 准入 → 建会话 → 302 /

失败状态码分四类:
  · 交互 / 协议不合法(缺 code,state 缺失或不匹配,Glue 拒绝授权)→ 400;
  · Glue 不可用 / 协议不符(discovery,JWKS,token 端点 5xx,错 secret)→ 502;
  · token 端点返回 invalid_grant → 静默重走一次 start,带 retry=1 防无限跳转;
  · 业务性失败(账号停用 / 无 CD 权限 / 身份或角色悬空 / 必填项缺失)→ 302 /login?error=<error_key>.

呈现方式按 Accept 分流:浏览器导航(`text/html`)302 回登录页;XHR / 脚本返回 JSON 错误体.
"""

import logging
import secrets
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
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
_STATE_TTL = 600
_LABEL = "OIDC 登录"


def _login_redirect(error_key: str, missing: list[str] | None = None, params: dict | None = None) -> RedirectResponse:
    """业务性失败:302 回登录页并带上 i18n key,同时清除一次性 state cookie.

    missing 补入 errors.oidc_not_configured 的 {items};params 为其余 i18n 插值参数.
    """
    query = f"error={error_key}"
    if missing:
        query += f"&missing={quote(','.join(missing), safe='')}"
    for k, v in (params or {}).items():
        query += f"&{quote(str(k), safe='')}={quote(str(v), safe='')}"
    resp = RedirectResponse(f"/login?{query}", status_code=302)
    resp.delete_cookie(_STATE_COOKIE, path="/")
    return resp


_NAV_FALLBACK_KEY = {400: "errors.sso_state_invalid", 502: "errors.sso_unavailable"}


def _is_browser_navigation(request: Request) -> bool:
    """是否为浏览器顶层导航(点登录按钮 / 跟随 302 / 地址栏回车)。按 `Accept: text/html` 判定."""
    headers = getattr(request, "headers", None)
    accept = (headers.get("accept") if headers else "") or ""
    return "text/html" in accept


def _require_oidc_config(request: Request) -> RedirectResponse | None:
    """OIDC 必填项是否齐备:缺失时提示要配的项目(配置问题,不是上游故障).

    导航下 302 回登录页并带上缺失清单(登录页渲染 errors.oidc_not_configured 的 {items});
    XHR / 脚本返回 None,由调用方抛 AppException(503 + error_key + error_params).
    """
    missing = settings.oidc_missing_settings()
    if not missing:
        return None
    logger.error("%s 缺少必填配置：%s", _LABEL, ", ".join(missing))
    if _is_browser_navigation(request):
        return _login_redirect("errors.oidc_not_configured", missing)
    return None


def _raise_not_configured() -> None:
    """必填项缺失(XHR / 脚本路径):503 + error_key,与导航路径同一份缺失清单."""
    missing = settings.oidc_missing_settings()
    raise AppException(
        f"OIDC 未配置完成，缺少 {' / '.join(missing)}",
        503,
        error_key="errors.oidc_not_configured",
        error_params={"items": ", ".join(missing)},
    )


def _nav_fail(request: Request, error_key: str, params: dict | None = None) -> RedirectResponse | None:
    """浏览器导航下的失败统一收口:302 回登录页 + i18n key(登录页在卡片下方提示).

    非导航返回 None:调用方按各自原语义继续(抛 AppException / 返回 JSON).
    """
    if _is_browser_navigation(request):
        logger.info("%s 失败 → 回登录页提示 %s", _LABEL, error_key)
        return _login_redirect(error_key, params=params)
    return None


def _gate(request: Request, db: Database) -> RedirectResponse | None:
    """部署闸门:CD 未启用 / 未部署 / 契约不可达 / 库不可用时,不进入登录流程.

    导航下 302 回登录页;XHR / 脚本抛 AppException(503).
    """
    try:
        cd_deploy_gate(db)
    except AppException as exc:
        nav = _nav_fail(request, exc.error_key or "errors.service_unavailable", exc.error_params)
        if nav is not None:
            return nav
        raise
    return None


@router.get("/api/auth/oidc/start")
def start(request: Request, db: Database = Depends(get_db)):
    """发起 SSO:生成 state/nonce → 302 到 Glue 授权页.失败一律 302 回登录页."""
    nav = _require_oidc_config(request)
    if nav is not None:
        return nav
    if not settings.oidc_ready():
        _raise_not_configured()
    nav = _gate(request, db)
    if nav is not None:
        return nav

    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    try:
        url = oidc.authorize_url(state, nonce)
    except AppException as exc:
        logger.warning("%s 发起失败：%s", _LABEL, exc.message)
        nav = _nav_fail(request, exc.error_key or "errors.sso_unavailable")
        if nav is not None:
            return nav
        raise
    resp = RedirectResponse(url, status_code=302)
    # state + nonce 都放 HttpOnly cookie(JS 不可读),回调时逐字符比对后立即清除(单次使用)
    resp.set_cookie(
        _STATE_COOKIE,
        f"{state}.{nonce}",
        httponly=True,
        samesite="lax",
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
    """协议 / 上游错误的唯一出口(两种呈现,见模块 docstring):

    · 浏览器导航 → 302 `/login?error=<key>`:登录页渲染成卡片下方的提示,用户不会看到裸 JSON;
    · XHR / 脚本 → JSON 错误体(形状与全局 AppException handler 一致)+ 状态码语义不变.

    两条路径都会清掉一次性 state cookie(state 严格单次使用).
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
    """Glue 回跳入口:校验 state → 兑 code → 验 id_token → 准入 → 建会话 → 302 /."""
    nav = _require_oidc_config(request)
    if nav is not None:
        return nav
    if not settings.oidc_ready():
        _raise_not_configured()
    nav = _gate(request, db)
    if nav is not None:
        return nav

    raw = request.cookies.get(_STATE_COOKIE) or ""
    expected_state, _, expected_nonce = raw.partition(".")

    if error:
        logger.warning("%s 授权被拒绝：error=%s", _LABEL, error)
        return _json_error(
            request, 400, "SSO 授权未完成（Glue 返回 error），请重新发起登录", nav_key="errors.sso_denied"
        )
    if not code or not state:
        return _json_error(request, 400, "OIDC 回调缺少 code / state 参数", nav_key="errors.sso_state_invalid")
    if not expected_state or not secrets.compare_digest(state.encode(), expected_state.encode()):
        logger.warning("%s state 校验失败（cookie 缺失或已消费），拒绝回调", _LABEL)
        return _json_error(request, 400, "OIDC state 校验失败，请重新发起登录", nav_key="errors.sso_state_invalid")

    try:
        tokens = oidc.exchange_code(code)
        username = oidc.verify_id_token(tokens["id_token"], expected_nonce)
    except oidc.OidcError as exc:
        if exc.invalid_grant and not retry:
            logger.info("%s 授权码失效，静默重走授权流程", _LABEL)
            restart = RedirectResponse("/api/auth/oidc/start?retry=1", status_code=302)
            restart.delete_cookie(_STATE_COOKIE, path="/")
            return restart
        logger.warning("%s 校验失败：%s", _LABEL, exc.message)
        return _json_error(
            request,
            400 if exc.invalid_grant else 502,
            exc.message,
            nav_key="errors.sso_state_invalid" if exc.invalid_grant else "errors.sso_unavailable",
        )

    try:
        ident = glue_identity.get_identity(username)
    except AppException as exc:
        if exc.status_code in (401, 403):
            logger.warning("%s 身份不可用（%s）：%s", _LABEL, username, exc.message)
            return _login_redirect(exc.error_key or "errors.identity_unavailable")
        logger.warning("%s 身份查询失败（%s）：%s", _LABEL, username, exc.message)
        return _json_error(request, 502, exc.message, nav_key=exc.error_key or "errors.sso_unavailable")

    if _identity_disabled(ident):
        return _login_redirect("errors.user_disabled")
    if not _has_system(ident.get("systems"), CD_SYSTEM):
        return _login_redirect("errors.no_cd_access")

    with db.conn() as conn:
        token = _create_session(conn, ident["username"])

    logger.info("%s 成功：%s", _LABEL, ident["username"])
    resp = RedirectResponse("/", status_code=302)
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
