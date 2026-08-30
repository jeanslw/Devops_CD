"""认证模块 — 与 php_api 共享 admin_users 表"""

import base64

import bcrypt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from backend.config import settings
from backend.database import Database

security = HTTPBearer(auto_error=False)

CD_SYSTEM = "cd"
_systems_col_ok = True  # 乐观假设 systems 列存在，查询失败后置 False


def _has_system(systems: str | None, target: str) -> bool:
    """检查 systems 字段是否包含指定系统（逗号分隔，trim 后精确匹配）。
    systems 为 None/空时默认放行（兼容旧数据）。"""
    if not systems:
        return True  # 字段为空 → 兼容老数据，默认有权限
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
    """从 Bearer token 中验证用户身份，返回 username（O(1) 直接查询）。
    同时检查 systems 字段（如果存在）是否允许 CD 访问。"""
    if credentials is None:
        raise HTTPException(401, "Please login first")

    token = credentials.credentials
    try:
        decoded = base64.b64decode(token).decode()
        username, _, _hash = decoded.partition(":")
    except Exception as e:
        raise HTTPException(401, "Invalid token format") from e

    with db.conn() as conn:
        row = _query_user_with_systems(conn, username, "password_hash, systems, status")

    if row is None:
        raise HTTPException(401, "Invalid or expired token")

    _check_cd_access(row)
    _check_disabled(row)

    # 完整校验：重组 token 并比对（防止 path traversal 类攻击）
    expected = base64.b64encode(f"{username}:{row['password_hash']}".encode()).decode()
    if not _timing_safe_compare(token, expected):
        raise HTTPException(401, "Invalid or expired token")

    return username


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Database = Depends(get_db),
) -> dict:
    """获取当前登录用户完整信息 {username, role, systems, permissions}。
    同时检查 systems 字段（如果存在）是否允许 CD 访问。"""
    if credentials is None:
        raise HTTPException(401, "Please login first")

    token = credentials.credentials
    try:
        decoded = base64.b64decode(token).decode()
        username, _, _hash = decoded.partition(":")
    except Exception as e:
        raise HTTPException(401, "Invalid token format") from e

    with db.conn() as conn:
        row = _query_user_with_systems(conn, username, "username, password_hash, role, systems, status")

    if row is None:
        raise HTTPException(401, "Invalid or expired token")

    _check_cd_access(row)
    _check_disabled(row)

    expected = base64.b64encode(f"{username}:{row['password_hash']}".encode()).decode()
    if not _timing_safe_compare(token, expected):
        raise HTTPException(401, "Invalid or expired token")

    # 查询该角色的权限列表
    role_name = row.get("role", settings.admin_role)
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
        if user.get("role") == "super_admin":
            return user
        if perm_key not in user.get("permissions", []):
            raise HTTPException(403, f"Permission denied: {perm_key} required")
        return user

    return checker


def require_admin_role(user: dict = Depends(get_current_user)):
    """角色级权限检查：允许 super_admin 和 admin 角色访问。
    用于用户管理等按角色而非权限 key 控制的功能。
    cd.admin 不在 CI permissions 表中，无法用 require_perm 检查。"""
    if user.get("role") not in (settings.super_admin_role, settings.admin_role):
        raise HTTPException(403, "Permission denied: admin or super_admin role required")
    return user


# ── 部署权限映射：deploy_type / cd_type → 需要的 permission key ──
# 顶层 blanket 权限 cd.deploy-manage 隐含所有子权限（super_admin 也隐含所有）
_DEPLOY_PERM_MAP: dict = {
    "ssh": "cd.deploy.single",
    "docker": "cd.deploy.docker",
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
    成功返回 token；账号已停用抛出 AppException(403) 以区别于密码错误；
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

    if bcrypt.checkpw(password.encode(), row["password_hash"].encode()):
        if not _has_system(row.get("systems"), CD_SYSTEM):
            return None  # 无权登录 CD
        return base64.b64encode(f"{user}:{row['password_hash']}".encode()).decode()
    return None


def _timing_safe_compare(a: str, b: str) -> bool:
    """常量时间字符串比较，防止时序攻击"""
    if len(a) != len(b):
        return False
    result = 0
    for x, y in zip(a, b, strict=True):
        result |= ord(x) ^ ord(y)
    return result == 0


def load_user_context(db: Database, username: str) -> dict:
    """按用户名加载执行上下文 {username, role, permissions}。

    用于审批单/回滚在后台执行时重建执行身份（无 token 场景）。返回的角色与权限
    与 get_current_user 一致，执行时仍会做部署权限二次校验（防御深度）。
    """
    with db.conn() as conn:
        row = conn.execute("SELECT role FROM admin_users WHERE username=?", (username,)).fetchone()
    role = (row["role"] if row else "") or settings.admin_role
    permissions = _query_permissions(db, role)
    return {"username": username, "role": role, "permissions": permissions}


def _query_user_with_systems(conn, username: str, columns: str):
    """查询用户行，优先读取 systems 列；列不存在时回退查询并默认放行。
    使用模块级 _systems_col_ok 标志避免重复 SQL 错误。"""
    global _systems_col_ok
    if _systems_col_ok:
        try:
            return conn.execute(
                f"SELECT {columns} FROM admin_users WHERE username=?",
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
            f"SELECT {fallback_cols} FROM admin_users WHERE username=?",
            (username,),
        ).fetchone()
    return None
