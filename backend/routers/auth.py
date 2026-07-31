"""认证路由"""

from fastapi import APIRouter, Depends
from backend.auth import authenticate, get_db, get_current_user
from backend.models import LoginRequest
from backend.database import Database
from backend.exceptions import AppException

router = APIRouter(tags=["auth"])


@router.post("/api/login")
def login(req: LoginRequest, db: Database = Depends(get_db)):
    token = authenticate(req.user, req.password, db)
    if token:
        return {"token": token}
    raise AppException("账号或密码错误", status_code=401, error_key="errors.invalid_credentials")


@router.get("/api/me")
def me(user: dict = Depends(get_current_user)):
    """返回当前登录用户信息"""
    return user
