"""服务器管理路由"""

from fastapi import APIRouter, HTTPException, Depends
from app.database import Database
from app.auth import get_db, verify_token
from app.models import ServerRequest
from app.crypto import encrypt, decrypt_server_row
from app.routers.monitor import clear_server_cache

router = APIRouter(prefix="/api/servers", tags=["servers"])


@router.get("")
def list_servers(
    db: Database = Depends(get_db),
    username: str = Depends(verify_token),
):
    conn = db.conn()
    try:
        rows = conn.execute("SELECT * FROM cd_servers ORDER BY name").fetchall()
        return [decrypt_server_row(dict(r)) for r in rows]
    finally:
        conn.close()


@router.post("")
def add_server(
    req: ServerRequest,
    db: Database = Depends(get_db),
    username: str = Depends(verify_token),
):
    conn = db.conn()
    try:
        conn.execute(
            "INSERT INTO cd_servers (name,host,port,user,auth_type,password,ssh_key,type,tags) VALUES (?,?,?,?,?,?,?,?,?)",
            (req.name, req.host, req.port, req.user, req.auth_type,
             encrypt(req.password), encrypt(req.ssh_key), req.type, req.tags),
        )
        conn.commit()
        clear_server_cache()
        return {"success": True}
    except Exception as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.put("/{sid}")
def update_server(
    sid: int,
    req: ServerRequest,
    db: Database = Depends(get_db),
    username: str = Depends(verify_token),
):
    conn = db.conn()
    try:
        conn.execute(
            "UPDATE cd_servers SET name=?, host=?, port=?, user=?, auth_type=?, password=?, ssh_key=?, type=?, tags=? WHERE id=?",
            (req.name, req.host, req.port, req.user, req.auth_type,
             encrypt(req.password), encrypt(req.ssh_key), req.type, req.tags, sid),
        )
        conn.commit()
        clear_server_cache()
        return {"success": True}
    except Exception as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.delete("/{sid}")
def delete_server(
    sid: int,
    db: Database = Depends(get_db),
    username: str = Depends(verify_token),
):
    conn = db.conn()
    try:
        conn.execute("DELETE FROM cd_servers WHERE id=?", (sid,))
        conn.commit()
        clear_server_cache()
        return {"success": True}
    finally:
        conn.close()
