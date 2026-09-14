"""用户只读路由 — 账号的创建/改角色/改密/删除统一在 CI（Devops-Glue）侧管理，CD 不提供写接口。"""

from fastapi import APIRouter, Depends

from backend.auth import require_perm
from backend.exceptions import ServiceUnavailableError
from backend.services.ci_client import CiClientError, get_ci_client

router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("")
def list_users(_user: dict = Depends(require_perm("cd.deploy.approve"))):
    """列出所有用户，供审批规则选择审批人。读走 CI 接口。"""
    try:
        users = get_ci_client().list_users()
    except CiClientError as e:
        raise ServiceUnavailableError("CI 服务不可用，请联系管理员", error_key="errors.ci_service_unavailable") from e
    # 保持既有响应形状 {username, role}（systems/status 留待后续 UI 使用）
    return [{"username": u["username"], "role": u["role"]} for u in users if u.get("username")]
