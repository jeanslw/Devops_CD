"""认证模块 — 与 php_api 共享 admin_users 表"""

import base64
import bcrypt
from fastapi import HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from app.database import Database
from app.config import settings

security = HTTPBearer(auto_error=False)


def get_db() -> Database:
    """FastAPI 依赖：获取数据库实例"""
    return Database(settings.db_path)


def verify_token(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Database = Depends(get_db),
) -> str:
    """从 Bearer token 中验证用户身份"""
    if credentials is None:
        raise HTTPException(401, "请登录")

    token = credentials.credentials
    conn = None
    try:
        conn = db.conn()
        for r in conn.execute("SELECT username, password_hash FROM admin_users").fetchall():
            expected = base64.b64encode(
                f"{r['username']}:{r['password_hash']}".encode()
            ).decode()
            if token == expected:
                return r["username"]
    finally:
        if conn:
            conn.close()

    raise HTTPException(401, "token 无效")


def authenticate(user: str, password: str, db: Database) -> str | None:
    """验证用户凭据，成功返回 token，失败返回 None"""
    conn = None
    try:
        conn = db.conn()
        row = conn.execute(
            "SELECT username, password_hash FROM admin_users WHERE username=?", (user,)
        ).fetchone()
        if row and bcrypt.checkpw(password.encode(), row["password_hash"].encode()):
            return base64.b64encode(
                f"{user}:{row['password_hash']}".encode()
            ).decode()
    finally:
        if conn:
            conn.close()
    return None
