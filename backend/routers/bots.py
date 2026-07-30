"""BOT 管理路由 — 钉钉/企微/自定义 webhook"""

from fastapi import APIRouter, HTTPException, Depends
from backend.database import Database
from backend.auth import get_db, verify_token, require_perm
from backend.models import BotRequest

router = APIRouter(prefix="/api/bots", tags=["bots"])


@router.get("")
def list_bots(
    db: Database = Depends(get_db),
    username: str = Depends(verify_token),
):
    with db.conn() as conn:
        return [
            dict(r)
            for r in conn.execute("SELECT * FROM cd_bots ORDER BY name").fetchall()
        ]


@router.post("")
def add_bot(
    req: BotRequest,
    db: Database = Depends(get_db),
    _user: dict = Depends(require_perm("cd.notification-manage")),
):
    with db.conn() as conn:
        try:
            conn.execute(
                "INSERT INTO cd_bots (name,type,webhook_url,template) VALUES (?,?,?,?)",
                (req.name, req.type, req.webhook_url, req.template or ""),
            )
            conn.commit()
            return {"success": True}
        except Exception:
            raise HTTPException(400, f"Bot '{req.name}' already exists")


@router.delete("/{bid}")
def delete_bot(
    bid: int,
    db: Database = Depends(get_db),
    _user: dict = Depends(require_perm("cd.notification-manage")),
):
    with db.conn() as conn:
        conn.execute("DELETE FROM cd_bots WHERE id=?", (bid,))
        conn.commit()
        return {"success": True}
