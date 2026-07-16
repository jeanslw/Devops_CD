"""认证路由"""

from fastapi import APIRouter, HTTPException, Depends
from app.auth import authenticate, get_db
from app.models import LoginRequest
from app.database import Database

router = APIRouter(tags=["auth"])


@router.post("/api/login")
def login(req: LoginRequest, db: Database = Depends(get_db)):
    token = authenticate(req.user, req.password, db)
    if token:
        return {"token": token}
    raise HTTPException(401, "账号或密码错误")
