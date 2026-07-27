"""认证路由"""

from fastapi import APIRouter, HTTPException, Depends
from backend.auth import authenticate, get_db, get_current_user
from backend.models import LoginRequest
from backend.database import Database

router = APIRouter(tags=["auth"])


@router.post("/api/login")
def login(req: LoginRequest, db: Database = Depends(get_db)):
    token = authenticate(req.user, req.password, db)
    if token:
        return {"token": token}
    raise HTTPException(401, "账号或密码错误")


@router.get("/api/me")
def me(user: dict = Depends(get_current_user)):
    """返回当前登录用户信息"""
    return user
