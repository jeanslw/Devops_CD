"""服务器管理路由"""

from fastapi import APIRouter, HTTPException, Depends
from app.database import Database
from app.auth import get_db, verify_token
from app.models import ServerRequest

router = APIRouter(prefix="/api/servers", tags=["servers"])


@router.get("")
def list_servers(
    db: Database = Depends(get_db),
    username: str = Depends(verify_token),
):
    conn = db.conn()
    try:
        return [
            dict(r)
            for r in conn.execute("SELECT * FROM cd_servers ORDER BY name").fetchall()
        ]
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
            "INSERT INTO cd_servers (name,host,port,user,password,type,tags) VALUES (?,?,?,?,?,?,?)",
            (req.name, req.host, req.port, req.user, req.password, req.type, req.tags),
        )
        conn.commit()
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
            "UPDATE cd_servers SET name=?, host=?, port=?, user=?, password=?, type=?, tags=? WHERE id=?",
            (req.name, req.host, req.port, req.user, req.password, req.type, req.tags, sid),
        )
        conn.commit()
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
        return {"success": True}
    finally:
        conn.close()
