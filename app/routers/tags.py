"""标签路由 — 从 cd_servers.tags 聚合（逗号分隔）去重后作为标签库"""

from fastapi import APIRouter, Depends
from app.database import Database
from app.auth import get_db, verify_token

router = APIRouter(prefix="/api/tags", tags=["tags"])


@router.get("")
def list_tags(
    db: Database = Depends(get_db),
    username: str = Depends(verify_token),
):
    """扫描所有服务器的 tags 字段，拆分为唯一标签列表"""
    conn = db.conn()
    try:
        tag_set = set()
        rows = conn.execute(
            "SELECT tags FROM cd_servers WHERE tags IS NOT NULL AND tags != ''"
        ).fetchall()
        for r in rows:
            for t in (r["tags"] if isinstance(r, dict) else r[0] or "").split(","):
                t = t.strip()
                if t:
                    tag_set.add(t)
        return [{"name": t} for t in sorted(tag_set)]
    finally:
        conn.close()
