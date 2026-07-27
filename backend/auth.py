"""认证模块 — 与 php_api 共享 admin_users 表"""

import base64
import bcrypt
from fastapi import HTTPException, Depends, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from backend.database import Database
from backend.config import settings

security = HTTPBearer(auto_error=False)


def get_db() -> Database:
    """FastAPI 依赖：获取数据库实例"""
    return Database(settings.db_path)


def verify_token(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Database = Depends(get_db),
) -> str:
    """从 Bearer token 中验证用户身份，返回 username（O(1) 直接查询）"""
    if credentials is None:
        raise HTTPException(401, "Please login first")

    token = credentials.credentials
    try:
        decoded = base64.b64decode(token).decode()
        username, _, _hash = decoded.partition(":")
    except Exception:
        raise HTTPException(401, "Invalid token format")

    with db.conn() as conn:
        row = conn.execute(
            "SELECT password_hash FROM admin_users WHERE username=?", (username,)
        ).fetchone()

    if row is None:
        raise HTTPException(401, "Invalid or expired token")

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
    """获取当前登录用户完整信息 {username, role}"""
    if credentials is None:
        raise HTTPException(401, "Please login first")

    token = credentials.credentials
    try:
        decoded = base64.b64decode(token).decode()
        username, _, _hash = decoded.partition(":")
    except Exception:
        raise HTTPException(401, "Invalid token format")

    with db.conn() as conn:
        row = conn.execute(
            "SELECT username, password_hash, role FROM admin_users WHERE username=?",
            (username,),
        ).fetchone()

    if row is None:
        raise HTTPException(401, "Invalid or expired token")

    expected = base64.b64encode(
        f"{username}:{row['password_hash']}".encode()
    ).decode()
    if not _timing_safe_compare(token, expected):
        raise HTTPException(401, "Invalid or expired token")

    return {"username": row["username"], "role": row.get("role", "admin")}


def require_admin(user: dict = Depends(get_current_user)) -> dict:
    """管理员权限依赖：非 admin 返回 403"""
    if user.get("role") != "admin":
        raise HTTPException(403, "Permission denied: admin required")
    return user


def require_deployer(user: dict = Depends(get_current_user)) -> str:
    """部署者权限依赖：admin 或 deployer 可执行部署操作，返回 username"""
    role = user.get("role", "")
    if role not in ("admin", "deployer"):
        raise HTTPException(403, "Permission denied: deployer or admin required")
    return user["username"]


def authenticate(user: str, password: str, db: Database) -> str | None:
    """验证用户凭据，成功返回 token，失败返回 None"""
    with db.conn() as conn:
        row = conn.execute(
            "SELECT username, password_hash FROM admin_users WHERE username=?", (user,)
        ).fetchone()

    if row and bcrypt.checkpw(password.encode(), row["password_hash"].encode()):
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


def ensure_role_column(db: Database):
    """确保 admin_users 表有 role 列（兼容旧数据库迁移）"""
    try:
        with db.conn() as conn:
            try:
                conn.execute("ALTER TABLE admin_users ADD COLUMN role VARCHAR(32) DEFAULT 'admin'")
                conn.commit()
            except Exception:
                pass  # 列已存在
    except Exception:
        pass
