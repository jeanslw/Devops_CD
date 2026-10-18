"""认证模块 - 会话校验 + Glue 身份 / 权限"""

import hashlib
import logging
import secrets
import time

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from backend.config import settings
from backend.database import Database, cd_deploy_hint, cd_not_enabled_hint
from backend.services import glue_identity

logger = logging.getLogger(__name__)
security = HTTPBearer(auto_error=False)

CD_SYSTEM = "cd"
# 通配权限:Glue 对 super_admin 返回 ["*"];CD 侧识别该通配,不依赖角色名判断.
WILDCARD_PERM = "*"
# 两种拒绝措辞必须分开:先说"没启用"(去 CI 后台开开关),再说"表没建全"(按 provision 补表)
CD_NOT_ENABLED_MSG = "CD 服务未启用：请在 Devops-Glue 后台「API集成服务」中启用 Devops-CD"
CD_NOT_DEPLOYED_MSG = "CD 服务未部署：CD 数据表未建全"


SESSION_TOKEN_BYTES = 32  # secrets.token_urlsafe(32) ≈ 43 字符,256 位熵


def _hash_token(token: str) -> str:
    """会话 token 只存 SHA-256 摘要."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _create_session(conn, username: str) -> str:
    """创建登录会话:生成随机 token,库内只存其 SHA-256 摘要,返回原始 token.
    同时清理所有过期会话."""
    token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
    now = int(time.time())
    expires = now + settings.auth_token_ttl_hours * 3600
    conn.execute("DELETE FROM cd_sessions WHERE expires_at<=?", (now,))
    conn.execute(
        "INSERT INTO cd_sessions (token_hash, username, expires_at) VALUES (?, ?, ?)",
        (_hash_token(token), username, expires),
    )
    return token


def _lookup_session(conn, token: str):
    """按 token 摘要查会话,返回 {username, expires_at} 或 None."""
    return conn.execute(
        "SELECT username, expires_at FROM cd_sessions WHERE token_hash=?",
        (_hash_token(token),),
    ).fetchone()


def _delete_session(conn, token: str) -> None:
    """删除指定会话(logout / 惰性清理过期 token)."""
    conn.execute("DELETE FROM cd_sessions WHERE token_hash=?", (_hash_token(token),))


def _extract_token(request: Request, credentials: HTTPAuthorizationCredentials | None) -> str | None:
    """提取 token:优先 HttpOnly cookie,回退 Bearer 头."""
    cookie_token = request.cookies.get("cd_token")
    if cookie_token:
        return cookie_token
    if credentials is not None:
        return credentials.credentials
    return None


def _has_system(systems: str | None, target: str) -> bool:
    """检查 systems 字段是否包含指定系统(逗号分隔,trim 后精确匹配).
    systems 为 None/空时按 settings.allow_empty_systems 决定."""
    if not systems:
        return settings.allow_empty_systems
    return target in [s.strip() for s in systems.split(",")]


def get_db() -> Database:
    """FastAPI 依赖:获取数据库实例"""
    return Database(settings.db_path)


def cd_deploy_gate(db: Database) -> None:
    """部署闸门:CD 已启用且表建全才放行.

    1) 未启用 → 503 errors.cd_not_enabled;
    2) 已启用但表缺失 → 503 errors.cd_not_deployed,带缺失表明细;
    3) 契约端点不可达 → 503 errors.ci_contract_unreachable;数据库不可用 → 503 errors.db_unavailable.
    """
    from backend.exceptions import AppException

    state = db.cd_integration_state()
    if not state.enabled:
        raise AppException(
            # 动作细节走 detail.hint,不进入用户提示框
            CD_NOT_ENABLED_MSG,
            status_code=503,
            detail={"hint": cd_not_enabled_hint()},
            error_key="errors.cd_not_enabled",
            error_params={"service": CD_SYSTEM},
        )
    missing = db.cd_missing_tables()
    if not missing:
        return
    preview = "、".join(missing[:6]) + ("…" if len(missing) > 6 else "")
    raise AppException(
        f"{CD_NOT_DEPLOYED_MSG}（缺失 {len(missing)} 张：{preview}）。{cd_deploy_hint(db.driver, state.provision)}",
        status_code=503,
        error_key="errors.cd_not_deployed",
        error_params={"tables": "、".join(missing)},
    )


# ── 会话校验 + 惰性 Glue 复核:会话存 CD 自有表 cd_sessions(不透明 token). ──


def _oidc_session(db: Database, token: str) -> dict:
    """校验服务端会话,返回 {username, expires_at}."""
    with db.conn() as conn:
        sess = _lookup_session(conn, token)
        if sess is None:
            raise HTTPException(401, "Invalid or expired token")
        if time.time() > sess["expires_at"]:
            _delete_session(conn, token)
            raise HTTPException(401, "Token expired, please login again")
    return sess


def _revoke_session_quietly(db: Database, token: str) -> None:
    """删除会话行(踢下线).清理失败仅记录日志,不抛出."""
    try:
        with db.conn() as conn:
            _delete_session(conn, token)
    except Exception:
        logger.warning("revoke session failed", exc_info=True)


def _identity_disabled(ident: dict) -> bool:
    """status=0 即已停用;None / 缺值时按启用处理."""
    status = ident.get("status")
    if status is None:
        return False
    try:
        return int(status) == 0
    except (TypeError, ValueError):
        return False


def _oidc_identity(db: Database, token: str, username: str) -> dict:
    """惰性复核 + 准入(每 OIDC_REVALIDATE_SECONDS 至多 1~2 次 Glue 调用),返回身份 dict:
    {username, role, systems, status, permissions, usable, checked_at, last_ok_at}.

    · status=0 / systems 不含 cd → 删会话行 + 401;
    · 账号被删 / 角色悬空 → 删会话行 + 401 errors.identity_unavailable;
    · Glue 不可达且超出软失败窗口 → 503 errors.service_unavailable;
    · 窗口内命中进程内缓存 → 零 Glue 调用.
    """
    from backend.exceptions import AppException

    try:
        ident = glue_identity.get_identity(username)
    except AppException as exc:
        if exc.status_code in (401, 403):
            _revoke_session_quietly(db, token)
        raise
    # 身份事实 → 准入判断(停用 / systems 不含 cd → 删会话行 + 401)
    if _identity_disabled(ident):
        _revoke_session_quietly(db, token)
        raise AppException("该账号已被停用，请联系管理员", status_code=401, error_key="errors.user_disabled")
    if not _has_system(ident.get("systems"), CD_SYSTEM):
        _revoke_session_quietly(db, token)
        raise AppException("Access denied: CD system not authorized", status_code=401, error_key="errors.no_cd_access")
    return ident


def verify_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: Database = Depends(get_db),
) -> str:
    """从 cookie / Bearer token 验证用户身份,返回 username.
    校验 cd_sessions 会话表,再经 Glue 复核 systems 是否允许 CD 访问."""
    token = _extract_token(request, credentials)
    if not token:
        raise HTTPException(401, "Please login first")

    # 部署闸门先于会话查询:缺表时返回 503 而非 500
    cd_deploy_gate(db)

    sess = _oidc_session(db, token)
    _oidc_identity(db, token, sess["username"])
    return sess["username"]


def verify_inbound_api_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> None:
    """CI→CD 入站静态 token 校验(Authorization: Bearer CD_API_TOKEN).

    使用常量时间比较;CD_API_TOKEN 留空或 token 缺失 / 错误一律 401.
    """
    token = credentials.credentials if credentials is not None else ""
    if not settings.cd_api_token or not token or not secrets.compare_digest(token, settings.cd_api_token):
        raise HTTPException(401, "Invalid or missing API token")


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: Database = Depends(get_db),
) -> dict:
    """获取当前登录用户完整信息 {username, role, systems, permissions}.
    校验流程同 verify_token."""
    token = _extract_token(request, credentials)
    if not token:
        raise HTTPException(401, "Please login first")

    # 部署闸门:与 verify_token 一致
    cd_deploy_gate(db)

    sess = _oidc_session(db, token)
    ident = _oidc_identity(db, token, sess["username"])
    return {
        "username": ident["username"],
        "role": ident.get("role") or "",
        "systems": ident.get("systems"),
        "permissions": ident.get("permissions") or [],
    }


def require_perm(perm_key: str):
    """权限依赖工厂:检查当前用户是否拥有指定权限.
    super_admin 角色隐含所有权限;通配权限 '*'(Glue 对 super_admin 返回)同样放行.
    用法: Depends(require_perm('cd.deploy.k8s'))  → 返回 user dict"""

    def checker(user: dict = Depends(get_current_user)):
        if user.get("role") == settings.super_admin_role:
            return user
        perms = user.get("permissions", [])
        if WILDCARD_PERM in perms or perm_key in perms:
            return user
        raise HTTPException(403, f"Permission denied: {perm_key} required")

    return checker


# ── 部署权限映射:deploy_type / cd_type → 需要的 permission key ──
# 顶层 blanket 权限 cd.deploy-manage 隐含所有子权限(super_admin 也隐含所有)
_DEPLOY_PERM_MAP: dict = {
    "ssh": "cd.deploy.single",
    # 前端与部署器注册名为 "compose",权限键为 cd.deploy.docker
    "compose": "cd.deploy.docker",
    "docker": "cd.deploy.docker",  # 兼容历史入参(老记录/外部脚本可能仍发 "docker")
    "k8s/kubectl": "cd.deploy.k8s",
    "k8s/argocd": "cd.deploy.k8s",
    "k8s/fluxcd": "cd.deploy.k8s",
    "k8s/helm": "cd.deploy.k8s",
}
_MANAGE_PERM = "cd.deploy-manage"


def resolve_deploy_perm(deploy_type: str, cd_type: str = "") -> str:
    """将 deploy_type + 可选 cd_type 映射为对应子权限 key.
    匹配不上时回退到 cd.deploy.single."""
    key = deploy_type or ""
    if cd_type:
        combined = f"{key}/{cd_type}" if key != "k8s" else f"k8s/{cd_type}"
        if combined in _DEPLOY_PERM_MAP:
            return _DEPLOY_PERM_MAP[combined]
    # 纯类型匹配(ssh / docker / k8s)
    if key in _DEPLOY_PERM_MAP:
        return _DEPLOY_PERM_MAP[key]
    return _DEPLOY_PERM_MAP["ssh"]


def enforce_deploy_perm(user: dict, deploy_type: str, cd_type: str = "") -> None:
    """部署执行前的二次权限校验.
    既无 cd.deploy-manage 也无对应 deploy_type 子权限则抛 403.
    super_admin 与通配权限 '*' 直接放行."""
    role = user.get("role") or ""
    if role == settings.super_admin_role:
        return
    perms: list = user.get("permissions") or []
    if WILDCARD_PERM in perms or _MANAGE_PERM in perms:
        return
    required = resolve_deploy_perm(deploy_type, cd_type)
    if required not in perms:
        raise HTTPException(403, f"Permission denied: {_MANAGE_PERM} or {required} required")


def revoke_session(db: Database, token: str) -> None:
    """吊销会话(logout):删除服务端会话行,token 立即失效.
    幂等:重复调用 / 无效 token 均不报错."""
    with db.conn() as conn:
        _delete_session(conn, token)


def load_user_context(db: Database, username: str) -> dict:
    """按用户名加载执行上下文 {username, role, permissions}.

    用于审批单 / 回滚后台执行时重建执行身份(无 token 场景).账号已停用(status=0)或
    systems 不含 cd 时抛 AppException(403);身份 / 权限来自 Glue.
    """
    from backend.exceptions import AppException

    ident = glue_identity.get_identity(username)
    if _identity_disabled(ident):
        raise AppException("该账号已被停用，请联系管理员", status_code=403, error_key="errors.user_disabled")
    # 与 _oidc_identity 同判据;本路径不删会话,用 403
    if not _has_system(ident.get("systems"), CD_SYSTEM):
        raise AppException(
            "该账号无 CD 访问权限，请联系管理员",
            status_code=403,
            error_key="errors.no_cd_access",
        )
    return {
        "username": ident.get("username") or username,
        "role": ident.get("role") or "",
        "permissions": ident.get("permissions") or [],
    }
