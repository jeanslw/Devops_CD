"""CI 项目路由 — 项目列表 + Pipeline 状态 + Tag 分页（需登录）"""

from fastapi import APIRouter, Depends

from backend.auth import verify_token
from backend.exceptions import NotFoundError, ServiceUnavailableError
from backend.services.ci_client import CiClientError, get_ci_client
from backend.services.ci_service import CiService

router = APIRouter(prefix="/api", tags=["projects"])


@router.get("/projects")
def list_projects(_user: str = Depends(verify_token)):
    """列出所有 CI 项目及最新 tag/pipeline → CI GET /api/build/projects"""
    return CiService().list_projects()


@router.get("/projects/{project:path}/tags")
def project_tags(
    project: str,
    page: int = 1,
    page_size: int = 50,
    _user: str = Depends(verify_token),
):
    """获取项目的 pipeline tag 列表（分页）→ CI GET /api/build/{path}/tags"""
    try:
        return get_ci_client().get_tags(project, page, page_size)
    except CiClientError as e:
        raise ServiceUnavailableError("CI 服务不可用，请联系管理员", error_key="errors.ci_service_unavailable") from e


@router.get("/projects/{project:path}/pipeline")
def pipeline_status(project: str, _user: str = Depends(verify_token)):
    """获取项目最新 pipeline 状态（调 PHP API）"""
    result = CiService().get_pipeline_status(project)
    if result is None:
        raise NotFoundError(
            f"项目 '{project}' 未找到", error_key="errors.project_not_found", error_params={"project": project}
        )
    return result
