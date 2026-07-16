"""CI 项目路由 — 项目列表 + Pipeline 状态"""

from fastapi import APIRouter, Depends
from app.database import Database
from app.auth import get_db
from app.services.ci_service import CiService

router = APIRouter(prefix="/api", tags=["projects"])


@router.get("/projects")
def list_projects(db: Database = Depends(get_db)):
    """列出所有 CI 项目及最新 tag/pipeline"""
    return CiService(db).list_projects()


@router.get("/projects/{project:path}/tags")
def project_tags(project: str, db: Database = Depends(get_db)):
    """获取项目的所有 pipeline tag 列表"""
    from app.services.ci_service import CiService
    svc = CiService(db)
    conn = db.conn()
    try:
        svc._resolve_tables(conn)
        keys = [project]
        map_row = conn.execute(
            f"SELECT current_path FROM {svc._job_map} WHERE (job_name=? OR current_path=?) AND status='active'",
            (project, project),
        ).fetchone()
        if map_row and map_row["current_path"] and map_row["current_path"] != project:
            keys.append(map_row["current_path"])
        placeholders = ",".join("?" * len(keys))
        rows = conn.execute(
            f"SELECT tag, pipeline_iid, created_at FROM {svc._pipeline_tags} "
            f"WHERE project IN ({placeholders}) ORDER BY created_at DESC",
            keys,
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/projects/{project:path}/pipeline")
def pipeline_status(project: str, db: Database = Depends(get_db)):
    """获取项目实时 pipeline 状态（调 PHP API）"""
    result = CiService(db).get_pipeline_status(project)
    if result is None:
        from fastapi import HTTPException
        raise HTTPException(404, f"项目 '{project}' 不存在")
    return result
