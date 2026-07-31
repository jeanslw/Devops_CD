"""服务器管理路由"""

import pymysql
from fastapi import APIRouter, Depends
from backend.database import Database
from backend.auth import get_db, verify_token, require_perm
from backend.models import ServerRequest
from backend.crypto import encrypt
from backend.services.monitor_utils import clear_server_cache
from backend.exceptions import ConflictError, DatabaseError, NotFoundError
from backend.responses import ok

router = APIRouter(prefix="/api/servers", tags=["servers"])


MASK = "***"

@router.get("")
def list_servers(
    db: Database = Depends(get_db),
    username: str = Depends(verify_token),
):
    with db.conn() as conn:
        rows = conn.execute("SELECT * FROM cd_servers ORDER BY name").fetchall()
        result = []
        for r in rows:
            row = dict(r)
            # 不暴露明文 key/password，只标记是否存在
            row["has_password"] = bool(row.get("password"))
            row["has_ssh_key"] = bool(row.get("ssh_key"))
            row["password"] = MASK if row.get("password") else ""
            row["ssh_key"] = MASK if row.get("ssh_key") else ""
            result.append(row)
        return result


@router.post("")
def add_server(
    req: ServerRequest,
    db: Database = Depends(get_db),
    _user: dict = Depends(require_perm("cd.server-manage")),
):
    with db.conn() as conn:
        try:
            conn.execute(
                "INSERT INTO cd_servers (name,host,port,user,auth_type,password,ssh_key,type,tags) VALUES (?,?,?,?,?,?,?,?,?)",
                (req.name, req.host, req.port, req.user, req.auth_type,
                 encrypt(req.password), encrypt(req.ssh_key), req.type, req.tags),
            )
            clear_server_cache()
            return ok(message=f"服务器 '{req.name}' 已添加")
        except pymysql.err.IntegrityError:
            raise ConflictError(f"服务器 '{req.name}' 已存在", error_key="errors.server_exists", error_params={"name": req.name})


@router.put("/{sid}")
def update_server(
    sid: int,
    req: ServerRequest,
    db: Database = Depends(get_db),
    _user: dict = Depends(require_perm("cd.server-manage")),
):
    with db.conn() as conn:
        existing = conn.execute("SELECT * FROM cd_servers WHERE id=?", (sid,)).fetchone()
        if not existing:
            raise NotFoundError("服务器不存在", error_key="errors.server_not_found")

        # 前端发来 MASK 表示未修改，保留数据库现有值
        password = existing["password"]
        if req.password and req.password != MASK:
            password = encrypt(req.password)

        ssh_key = existing["ssh_key"]
        if req.ssh_key and req.ssh_key != MASK:
            ssh_key = encrypt(req.ssh_key)
        elif req.ssh_key == "":
            ssh_key = ""  # 显式清空

        # 切换认证方式时清除旧凭据
        if req.auth_type == "password":
            ssh_key = ""
        elif req.auth_type == "key":
            password = ""

        try:
            conn.execute(
                "UPDATE cd_servers SET name=?, host=?, port=?, user=?, auth_type=?, password=?, ssh_key=?, type=?, tags=? WHERE id=?",
                (req.name, req.host, req.port, req.user, req.auth_type,
                 password, ssh_key, req.type, req.tags, sid),
            )
            clear_server_cache()
            return ok(message=f"服务器 '{req.name}' 已更新")
        except pymysql.err.IntegrityError:
            raise ConflictError(f"服务器 '{req.name}' 已存在", error_key="errors.server_exists", error_params={"name": req.name})


@router.delete("/{sid}")
def delete_server(
    sid: int,
    db: Database = Depends(get_db),
    _user: dict = Depends(require_perm("cd.server-manage")),
):
    with db.conn() as conn:
        conn.execute("DELETE FROM cd_servers WHERE id=?", (sid,))
        clear_server_cache()
        return ok(message="服务器已删除")
