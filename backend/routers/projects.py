"""CI 项目路由 — 项目列表 + Pipeline 状态（需登录）"""

from fastapi import APIRouter, Depends
from backend.database import Database
from backend.auth import get_db, verify_token
from backend.services.ci_service import CiService

router = APIRouter(prefix="/api", tags=["projects"])


@router.get("/projects")
def list_projects(
    _user: str = Depends(verify_token),
    db: Database = Depends(get_db),
):
    """列出所有 CI 项目及最新 tag/pipeline"""
    return CiService(db).list_projects()


@router.get("/projects/{project:path}/tags")
def project_tags(
    project: str,
    page: int = 1,
    page_size: int = 50,
    _user: str = Depends(verify_token),
    db: Database = Depends(get_db),
):
    """获取项目的 pipeline tag 列表（分页）"""
    from backend.services.ci_service import CiService
    svc = CiService(db)
    with db.conn() as conn:
        svc._resolve_tables(conn)
        keys = [project]
        map_row = conn.execute(
            f"SELECT current_path FROM {svc._job_map} WHERE (job_name=? OR current_path=?) AND status='active'",
            (project, project),
        ).fetchone()
        if map_row and map_row["current_path"] and map_row["current_path"] != project:
            keys.append(map_row["current_path"])
        placeholders = ",".join("?" * len(keys))
        # 总数
        total = conn.execute(
            f"SELECT COUNT(*) as cnt FROM {svc._pipeline_tags} WHERE project IN ({placeholders})",
            keys,
        ).fetchone()["cnt"]
        # 分页
        if page_size < 1 or page_size > 200:
            page_size = 50
        total_pages = max(1, (total + page_size - 1) // page_size) if total else 1
        if page < 1:
            page = 1
        if page > total_pages:
            page = total_pages
        offset = (page - 1) * page_size
        rows = conn.execute(
            f"SELECT tag, pipeline_iid, created_at FROM {svc._pipeline_tags} "
            f"WHERE project IN ({placeholders}) ORDER BY created_at DESC LIMIT ? OFFSET ?",
            keys + [page_size, offset],
        ).fetchall()
        return {
            "items": [dict(r) for r in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
        }


@router.get("/projects/{project:path}/pipeline")
def pipeline_status(
    project: str,
    _user: str = Depends(verify_token),
    db: Database = Depends(get_db),
):
    """获取项目实时 pipeline 状态（调 PHP API）"""
    result = CiService(db).get_pipeline_status(project)
    if result is None:
        from fastapi import HTTPException
        raise HTTPException(404, f"Project '{project}' not found")
    return result
