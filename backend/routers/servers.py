"""服务器管理路由"""

import pymysql
from fastapi import APIRouter, Depends
from backend.database import Database
from backend.auth import get_db, verify_token, require_perm
from backend.models import ServerRequest
from backend.crypto import encrypt, decrypt_server_row
from backend.services.monitor_utils import clear_server_cache
from backend.exceptions import ConflictError, DatabaseError
from backend.responses import ok

router = APIRouter(prefix="/api/servers", tags=["servers"])


@router.get("")
def list_servers(
    db: Database = Depends(get_db),
    username: str = Depends(verify_token),
):
    with db.conn() as conn:
        rows = conn.execute("SELECT * FROM cd_servers ORDER BY name").fetchall()
        return [decrypt_server_row(dict(r)) for r in rows]


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
            raise ConflictError(f"服务器 '{req.name}' 已存在")


@router.put("/{sid}")
def update_server(
    sid: int,
    req: ServerRequest,
    db: Database = Depends(get_db),
    _user: dict = Depends(require_perm("cd.server-manage")),
):
    with db.conn() as conn:
        try:
            conn.execute(
                "UPDATE cd_servers SET name=?, host=?, port=?, user=?, auth_type=?, password=?, ssh_key=?, type=?, tags=? WHERE id=?",
                (req.name, req.host, req.port, req.user, req.auth_type,
                 encrypt(req.password), encrypt(req.ssh_key), req.type, req.tags, sid),
            )
            clear_server_cache()
            return ok(message=f"服务器 '{req.name}' 已更新")
        except pymysql.err.IntegrityError:
            raise ConflictError(f"服务器 '{req.name}' 已存在")


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
