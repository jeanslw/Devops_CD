"""认证模块 — 与 php_api 共享 admin_users 表"""

import base64
import bcrypt
from fastapi import HTTPException, Depends, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from backend.database import Database
from backend.config import settings

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
    except Exception:
        raise HTTPException(401, "Invalid token format")

    with db.conn() as conn:
        row = _query_user_with_systems(conn, username, "password_hash, systems")

    if row is None:
        raise HTTPException(401, "Invalid or expired token")

    _check_cd_access(row)

    # 完整校验：重组 token 并比对（防止 path traversal 类攻击）
    expected = base64.b64encode(
        f"{username}:{row['password_hash']}".encode()
    ).decode()
    if not _timing_safe_compare(token, expected):
        raise HTTPException(401, "Invalid or expired token")

    return username


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Database = Depends(get_db),
) -> dict:
    """获取当前登录用户完整信息 {username, role, systems}。
    同时检查 systems 字段（如果存在）是否允许 CD 访问。"""
    if credentials is None:
        raise HTTPException(401, "Please login first")

    token = credentials.credentials
    try:
        decoded = base64.b64decode(token).decode()
        username, _, _hash = decoded.partition(":")
    except Exception:
        raise HTTPException(401, "Invalid token format")

    with db.conn() as conn:
        row = _query_user_with_systems(conn, username, "username, password_hash, role, systems")

    if row is None:
        raise HTTPException(401, "Invalid or expired token")

    _check_cd_access(row)

    expected = base64.b64encode(
        f"{username}:{row['password_hash']}".encode()
    ).decode()
    if not _timing_safe_compare(token, expected):
        raise HTTPException(401, "Invalid or expired token")

    return {
        "username": row["username"],
        "role": row.get("role", settings.admin_role),
        "systems": row.get("systems"),
    }


def require_admin(user: dict = Depends(get_current_user)) -> dict:
    """管理员权限依赖：admin 或 super_admin 可访问"""
    if user.get("role") not in (settings.admin_role, settings.super_admin_role):
        raise HTTPException(403, "Permission denied: admin required")
    return user


def require_super_admin(user: dict = Depends(get_current_user)) -> dict:
    """超管权限依赖：仅 super_admin 可操作管理员账号"""
    if user.get("role") != settings.super_admin_role:
        raise HTTPException(403, "Permission denied: super_admin required")
    return user


def require_deployer(user: dict = Depends(get_current_user)) -> str:
    """部署者权限依赖：admin / super_admin / deployer 可执行部署操作，返回 username"""
    role = user.get("role", "")
    if role not in (settings.admin_role, settings.super_admin_role, settings.deployer_role):
        raise HTTPException(403, "Permission denied: deployer or admin required")
    return user["username"]


def authenticate(user: str, password: str, db: Database) -> str | None:
    """验证用户凭据，同时检查 systems（如果存在）是否允许 CD 访问。
    成功返回 token，失败返回 None"""
    with db.conn() as conn:
        row = _query_user_with_systems(conn, user, "username, password_hash, systems")

    if row and bcrypt.checkpw(password.encode(), row["password_hash"].encode()):
        if not _has_system(row.get("systems"), CD_SYSTEM):
            return None  # 无权登录 CD
        return base64.b64encode(
            f"{user}:{row['password_hash']}".encode()
        ).decode()
    return None


def _timing_safe_compare(a: str, b: str) -> bool:
    """常量时间字符串比较，防止时序攻击"""
    if len(a) != len(b):
        return False
    result = 0
    for x, y in zip(a, b):
        result |= ord(x) ^ ord(y)
    return result == 0


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
    # 回退：不查 systems 列，返回的行不含 systems key → _has_system(None, ...) = True（放行）
    fallback_cols = columns.replace(", systems", "").replace("systems, ", "").replace("systems", "")
    if fallback_cols.strip():
        return conn.execute(
            f"SELECT {fallback_cols} FROM admin_users WHERE username=?",
            (username,),
        ).fetchone()
    return None
