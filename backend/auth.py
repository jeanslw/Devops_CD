"""认证模块 — 与 php_api 共享 admin_users 表"""

import hashlib
import secrets
import time

import bcrypt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from backend.config import settings
from backend.database import Database

security = HTTPBearer(auto_error=False)

CD_SYSTEM = "cd"
_systems_col_ok = True  # 乐观假设 systems 列存在，查询失败后置 False


SESSION_TOKEN_BYTES = 32  # secrets.token_urlsafe(32) ≈ 43 字符，256 位熵


def _hash_token(token: str) -> str:
    """会话 token 只存 SHA-256 摘要：数据库泄露不会暴露可用的明文 token。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _create_session(conn, username: str) -> str:
    """创建登录会话：生成不透明随机 token，库内只存其 SHA-256 摘要。
    返回原始 token（仅此一次交给客户端，之后无法从库内反推）。
    顺带清理该用户已过期的旧会话，避免表无限增长。"""
    token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
    now = int(time.time())
    expires = now + settings.auth_token_ttl_hours * 3600
    conn.execute("DELETE FROM cd_sessions WHERE username=? AND expires_at<=?", (username, now))
    conn.execute(
        "INSERT INTO cd_sessions (token_hash, username, expires_at) VALUES (?, ?, ?)",
        (_hash_token(token), username, expires),
    )
    return token


def _lookup_session(conn, token: str):
    """按 token 摘要查会话，返回 {username, expires_at} 或 None。"""
    return conn.execute(
        "SELECT username, expires_at FROM cd_sessions WHERE token_hash=?",
        (_hash_token(token),),
    ).fetchone()


def _delete_session(conn, token: str) -> None:
    """删除指定会话（logout / 惰性清理过期 token）。"""
    conn.execute("DELETE FROM cd_sessions WHERE token_hash=?", (_hash_token(token),))


def _has_system(systems: str | None, target: str) -> bool:
    """检查 systems 字段是否包含指定系统（逗号分隔，trim 后精确匹配）。
    systems 为 None/空时按 settings.allow_empty_systems 决定：默认放行（兼容旧数据），
    可置 False 改为 deny-by-default（空 systems 即无 CD 权限）。"""
    if not systems:
        return settings.allow_empty_systems
    return target in [s.strip() for s in systems.split(",")]


def _check_cd_access(row) -> None:
    """检查数据库行的 systems 字段是否允许 CD 访问，拒绝则抛 401。
    row 必须包含 'systems' key（由调用方保证列已被读取）。
    不会修改 row 对象。"""
    if not _has_system(row.get("systems"), CD_SYSTEM):
        raise HTTPException(401, "Access denied: CD system not authorized")


def _check_disabled(row) -> None:
    """账号已停用（status=0）则抛 401，让已登录用户的旧 token 即时失效（踢下线）。
    401 会触发前端 useAuth.handle401 → logout，清除本地 token 并回到登录页。
    status 列不存在时默认为 1 放行，兼容旧库。"""
    try:
        status = row["status"]
    except (KeyError, IndexError):
        status = 1
    if status is not None and int(status) == 0:
        raise HTTPException(401, "该账号已被停用，请联系管理员")


def get_db() -> Database:
    """FastAPI 依赖：获取数据库实例"""
    return Database(settings.db_path)


def verify_token(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Database = Depends(get_db),
) -> str:
    """从 Bearer token 验证用户身份，返回 username。
    服务端会话表 + 不透明 token：token 本身不携带任何用户信息，
    校验即查 cd_sessions（O(1) 哈希查找），logout 删行即时失效。
    同时检查 systems 字段（如果存在）是否允许 CD 访问。"""
    if credentials is None:
        raise HTTPException(401, "Please login first")

    token = credentials.credentials
    with db.conn() as conn:
        sess = _lookup_session(conn, token)
        if sess is None:
            raise HTTPException(401, "Invalid or expired token")
        if time.time() > sess["expires_at"]:
            _delete_session(conn, token)
            raise HTTPException(401, "Token expired, please login again")
        row = _query_user_with_systems(conn, sess["username"], "username, systems, status")

    if row is None:
        raise HTTPException(401, "Invalid or expired token")

    _check_cd_access(row)
    _check_disabled(row)

    return sess["username"]


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Database = Depends(get_db),
) -> dict:
    """获取当前登录用户完整信息 {username, role, systems, permissions}。
    同 verify_token：服务端会话校验，同时检查 systems 字段是否允许 CD 访问。"""
    if credentials is None:
        raise HTTPException(401, "Please login first")

    token = credentials.credentials
    with db.conn() as conn:
        sess = _lookup_session(conn, token)
        if sess is None:
            raise HTTPException(401, "Invalid or expired token")
        if time.time() > sess["expires_at"]:
            _delete_session(conn, token)
            raise HTTPException(401, "Token expired, please login again")
        row = _query_user_with_systems(conn, sess["username"], "username, role, systems, status")

    if row is None:
        raise HTTPException(401, "Invalid or expired token")

    _check_cd_access(row)
    _check_disabled(row)

    # 查询该角色的权限列表（无角色 → 空权限，deny-by-default）
    role_name = row.get("role") or ""
    permissions = _query_permissions(db, role_name)

    return {
        "username": row["username"],
        "role": role_name,
        "systems": row.get("systems"),
        "permissions": permissions,
    }


def _query_permissions(db: Database, role_name: str) -> list:
    """通过 roles / role_permissions 表查询指定角色的权限列表。"""
    try:
        with db.conn() as conn:
            rows = conn.execute(
                "SELECT rp.perm_key FROM role_permissions rp JOIN roles r ON r.id = rp.role_id WHERE r.name=?",
                (role_name,),
            ).fetchall()
            return [r["perm_key"] for r in rows]
    except Exception:
        return []  # 表不存在或查询失败时优雅降级


def require_perm(perm_key: str):
    """权限依赖工厂：检查当前用户是否拥有指定权限。
    super_admin 角色隐含所有权限。
    用法: Depends(require_perm("cd.deploy.k8s"))  → 返回 user dict"""

    def checker(user: dict = Depends(get_current_user)):
        if user.get("role") == settings.super_admin_role:
            return user
        if perm_key not in user.get("permissions", []):
            raise HTTPException(403, f"Permission denied: {perm_key} required")
        return user

    return checker


# ── 部署权限映射：deploy_type / cd_type → 需要的 permission key ──
# 顶层 blanket 权限 cd.deploy-manage 隐含所有子权限（super_admin 也隐含所有）
_DEPLOY_PERM_MAP: dict = {
    "ssh": "cd.deploy.single",
    # Docker/Compose 部署：前端（DockerDeployView）与部署器注册名均为 "compose"，
    # 权限键是 cd.deploy.docker。此前误写为 "docker" 键，导致 resolve_deploy_perm("compose")
    # 匹配不上而回退到 cd.deploy.single（少权用户 403 / 单机权限用户反可部署 Docker）。
    "compose": "cd.deploy.docker",
    "docker": "cd.deploy.docker",  # 兼容历史入参（老记录/外部脚本可能仍发 "docker"）
    "k8s/kubectl": "cd.deploy.k8s",
    "k8s/argocd": "cd.deploy.k8s",
    "k8s/fluxcd": "cd.deploy.k8s",
    "k8s/helm": "cd.deploy.k8s",
}
_MANAGE_PERM = "cd.deploy-manage"


def resolve_deploy_perm(deploy_type: str, cd_type: str = "") -> str:
    """将 deploy_type + 可选 cd_type 映射为对应子权限 key。
    匹配不上时回退到 cd.deploy.single。"""
    key = deploy_type or ""
    if cd_type:
        combined = f"{key}/{cd_type}" if key != "k8s" else f"k8s/{cd_type}"
        if combined in _DEPLOY_PERM_MAP:
            return _DEPLOY_PERM_MAP[combined]
    # 纯类型匹配（ssh / docker / k8s）
    if key in _DEPLOY_PERM_MAP:
        return _DEPLOY_PERM_MAP[key]
    return _DEPLOY_PERM_MAP["ssh"]


def enforce_deploy_perm(user: dict, deploy_type: str, cd_type: str = "") -> None:
    """部署执行前的二次权限校验（防御深度：API 层 + Service 层双保险）。
    若用户既无 blanket 级 cd.deploy-manage，也无对应 deploy_type 的子权限则抛 403。
    super_admin 直接放行（由 require_perm 语义保持一致）。"""
    role = user.get("role") or ""
    if role == settings.super_admin_role:
        return
    perms: list = user.get("permissions") or []
    if _MANAGE_PERM in perms:
        return
    required = resolve_deploy_perm(deploy_type, cd_type)
    if required not in perms:
        raise HTTPException(403, f"Permission denied: {_MANAGE_PERM} or {required} required")


def authenticate(user: str, password: str, db: Database) -> str | None:
    """验证用户凭据，同时检查 systems（如果存在）是否允许 CD 访问。
    成功返回不透明会话 token（会话写入 cd_sessions，logout 删行即时吊销）；
    账号已停用抛出 AppException(403) 以区别于密码错误；
    其余失败返回 None（由调用方统一按"账号或密码错误"处理）"""
    from backend.exceptions import AppException

    with db.conn() as conn:
        row = _query_user_with_systems(conn, user, "username, password_hash, systems, status")

        if row is None:
            return None

        # status=0 表示账号已停用。必须先于密码校验判断：无论密码对错都提示「已停用」，
        # 否则停用账号输入错误密码会落到「账号或密码错误」分支，误导用户以为只是密码忘了。
        # (列不存在时默认为 1 放行,兼容旧库)
        try:
            status = row["status"]
        except (KeyError, IndexError):
            status = 1
        if status is not None and int(status) == 0:
            raise AppException("该账号已被停用，请联系管理员", status_code=403, error_key="errors.user_disabled")

        if not bcrypt.checkpw(password.encode(), row["password_hash"].encode()):
            return None

        if not _has_system(row.get("systems"), CD_SYSTEM):
            # 密码正确但 systems 不含 "cd"：明确提示无权限，而非误导为「账号或密码错误」
            raise AppException(
                "该账号无 CD 访问权限，请联系管理员",
                status_code=403,
                error_key="errors.no_cd_access",
            )
        return _create_session(conn, user)


def revoke_session(db: Database, token: str) -> None:
    """吊销会话（logout）：删除服务端会话行，token 立即失效。
    幂等：重复调用 / 无效 token 均不报错。"""
    with db.conn() as conn:
        _delete_session(conn, token)


def load_user_context(db: Database, username: str) -> dict:
    """按用户名加载执行上下文 {username, role, permissions}。

    用于审批单/回滚在后台执行时重建执行身份（无 token 场景）。返回的角色与权限
    与 get_current_user 一致，执行时仍会做部署权限二次校验（防御深度）。
    账号已停用（status=0）抛 AppException(403)：手动执行路径登录即被拦，
    后台/定时执行路径由此处拦截，保证停用账号的已批准定时单不会被自动执行。
    """
    from backend.exceptions import AppException

    with db.conn() as conn:
        row = conn.execute("SELECT role, status FROM admin_users WHERE username=?", (username,)).fetchone()
    # 停用账号检查（与 authenticate / _check_disabled 同口径）：列不存在时默认 1 放行（兼容旧库）
    try:
        status = row["status"] if row else 1
    except (KeyError, IndexError):
        status = 1
    if status is not None and int(status) == 0:
        raise AppException("该账号已被停用，请联系管理员", status_code=403, error_key="errors.user_disabled")
    # 用户不存在（账号被删）或无角色 → 空权限，执行时权限校验会拒绝（deny-by-default），
    # 不再回退到任何默认角色
    role = (row["role"] if row else "") or ""
    permissions = _query_permissions(db, role)
    return {"username": username, "role": role, "permissions": permissions}


def _query_user_with_systems(conn, username: str, columns: str):
    """查询用户行，优先读取 systems 列；列不存在时回退查询并默认放行。
    使用模块级 _systems_col_ok 标志避免重复 SQL 错误。"""
    global _systems_col_ok
    if _systems_col_ok:
        try:
            return conn.execute(
                f"SELECT {columns} FROM admin_users WHERE username=?",  # nosec
                (username,),
            ).fetchone()
        except Exception:
            _systems_col_ok = False
    # 回退：不查 systems/status 列，返回的行不含对应 key → 默认放行（兼容旧表）
    fallback_cols = (
        columns.replace(", systems", "")
        .replace("systems, ", "")
        .replace("systems", "")
        .replace(", status", "")
        .replace("status, ", "")
        .replace("status", "")
    )
    if fallback_cols.strip():
        return conn.execute(
            f"SELECT {fallback_cols} FROM admin_users WHERE username=?",  # nosec
            (username,),
        ).fetchone()
    return None
