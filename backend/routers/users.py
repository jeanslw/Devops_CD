"""用户管理路由 — 仅 admin 可操作"""

import bcrypt
from fastapi import APIRouter, HTTPException, Depends
from backend.auth import get_current_user, require_admin, get_db, CD_SYSTEM
from backend.config import settings
from backend.models import UserCreateRequest, ChangePasswordRequest
from backend.database import Database

router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("")
def list_users(
    db: Database = Depends(get_db),
    admin: dict = Depends(require_admin),
):
    """列出所有用户（仅 admin）"""
    with db.conn() as conn:
        rows = conn.execute(
            "SELECT username, role FROM admin_users ORDER BY username"
        ).fetchall()
        return [{"username": r["username"], "role": r.get("role", settings.admin_role)} for r in rows]


@router.post("")
def create_user(
    req: UserCreateRequest,
    db: Database = Depends(get_db),
    admin: dict = Depends(require_admin),
):
    """创建新用户（仅 admin）。只有 super_admin 可以创建管理员账号。"""
    if not req.username or not req.password:
        raise HTTPException(400, "用户名和密码不能为空")
    if req.role not in (settings.admin_role, settings.viewer_role, settings.deployer_role):
        raise HTTPException(400, "无效的角色")
    if req.role == settings.admin_role and admin["role"] != settings.super_admin_role:
        raise HTTPException(403, "只有 super_admin 可以创建管理员账号")

    pwd_hash = bcrypt.hashpw(req.password.encode(), bcrypt.gensalt()).decode()
    with db.conn() as conn:
        existing = conn.execute(
            "SELECT username FROM admin_users WHERE username=?", (req.username,)
        ).fetchone()
        if existing:
            raise HTTPException(409, f"用户 '{req.username}' 已存在")

        conn.execute(
            f"INSERT INTO admin_users (username, password_hash, role, systems) VALUES (?, ?, ?, '{CD_SYSTEM}')",
            (req.username, pwd_hash, req.role),
        )
        conn.commit()
        return {"username": req.username, "role": req.role, "systems": CD_SYSTEM}


@router.delete("/{username}")
def delete_user(
    username: str,
    db: Database = Depends(get_db),
    admin: dict = Depends(require_admin),
):
    """删除用户（仅 admin），不能删除自己。只有 super_admin 可以删除管理员。"""
    if username == admin["username"]:
        raise HTTPException(400, "不能删除自己的账户")

    with db.conn() as conn:
        target = conn.execute(
            "SELECT role FROM admin_users WHERE username=?", (username,)
        ).fetchone()
        if target is None:
            raise HTTPException(404, f"用户 '{username}' 不存在")
        if target["role"] in (settings.admin_role, settings.super_admin_role) and admin["role"] != settings.super_admin_role:
            raise HTTPException(403, "只有 super_admin 可以删除管理员账号")

        conn.execute("DELETE FROM admin_users WHERE username=?", (username,))
        conn.commit()
        return {"deleted": username}


@router.put("/{username}/role")
def change_role(
    username: str,
    req: dict,
    db: Database = Depends(get_db),
    admin: dict = Depends(require_admin),
):
    """修改用户角色（仅 admin），不能修改自己的角色。只有 super_admin 可指定/修改管理员角色。"""
    role = req.get("role", "")
    if role not in (settings.admin_role, settings.viewer_role, settings.deployer_role):
        raise HTTPException(400, "无效的角色")
    if role == settings.admin_role and admin["role"] != settings.super_admin_role:
        raise HTTPException(403, "只有 super_admin 可以设置管理员角色")
    if username == admin["username"]:
        raise HTTPException(400, "不能修改自己的角色")

    with db.conn() as conn:
        target = conn.execute(
            "SELECT role FROM admin_users WHERE username=?", (username,)
        ).fetchone()
        if target is None:
            raise HTTPException(404, f"用户 '{username}' 不存在")
        if target["role"] in (settings.admin_role, settings.super_admin_role) and admin["role"] != settings.super_admin_role:
            raise HTTPException(403, "只有 super_admin 可以修改管理员角色")

        conn.execute(
            "UPDATE admin_users SET role=? WHERE username=?", (role, username)
        )
        conn.commit()
        return {"username": username, "role": role}


@router.put("/{username}/password")
def change_password(
    username: str,
    req: ChangePasswordRequest,
    db: Database = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """修改密码：super_admin 可改任意用户；admin 只能改 deployer/viewer；普通用户只能改自己"""
    if user["role"] not in (settings.admin_role, settings.super_admin_role) and user["username"] != username:
        raise HTTPException(403, "无权修改其他用户的密码")

    with db.conn() as conn:
        row = conn.execute(
            "SELECT username, role, password_hash FROM admin_users WHERE username=?", (username,)
        ).fetchone()
        if not row:
            raise HTTPException(404, f"用户 '{username}' 不存在")

        # admin 不能改上级（super_admin / admin）的密码
        if user["role"] == settings.admin_role and row["role"] in (settings.super_admin_role, settings.admin_role) and user["username"] != username:
            raise HTTPException(403, "无权修改该用户的密码")

        # 非 admin / super_admin 需要验证旧密码
        if user["role"] not in (settings.admin_role, settings.super_admin_role):
            if not req.old_password:
                raise HTTPException(400, "请输入旧密码")
            if not bcrypt.checkpw(req.old_password.encode(), row["password_hash"].encode()):
                raise HTTPException(400, "旧密码错误")

        new_hash = bcrypt.hashpw(req.new_password.encode(), bcrypt.gensalt()).decode()
        conn.execute(
            "UPDATE admin_users SET password_hash=? WHERE username=?",
            (new_hash, username),
        )
        conn.commit()
        return {"updated": username}
