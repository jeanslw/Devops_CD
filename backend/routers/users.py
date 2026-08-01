"""用户管理路由 — 仅 admin 可操作"""

import bcrypt
import pymysql
from fastapi import APIRouter, Depends
from backend.auth import get_current_user, require_admin_role, get_db, CD_SYSTEM
from backend.config import settings
from backend.models import UserCreateRequest, ChangePasswordRequest
from backend.database import Database
from backend.exceptions import (
    AppException, ValidationError, NotFoundError, ConflictError,
)

router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("")
def list_users(
    db: Database = Depends(get_db),
    _user: dict = Depends(require_admin_role),
):
    """列出所有用户（仅 admin / super_admin）"""
    with db.conn() as conn:
        rows = conn.execute(
            "SELECT username, role FROM admin_users ORDER BY username"
        ).fetchall()
        return [{"username": r["username"], "role": r.get("role", settings.admin_role)} for r in rows]


@router.post("")
def create_user(
    req: UserCreateRequest,
    db: Database = Depends(get_db),
    user: dict = Depends(require_admin_role),
):
    """创建新用户（仅 admin / super_admin）。只有 super_admin 可以创建管理员账号。"""
    if not req.username or not req.password:
        raise ValidationError("用户名和密码不能为空", error_key="errors.user_pass_required")
    if req.role not in (settings.admin_role, settings.viewer_role, settings.deployer_role):
        raise ValidationError("无效的角色", error_key="errors.invalid_role")
    if req.role == settings.admin_role and user.get("role") != "super_admin":
        raise AppException("只有 super_admin 可以创建管理员账号", status_code=403, error_key="errors.super_admin_only")

    pwd_hash = bcrypt.hashpw(req.password.encode(), bcrypt.gensalt()).decode()
    with db.conn() as conn:
        existing = conn.execute(
            "SELECT username FROM admin_users WHERE username=?", (req.username,)
        ).fetchone()
        if existing:
            raise ConflictError(f"用户 '{req.username}' 已存在", error_key="errors.user_exists", error_params={"username": req.username})

        conn.execute(
            f"INSERT INTO admin_users (username, password_hash, role, systems) VALUES (?, ?, ?, '{CD_SYSTEM}')",
            (req.username, pwd_hash, req.role),
        )
        return {"username": req.username, "role": req.role, "systems": CD_SYSTEM}


@router.delete("/{username}")
def delete_user(
    username: str,
    db: Database = Depends(get_db),
    user: dict = Depends(require_admin_role),
):
    """删除用户（仅 admin / super_admin），不能删除自己。只有 super_admin 可以删除管理员。"""
    if username == user["username"]:
        raise ValidationError("不能删除自己的账户", error_key="errors.cannot_delete_self")

    with db.conn() as conn:
        target = conn.execute(
            "SELECT role FROM admin_users WHERE username=?", (username,)
        ).fetchone()
        if target is None:
            raise NotFoundError(f"用户 '{username}' 不存在", error_key="errors.user_not_found", error_params={"username": username})
        if target["role"] in (settings.admin_role, settings.super_admin_role) and user.get("role") != "super_admin":
            raise AppException("只有 super_admin 可以删除管理员账号", status_code=403, error_key="errors.super_admin_only")

        conn.execute("DELETE FROM admin_users WHERE username=?", (username,))
        return {"deleted": username}


@router.put("/{username}/role")
def change_role(
    username: str,
    req: dict,
    db: Database = Depends(get_db),
    user: dict = Depends(require_admin_role),
):
    """修改用户角色（仅 admin / super_admin），不能修改自己的角色。只有 super_admin 可指定/修改管理员角色。"""
    role = req.get("role", "")
    if role not in (settings.admin_role, settings.viewer_role, settings.deployer_role):
        raise ValidationError("无效的角色", error_key="errors.invalid_role")
    if role == settings.admin_role and user.get("role") != "super_admin":
        raise AppException("只有 super_admin 可以设置管理员角色", status_code=403, error_key="errors.super_admin_only")
    if username == user["username"]:
        raise ValidationError("不能修改自己的角色", error_key="errors.cannot_change_own_role")

    with db.conn() as conn:
        target = conn.execute(
            "SELECT role FROM admin_users WHERE username=?", (username,)
        ).fetchone()
        if target is None:
            raise NotFoundError(f"用户 '{username}' 不存在", error_key="errors.user_not_found", error_params={"username": username})
        if target["role"] in (settings.admin_role, settings.super_admin_role) and user.get("role") != "super_admin":
            raise AppException("只有 super_admin 可以修改管理员角色", status_code=403, error_key="errors.super_admin_only")

        conn.execute(
            "UPDATE admin_users SET role=? WHERE username=?", (role, username)
        )
        return {"username": username, "role": role}


@router.put("/{username}/password")
def change_password(
    username: str,
    req: ChangePasswordRequest,
    db: Database = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """修改密码：super_admin 可改任意用户；admin 只能改 deployer/viewer；普通用户只能改自己"""
    is_super_admin = user.get("role") == "super_admin"
    is_cd_admin = user.get("role") == "admin"
    if not is_super_admin and not is_cd_admin and user["username"] != username:
        raise AppException("无权修改其他用户的密码", status_code=403, error_key="errors.no_permission_change_pwd")

    with db.conn() as conn:
        row = conn.execute(
            "SELECT username, role, password_hash FROM admin_users WHERE username=?", (username,)
        ).fetchone()
        if not row:
            raise NotFoundError(f"用户 '{username}' 不存在", error_key="errors.user_not_found", error_params={"username": username})

        # admin 不能改上级（super_admin / admin）的密码
        if is_cd_admin and row["role"] in (settings.super_admin_role, settings.admin_role) and user["username"] != username:
            raise AppException("无权修改该用户的密码", status_code=403, error_key="errors.cannot_change_admin_pwd")

        # 非 admin / super_admin 需要验证旧密码
        if not is_super_admin and not is_cd_admin:
            if not req.old_password:
                raise ValidationError("请输入旧密码", error_key="errors.old_password_required")
            if not bcrypt.checkpw(req.old_password.encode(), row["password_hash"].encode()):
                raise ValidationError("旧密码错误", error_key="errors.old_password_wrong")

        new_hash = bcrypt.hashpw(req.new_password.encode(), bcrypt.gensalt()).decode()
        conn.execute(
            "UPDATE admin_users SET password_hash=? WHERE username=?",
            (new_hash, username),
        )
        return {"updated": username}
