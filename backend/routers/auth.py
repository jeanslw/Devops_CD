"""认证路由"""

from fastapi import APIRouter, Depends, Request

from backend.auth import get_current_user, get_db
from backend.database import Database
from backend.exceptions import AppException
from backend.login_guard import (
    client_ip,
    clear_login_failure,
    is_login_locked,
    record_login_failure,
)
from backend.models import LoginRequest

router = APIRouter(tags=["auth"])


@router.post("/api/login")
def login(req: LoginRequest, request: Request, db: Database = Depends(get_db)):
    # 登录失败限流：与 CI（Devops-Glue）共享 cache 表计数，5 次失败锁 15 分钟。
    # 必须在验密之前检查，任何失败（含停用/无 CD 权限）都计数，成功才清零。
    ip = client_ip(request)
    username = (req.user or "").strip().lower()
    # 空用户名直接按凭据错误拒绝：不查锁、不计数，避免所有空用户名失败共享
    # md5(ip+":") 一个桶，也不泄露"该用户名不存在"之外的任何信息。
    if not username:
        raise AppException("账号或密码错误", status_code=401, error_key="errors.invalid_credentials")
    if is_login_locked(db, ip, username):
        raise AppException(
            "登录失败次数过多，账户已锁定 15 分钟，请稍后再试",
            status_code=429,
            error_key="errors.login_locked",
        )

    # 延迟导入 authenticate，避免路由模块加载期产生循环依赖风险
    from backend.auth import authenticate

    try:
        token = authenticate(req.user, req.password, db)
    except AppException:
        # 账号停用 / 无 CD 访问权限同样计入失败次数
        record_login_failure(db, ip, username)
        raise
    if token:
        clear_login_failure(db, ip, username)
        return {"token": token}
    record_login_failure(db, ip, username)
    raise AppException("账号或密码错误", status_code=401, error_key="errors.invalid_credentials")


@router.get("/api/me")
def me(user: dict = Depends(get_current_user)):
    """返回当前登录用户信息"""
    return user
