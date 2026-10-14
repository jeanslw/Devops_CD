"""部署日志路由"""

import re

from fastapi import APIRouter, Depends

from backend.auth import cd_deploy_gate, get_db, verify_inbound_api_token, verify_token
from backend.database import Database
from backend.exceptions import ValidationError
from backend.services.deploy_service import DeployService

router = APIRouter(prefix="/api", tags=["logs"])

# CI→CD 只读契约端点暴露的部署记录列(审计字段,不含 output 等大字段)
_CONTRACT_COLS = (
    "id, project, tag, image, deploy_type, target, status, triggered_by, deploy_note, created_at"
)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@router.get("/deploy-logs")
def deploy_logs(
    project: str = "",
    page: int = 1,
    page_size: int = 15,
    db: Database = Depends(get_db),
    _username: str = Depends(verify_token),
):
    return DeployService(db).list_logs(project, page, page_size)


@router.get("/integration/deploy-logs")
def integration_deploy_logs(
    project: str = "",
    status: str = "",
    deploy_type: str = "",
    date_from: str = "",
    date_to: str = "",
    page: int = 1,
    per_page: int = 20,
    db: Database = Depends(get_db),
    _auth: None = Depends(verify_inbound_api_token),
):
    """部署记录只读契约(CI→CD):10 列 + 筛选 + 分页;可用性走 HTTP 状态码."""
    cd_deploy_gate(db)

    for name, value in (("date_from", date_from), ("date_to", date_to)):
        if value and not _DATE_RE.match(value):
            raise ValidationError(f"{name} 格式须为 YYYY-MM-DD")

    page = max(page, 1)
    per_page = max(min(per_page, 100), 1)
    offset = (page - 1) * per_page

    where: list[str] = []
    params: list = []
    if project:
        where.append("project LIKE ?")
        params.append(f"%{project}%")
    if status:
        where.append("status = ?")
        params.append(status)
    if deploy_type:
        where.append("deploy_type = ?")
        params.append(deploy_type)
    if date_from:
        where.append("created_at >= ?")
        params.append(date_from)
    if date_to:
        where.append("created_at <= ?")
        params.append(f"{date_to} 23:59:59")

    where_sql = f" WHERE {' AND '.join(where)}" if where else ""

    with db.conn() as conn:
        total = conn.execute(f"SELECT COUNT(*) AS cnt FROM cd_deploy_logs{where_sql}", params).fetchone()["cnt"]
        rows = conn.execute(
            f"SELECT {_CONTRACT_COLS} FROM cd_deploy_logs{where_sql} ORDER BY id DESC LIMIT ? OFFSET ?",
            (*params, per_page, offset),
        ).fetchall()

    items = []
    for r in rows:
        item = dict(r)
        if item.get("created_at") is not None:
            item["created_at"] = str(item["created_at"])
        items.append(item)

    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": (total + per_page - 1) // per_page,
        "items": items,
    }
