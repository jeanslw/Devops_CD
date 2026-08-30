"""用户管理路由 — 仅 cd_admin / super_admin 可操作。写/读均走 CI 接口，不直读 admin_users。"""

from fastapi import APIRouter, Depends

from backend.auth import CD_SYSTEM, get_current_user, require_admin_role, require_perm
from backend.config import settings
from backend.exceptions import (
    AppException,
    ConflictError,
    NotFoundError,
    ServiceUnavailableError,
    ValidationError,
)
from backend.models import ChangePasswordRequest, UserCreateRequest
from backend.services.ci_client import CiClientError, get_ci_client

router = APIRouter(prefix="/api/users", tags=["users"])


def _raise_ci_rbac_error(e: CiClientError, username: str = "") -> None:
    """将 CI /api/rbac/users 接口错误按状态码映射为 CD 异常。"""
    status = e.status_code or 500
    if status == 409:
        raise ConflictError(
            f"用户 '{username}' 已存在", error_key="errors.user_exists", error_params={"username": username}
        ) from e
    if status == 404:
        raise NotFoundError(
            f"用户 '{username}' 不存在", error_key="errors.user_not_found", error_params={"username": username}
        ) from e
    if status == 403:
        raise AppException("仅 super_admin 可执行此操作", status_code=403, error_key="errors.super_admin_only") from e
    if status == 400:
        raise ValidationError(e.remote_message or "参数无效", error_key=None) from e
    raise ServiceUnavailableError("CI 服务不可用，请联系管理员", error_key="errors.ci_service_unavailable") from e


def _get_rbac_user(username: str) -> dict:
    """经 CI 读取单个用户（用于目标角色 / 存在性校验）。404 → NotFoundError。"""
    try:
        target = get_ci_client().get_user(username)
    except CiClientError as e:
        _raise_ci_rbac_error(e, username)
    if not target:
        raise NotFoundError(
            f"用户 '{username}' 不存在", error_key="errors.user_not_found", error_params={"username": username}
        )
    return target


@router.get("")
def list_users(_user: dict = Depends(require_perm("cd.deploy-manage"))):
    """列出所有用户，供用户管理/审批规则选择审批人。读走 CI 接口。"""
    try:
        users = get_ci_client().list_users()
    except CiClientError as e:
        raise ServiceUnavailableError("CI 服务不可用，请联系管理员", error_key="errors.ci_service_unavailable") from e
    # 保持既有响应形状 {username, role}（systems/status 留待后续 UI 使用）
    return [{"username": u["username"], "role": u["role"]} for u in users if u.get("username")]


@router.post("")
def create_user(
    req: UserCreateRequest,
    user: dict = Depends(require_admin_role),
):
    """创建新用户（仅 cd_admin / super_admin）。只有 super_admin 可以创建管理员账号。写走 CI 接口。"""
    if not req.username or not req.password:
        raise ValidationError("用户名和密码不能为空", error_key="errors.user_pass_required")
    if req.role not in (settings.admin_role, settings.viewer_role, settings.deployer_role):
        raise ValidationError("无效的角色", error_key="errors.invalid_role")
    if req.role == settings.admin_role and user.get("role") != settings.super_admin_role:
        raise AppException("只有 super_admin 可以创建管理员账号", status_code=403, error_key="errors.super_admin_only")

    try:
        get_ci_client().create_user(req.username, req.password, req.role)
    except CiClientError as e:
        _raise_ci_rbac_error(e, req.username)
    return {"username": req.username, "role": req.role, "systems": CD_SYSTEM}


@router.delete("/{username}")
def delete_user(
    username: str,
    user: dict = Depends(require_admin_role),
):
    """删除用户（仅 cd_admin / super_admin），不能删除自己。只有 super_admin 可以删除管理员。写走 CI 接口。"""
    if username == user["username"]:
        raise ValidationError("不能删除自己的账户", error_key="errors.cannot_delete_self")

    target = _get_rbac_user(username)
    if target["role"] in (settings.admin_role, settings.super_admin_role) and user.get("role") != settings.super_admin_role:
        raise AppException(
            "只有 super_admin 可以删除管理员账号", status_code=403, error_key="errors.super_admin_only"
        )

    try:
        get_ci_client().delete_user(username)
    except CiClientError as e:
        _raise_ci_rbac_error(e, username)
    return {"deleted": username}


@router.put("/{username}/role")
def change_role(
    username: str,
    req: dict,
    user: dict = Depends(require_admin_role),
):
    """修改用户角色（仅 cd_admin / super_admin），不能修改自己的角色。只有 super_admin 可指定/修改管理员角色。写走 CI 接口。"""
    role = req.get("role", "")
    if role not in (settings.admin_role, settings.viewer_role, settings.deployer_role):
        raise ValidationError("无效的角色", error_key="errors.invalid_role")
    if role == settings.admin_role and user.get("role") != settings.super_admin_role:
        raise AppException("只有 super_admin 可以设置管理员角色", status_code=403, error_key="errors.super_admin_only")
    if username == user["username"]:
        raise ValidationError("不能修改自己的角色", error_key="errors.cannot_change_own_role")

    target = _get_rbac_user(username)
    if target["role"] in (settings.admin_role, settings.super_admin_role) and user.get("role") != settings.super_admin_role:
        raise AppException(
            "只有 super_admin 可以修改管理员角色", status_code=403, error_key="errors.super_admin_only"
        )

    try:
        get_ci_client().update_user(username, role=role)
    except CiClientError as e:
        _raise_ci_rbac_error(e, username)
    return {"username": username, "role": role}


@router.put("/{username}/password")
def change_password(
    username: str,
    req: ChangePasswordRequest,
    user: dict = Depends(get_current_user),
):
    """修改密码：super_admin 可改任意用户；cd_admin 只能改 deployer/viewer；普通用户只能改自己。写走 CI 接口。"""
    is_super_admin = user.get("role") == settings.super_admin_role
    is_cd_admin = user.get("role") == settings.admin_role
    if not is_super_admin and not is_cd_admin and user["username"] != username:
        raise AppException("无权修改其他用户的密码", status_code=403, error_key="errors.no_permission_change_pwd")

    target = _get_rbac_user(username)

    # admin 不能改上级（super_admin / admin）的密码
    if (
        is_cd_admin
        and target["role"] in (settings.super_admin_role, settings.admin_role)
        and user["username"] != username
    ):
        raise AppException("无权修改该用户的密码", status_code=403, error_key="errors.cannot_change_admin_pwd")

    # 非 admin / super_admin 需要验证旧密码（哈希不出 CI，走 verify-password）
    if not is_super_admin and not is_cd_admin:
        if not req.old_password:
            raise ValidationError("请输入旧密码", error_key="errors.old_password_required")
        try:
            if not get_ci_client().verify_password(username, req.old_password):
                raise ValidationError("旧密码错误", error_key="errors.old_password_wrong")
        except CiClientError as e:
            _raise_ci_rbac_error(e, username)

    try:
        get_ci_client().update_user(username, password=req.new_password)
    except CiClientError as e:
        _raise_ci_rbac_error(e, username)
    return {"updated": username}
