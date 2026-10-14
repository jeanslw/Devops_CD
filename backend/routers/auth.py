"""认证路由 - 当前用户信息 + 登出"""

from fastapi import APIRouter, Depends, Request, Response
from fastapi.security import HTTPAuthorizationCredentials

from backend.auth import _extract_token, get_current_user, get_db, revoke_session, security
from backend.database import Database

router = APIRouter(tags=["auth"])

@router.get("/api/me")
def me(user: dict = Depends(get_current_user)):
    """返回当前登录用户信息"""
    return user


@router.post("/api/logout")
def logout(
    request: Request,
    response: Response,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: Database = Depends(get_db),
):
    """吊销当前会话:删除服务端会话行,token 立即失效,并清除 HttpOnly cookie.
    幂等:未登录 / 已过期 / 无效 token 也返回成功(前端本地状态照常清理)."""
    token = _extract_token(request, credentials)
    if token:
        revoke_session(db, token)
    response.delete_cookie("cd_token", path="/")
    return {"success": True}
