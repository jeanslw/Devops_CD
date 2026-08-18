"""部署日志路由"""

from fastapi import APIRouter, Depends

from backend.auth import get_db, verify_token
from backend.database import Database
from backend.services.deploy_service import DeployService

router = APIRouter(prefix="/api", tags=["logs"])


@router.get("/deploy-logs")
def deploy_logs(
    project: str = "",
    page: int = 1,
    page_size: int = 15,
    db: Database = Depends(get_db),
    _username: str = Depends(verify_token),
):
    return DeployService(db).list_logs(project, page, page_size)
