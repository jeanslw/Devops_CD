"""Glue 身份与权限客户端 + 进程内 TTL 缓存 + 惰性复核

OIDC 模式下 CD 在请求路径上不读任何共享表:
  · 身份(role / systems / status)来自 GET /api/rbac/users/{username}
  · 权限(perm_key)来自 GET /api/rbac/roles/{role}/permissions
其余判据都来自自有 cd_* 表.缓存只存进程内(单进程 uvicorn:不写数据库,零 DDL).

三条不可动摇的语义:
  1. 角色权限 404 ≠ 空权限:查不到角色是'身份不可用',fail-closed(拒绝),
     不降级为空权限 -- 角色名写错/角色被删会成为一次静默降权(用户能登录,
     界面正常,操作全 403,且没有任何诊断信号).
  2. 软失败窗口(OIDC_STALE_GRACE_SECONDS):Glue 短暂不可达时用 last-known-good 放行,
     超过窗口即 fail-closed;'身份已消失 / 角色悬空'这类明确否定立即生效,不进软失败.
  3. role 原样透传(不 lower):Glue 侧 roleExists 精确匹配 roles.name,只 trim;
     只有 username 需要 strip().lower().
"""

import logging
import threading
import time
from typing import Any
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from backend.config import settings
from backend.exceptions import AppException

logger = logging.getLogger("glue_identity")

_USERS_PATH = "/api/rbac/users/{username}"
_ROLE_PERMS_PATH = "/api/rbac/roles/{role}/permissions"
_HTTP_TIMEOUT = 10
_WARN_INTERVAL = 60  # 同一 key 的 WARN 最短间隔(秒):降权瞬间不刷日志

_USERNAME_MAX_LEN = 128  # 与 Glue 侧 users.username 列宽同量级,超长即视为异常输入


class GlueIdentityError(AppException):
    """Glue 身份/权限不可用.

    error_key 直接下发到前端(回登录页时按 errors.* 翻译):
      · errors.identity_unavailable -- 账号已消失 / 角色悬空(明确否定,fail-closed)
      · errors.service_unavailable   -- Glue 不可达且超出软失败窗口
    """


class _GlueUnavailable(Exception):
    """内部信号:Glue 不可达 / 返回非 2xx 的**暂时性**错误(可进软失败窗口).

    与 GlueIdentityError 的区别是'暂时 vs 明确':网络抖动与 5xx 属于前者,
    账号 404 / 角色 404 属于后者 -- 后者永远不软失败.
    """

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


# ── 进程内状态(键为 username / role)──

_lock = threading.RLock()
# username → {username, role, systems, status, permissions, usable, checked_at, last_ok_at}
_identity_cache: dict[str, dict] = {}
# role → {permissions, checked_at}:角色的权限清单(窗口 = OIDC_REVALIDATE_SECONDS).
# 按**角色**缓存而非按用户:同一角色每窗口只查一次,N 个同角色用户共摊 1 次调用.
_perm_cache: dict[str, dict] = {}
# role → 负缓存过期时间戳:角色 404 在复核窗口内只查一次(fail-closed 但不高频打 Glue)
_role_negative: dict[str, float] = {}
# 限流日志:key → 上次 WARN 时间戳
_last_warn: dict[str, float] = {}

# GET 是幂等的,可带重试(瞬时 5xx / 连接抖动不该直接判用户登录失败)
_session = requests.Session()
_retry = Retry(total=2, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
_session.mount("http://", HTTPAdapter(max_retries=_retry))
_session.mount("https://", HTTPAdapter(max_retries=_retry))


def _warn_throttled(key: str, message: str, *args: Any) -> None:
    """按 key 限流地打 WARN:同一问题在一个窗口内只留一条日志."""
    with _lock:
        now = time.time()
        if now - _last_warn.get(key, 0.0) < _WARN_INTERVAL:
            return
        _last_warn[key] = now
    logger.warning(message, *args)


def _glue_get(path: str) -> tuple[int, Any]:
    """GET Glue(Bearer dg_ 服务 token).

    硬约束:**不加 ?format=json**(加了响应会被包成 {"data":...}),
    **不解析 message** 判错(message 是已翻译文本,不是稳定 key) -- 调用方只看状态码.
    """
    base = (settings.ci_api_url or "").rstrip("/")
    if not base:
        # OIDC 模式下 CI_API_URL / CI_API_TOKEN 是身份来源,缺了就是"身份不可用"
        raise _GlueUnavailable("CI_API_URL 未配置：oidc 模式需要 Glue 地址作为身份来源")
    if not settings.ci_api_token:
        raise _GlueUnavailable("CI_API_TOKEN 未配置：oidc 模式下所有 Glue 调用都用 dg_ 服务 token（§3.1）")
    try:
        resp = _session.get(
            f"{base}{path}",
            headers={"Authorization": f"Bearer {settings.ci_api_token}"},
            timeout=_HTTP_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise _GlueUnavailable(f"Glue 不可达：{exc}") from exc
    if resp.status_code != 200:
        return resp.status_code, None
    try:
        return 200, resp.json()
    except ValueError as exc:
        raise _GlueUnavailable("Glue 响应不是合法 JSON") from exc


# ── 角色权限 ──


def role_permissions(role: str) -> list[str]:
    """取角色的权限清单(按角色缓存,窗口 = OIDC_REVALIDATE_SECONDS).

    · 404 即 fail-closed(GlueIdentityError):角色名写错 / 角色行被删,不是'没有权限';
      同时写负缓存(窗口内不再打 Glue)并打一条含角色名的限流 WARN,便于定位'角色悬空';
      `super_admin` 由 Glue 返回 ["*"],CD 侧按通配放行.
    · 200 但 permissions 为空数组 即 真实事实(角色存在但零权限)即 正常返回 [],由调用方按 deny 处理.
    · 非 200/404 即 暂时性失败(_GlueUnavailable),交给软失败窗口.
    """
    role = (role or "").strip()  # 只 trim,不 lower(lower 会导致 404)
    if not role:
        return []  # 无角色:deny-by-default(与 local 模式"无角色 = 空权限"同口径)

    now = time.time()
    window = max(settings.oidc_revalidate_seconds, 1)
    with _lock:
        cached = _perm_cache.get(role)
        if cached and now - cached["checked_at"] < window:
            return list(cached["permissions"])
        if _role_negative.get(role, 0.0) > now:
            raise GlueIdentityError(
                f"角色 {role!r} 不存在（404 负缓存中，窗口 {window}s）",
                status_code=401,
                error_key="errors.identity_unavailable",
            )

    code, body = _glue_get(_ROLE_PERMS_PATH.format(role=quote(role, safe="")))
    if code == 404:
        with _lock:
            _role_negative[role] = time.time() + window
        _warn_throttled(
            f"role404:{role}",
            "Glue 角色权限端返回 404：角色 %r 在 roles 表中不存在（角色悬空），按 fail-closed 拒绝；"
            "角色行恢复后 ≤ %ss 自动恢复",
            role,
            window,
        )
        raise GlueIdentityError(
            f"角色 {role!r} 不存在：身份不可用（请联系管理员核对 Glue 角色配置）",
            status_code=401,
            error_key="errors.identity_unavailable",
        )
    if code != 200:
        raise _GlueUnavailable(f"角色权限端返回 HTTP {code}")
    perms = body.get("permissions") if isinstance(body, dict) else None
    if not isinstance(perms, list):
        raise _GlueUnavailable("角色权限端响应结构异常（缺 permissions 数组）")
    resolved = [str(p) for p in perms]
    with _lock:
        _perm_cache[role] = {"permissions": resolved, "checked_at": time.time()}
        _role_negative.pop(role, None)  # 角色已恢复:立刻解除负缓存
    return list(resolved)


# ── 身份 ──


def _fetch_identity(username: str) -> dict:
    """GET /api/rbac/users/{username} → {username, role, systems, status}.

    · username 先 strip().lower()(Glue 侧 strtolower 归一);
    · role 原样透传(不 lower);
    · 404 即 身份已消失(明确否定,与角色 404 同等对待).
    """
    code, body = _glue_get(_USERS_PATH.format(username=quote(username, safe="")))
    if code == 404:
        raise GlueIdentityError(
            f"账号 {username!r} 在 Glue 中不存在（可能已被删除）",
            status_code=401,
            error_key="errors.identity_unavailable",
        )
    if code != 200:
        raise _GlueUnavailable(f"用户端返回 HTTP {code}")
    if not isinstance(body, dict):
        raise _GlueUnavailable("用户端响应结构异常（应为裸对象，不带 data 包裹）")
    return {
        "username": str(body.get("username") or username).strip().lower(),
        "role": body.get("role") or "",
        "systems": body.get("systems"),
        "status": body.get("status"),
    }


def _soft_fail_or_raise(key: str, cached: dict | None, exc: _GlueUnavailable) -> dict:
    """Glue 不可达时的统一策略:软失败窗口内用 last-known-good 放行,否则 fail-closed.

    窗口以 last_ok_at 计(= 上次**成功**复核的时间),所以窗口不会被失败请求无限续期;
    OIDC_STALE_GRACE_SECONDS=0 表示立即 fail-closed.
    """
    grace = settings.oidc_stale_grace_seconds
    if cached and cached["usable"] and grace > 0 and time.time() - cached["last_ok_at"] <= grace:
        _warn_throttled(
            f"softfail:{key}",
            "Glue 不可达：账号 %r 在软失败窗口（%ss）内沿用 last-known-good 身份（%s）",
            key,
            grace,
            exc.message,
        )
        return dict(cached)
    raise GlueIdentityError(
        f"Glue 不可达且超出软失败窗口，拒绝访问：{exc.message}",
        status_code=503,
        error_key="errors.service_unavailable",
    ) from exc


def get_identity(username: str, *, force: bool = False) -> dict:
    """取账号身份 + 权限(带惰性复核缓存).返回:

        {username, role, systems, status, permissions, usable, checked_at, last_ok_at}

    · 窗口内命中(< OIDC_REVALIDATE_SECONDS)即 零 Glue 调用,直接放行;
    · 超窗口 即 复核 /api/rbac/users/{username} + 刷新该角色权限:
      身份 1 次调用/账号/窗口,权限按角色缓存,N 个同角色用户每窗口共摊 1 次;
    · 复核发现 status=0 / systems 不含 cd 即 由调用方判拒并删会话行;
      本函数只负责'身份事实',不做准入判断.
    """
    key = (username or "").strip().lower()
    if not key or len(key) > _USERNAME_MAX_LEN:
        raise GlueIdentityError("用户名非法", status_code=401, error_key="errors.identity_unavailable")

    now = time.time()
    window = max(settings.oidc_revalidate_seconds, 1)
    with _lock:
        cached = dict(_identity_cache[key]) if key in _identity_cache else None

    if cached is not None and not force and now - cached["checked_at"] < window:
        if cached["usable"]:
            return cached
        # 窗口内已知该账号身份不可用(角色悬空):零 Glue 调用继续 fail-closed,
        # "降权/悬空瞬间"不会把 Glue 打满
        raise GlueIdentityError(
            f"账号 {key!r} 的身份当前不可用（角色悬空，详见 WARN 日志）",
            status_code=401,
            error_key="errors.identity_unavailable",
        )

    try:
        ident = _fetch_identity(key)
    except GlueIdentityError:
        invalidate(key)  # 明确否定:立即丢弃缓存,不保留 last-known-good
        raise
    except _GlueUnavailable as exc:
        return _soft_fail_or_raise(key, cached, exc)

    # 每个复核窗口都刷新一次该角色的权限:只看"角色名是否变化"不够 --
    # 同角色被去掉某个 perm_key 或角色行被删都必须在窗口内生效.
    # 权限按角色缓存,同一角色在同一窗口内只查一次(N 个用户共享 1 次调用).
    try:
        permissions = role_permissions(ident["role"])
    except GlueIdentityError:
        # 角色悬空:写一份 usable=False 的负缓存(窗口内不再打 Glue),但每次都 fail-closed;
        # 角色行恢复后 ≤1 个窗口自动恢复
        with _lock:
            _identity_cache[key] = {
                **ident,
                "permissions": None,
                "usable": False,
                "checked_at": now,
                "last_ok_at": (cached or {}).get("last_ok_at", 0.0),
            }
        raise
    except _GlueUnavailable as exc:
        return _soft_fail_or_raise(key, cached, exc)

    now = time.time()
    entry = {**ident, "permissions": permissions, "usable": True, "checked_at": now, "last_ok_at": now}
    with _lock:
        _identity_cache[key] = entry
    return dict(entry)


def invalidate(username: str) -> None:
    """丢弃某账号的身份缓存(账号被删 / 需要立即重查时用)."""
    with _lock:
        _identity_cache.pop((username or "").strip().lower(), None)


def reset_cache() -> None:
    """清空全部进程内缓存(登出批量失效 / 测试用)."""
    with _lock:
        _identity_cache.clear()
        _perm_cache.clear()
        _role_negative.clear()
        _last_warn.clear()
