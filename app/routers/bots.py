"""BOT 管理路由 — 钉钉/企微/自定义 webhook"""

from fastapi import APIRouter, HTTPException, Depends
from app.database import Database
from app.auth import get_db, verify_token
from app.models import BotRequest

router = APIRouter(prefix="/api/bots", tags=["bots"])


@router.get("")
def list_bots(
    db: Database = Depends(get_db),
    username: str = Depends(verify_token),
):
    return [
        dict(r)
        for r in db.conn().execute("SELECT * FROM bots ORDER BY name").fetchall()
    ]


@router.post("")
def add_bot(
    req: BotRequest,
    db: Database = Depends(get_db),
    username: str = Depends(verify_token),
):
    conn = db.conn()
    try:
        conn.execute(
            "INSERT INTO bots (name,type,webhook_url) VALUES (?,?,?)",
            (req.name, req.type, req.webhook_url),
        )
        conn.commit()
        return {"success": True}
    except Exception:
        raise HTTPException(400, f"BOT '{req.name}' 已存在")


@router.delete("/{bid}")
def delete_bot(
    bid: int,
    db: Database = Depends(get_db),
    username: str = Depends(verify_token),
):
    conn = db.conn()
    conn.execute("DELETE FROM bots WHERE id=?", (bid,))
    conn.commit()
    return {"success": True}
