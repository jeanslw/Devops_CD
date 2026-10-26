"""镜像制品库 API - 仓库/Artifact/同步/扫描(多类型:Harbor / 标准 registry:2)"""

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.auth import get_db, require_perm
from backend.database import Database
from backend.exceptions import NotFoundError, ServiceUnavailableError, ValidationError
from backend.services.registry_provider import RegistryPermissionDeniedError
from backend.services.registry_service import HarborUnavailableError, RegistryService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/registry", tags=["registry"])


class SyncConfigRequest(BaseModel):
    interval: int  # 分钟,0 关闭


@router.get("/repositories")
def list_repositories(
    _user: dict = Depends(require_perm("cd.image-registry")),
    db: Database = Depends(get_db),
):
    """列出所有仓库(从数据库缓存读取)"""
    svc = RegistryService(db)
    return svc.get_repositories()


@router.get("/artifacts/{repo_id}")
def list_artifacts(
    repo_id: int,
    page: int = 1,
    page_size: int = 20,
    _user: dict = Depends(require_perm("cd.image-registry")),
    db: Database = Depends(get_db),
):
    """列出指定仓库的 artifacts/tags(分页,含扫描摘要)"""
    svc = RegistryService(db)
    return svc.get_artifacts(repo_id, page=page, page_size=page_size)


@router.get("/scan/report/{repo_id}/{tag}")
def get_scan_report(
    repo_id: int,
    tag: str,
    _user: dict = Depends(require_perm("cd.image-registry")),
    db: Database = Depends(get_db),
):
    """从数据库缓存读取扫描报告(秒开)"""
    try:
        svc = RegistryService(db)
        result = svc.get_scan_report(repo_id, tag)
    except RegistryPermissionDeniedError as e:
        # 仓库账号无查询权限(401/403):明确提示,不落 500
        logger.error("Registry permission denied", exc_info=e)
        raise ValidationError("镜像仓库账号没有查询权限", error_key="errors.registry_permission_denied") from e
    if result is None:
        raise NotFoundError("扫描报告未找到", error_key="errors.scan_report_not_found")
    if isinstance(result, dict) and "error" in result:
        # 透传 service 自带的 error_key(如"该仓库类型不支持漏洞扫描"),前端据此给出准确提示;
        # 没有 error_key 的(Tag 不存在等)归入扫描报告错误
        raise NotFoundError(result["error"], error_key=result.get("error_key", "errors.scan_report_error"))
    return result


@router.post("/scan/trigger/{repo_id}/{tag}")
def trigger_scan(
    repo_id: int,
    tag: str,
    _user: dict = Depends(require_perm("cd.image-registry")),
    db: Database = Depends(get_db),
):
    """触发 Harbor 重新扫描该镜像(标准 registry:2 无此能力,由 service 闸门拦截)"""
    try:
        svc = RegistryService(db)
        result = svc.trigger_scan(repo_id, tag)
        if not result.get("ok"):
            # 优先透传 service 给出的 error_key(如 errors.scan_unsupported)
            raise ValidationError(
                result.get("error", "触发失败"),
                error_key=result.get("error_key", "errors.scan_trigger_failed"),
            )
        return result
    except RegistryPermissionDeniedError as e:
        logger.error("Registry permission denied", exc_info=e)
        raise ValidationError("镜像仓库账号没有查询权限", error_key="errors.registry_permission_denied") from e
    except HarborUnavailableError as e:
        logger.error("Harbor unavailable", exc_info=e)
        raise ServiceUnavailableError("Harbor 服务不可用，请联系管理员", error_key="errors.harbor_unavailable") from e


@router.post("/sync")
def trigger_sync(
    project: str = "",
    _user: dict = Depends(require_perm("cd.image-registry")),
    db: Database = Depends(get_db),
):
    """触发同步:留空全量同步,指定 project 增量同步"""
    try:
        svc = RegistryService(db)
        if project:
            return svc.sync_for_project(project)
        return svc.sync_all()
    except HarborUnavailableError as e:
        logger.error("Harbor unavailable", exc_info=e)
        raise ServiceUnavailableError("Harbor 服务不可用，请联系管理员", error_key="errors.harbor_unavailable") from e


@router.get("/config")
def get_sync_config(
    _user: dict = Depends(require_perm("cd.image-registry")),
    db: Database = Depends(get_db),
):
    """获取定时同步配置与镜像仓库类型/能力开关(前端据此隐藏不可用操作)"""
    try:
        svc = RegistryService(db)
        return {"interval": svc.get_sync_interval(), **svc.get_registry_meta()}
    except Exception as e:
        logger.error("get_sync_config error", exc_info=e)
        raise ServiceUnavailableError("读取配置失败，请联系管理员", error_key="errors.sync_config_read_failed") from e


@router.put("/config")
def update_sync_config(
    body: SyncConfigRequest,
    _user: dict = Depends(require_perm("cd.image-registry")),
    db: Database = Depends(get_db),
):
    """更新定时同步间隔(分钟,0=关闭)"""
    if body.interval < 0:
        raise ValidationError("间隔不能为负数", error_key="errors.negative_interval")
    try:
        svc = RegistryService(db)
        svc.set_sync_interval(body.interval)
    except Exception as e:
        logger.error("update_sync_config error", exc_info=e)
        raise ServiceUnavailableError("保存配置失败，请联系管理员", error_key="errors.sync_config_save_failed") from e
    if body.interval <= 0:
        return {"ok": True, "interval": 0, "detail": "定时同步已关闭"}
    return {"ok": True, "interval": body.interval, "detail": f"定时同步已设为每 {body.interval} 分钟"}
