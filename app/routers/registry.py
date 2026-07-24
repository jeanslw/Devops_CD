"""镜像制品库 API — 仓库/Artifact/同步/扫描/删除"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth import verify_token, get_db
from app.database import Database
from app.services.registry_service import RegistryService, HarborUnavailableError

router = APIRouter(prefix="/api/registry", tags=["registry"])


class DeleteArtifactRequest(BaseModel):
    repo_id: int
    tag: str


class SyncConfigRequest(BaseModel):
    interval: int  # 分钟，0 关闭


# ── 仓库 ──

@router.get("/repositories")
def list_repositories(
    _user: str = Depends(verify_token),
    db: Database = Depends(get_db),
):
    """列出所有仓库（从数据库缓存读取）"""
    svc = RegistryService(db)
    return svc.get_repositories()


# ── Artifact / Tag ──

@router.get("/artifacts/{repo_id}")
def list_artifacts(
    repo_id: int,
    _user: str = Depends(verify_token),
    db: Database = Depends(get_db),
):
    """列出指定仓库的所有 artifacts/tags（含扫描摘要）"""
    svc = RegistryService(db)
    return svc.get_artifacts(repo_id)


# ── 扫描报告 ──

@router.get("/scan/report/{repo_id}/{tag}")
def get_scan_report(
    repo_id: int,
    tag: str,
    _user: str = Depends(verify_token),
    db: Database = Depends(get_db),
):
    """从数据库缓存读取扫描报告（秒开）"""
    svc = RegistryService(db)
    result = svc.get_scan_report(repo_id, tag)
    if result is None:
        raise HTTPException(404, "扫描报告未找到")
    if isinstance(result, dict) and "error" in result:
        raise HTTPException(404, result["error"])
    return result


@router.post("/scan/trigger/{repo_id}/{tag}")
def trigger_scan(
    repo_id: int,
    tag: str,
    _user: str = Depends(verify_token),
    db: Database = Depends(get_db),
):
    """触发 Harbor 重新扫描该镜像"""
    try:
        svc = RegistryService(db)
        result = svc.trigger_scan(repo_id, tag)
        if not result.get("ok"):
            raise HTTPException(400, result.get("error", "触发失败"))
        return result
    except HarborUnavailableError as e:
        raise HTTPException(503, str(e))


# ── 删除 ──

@router.delete("/artifacts/{repo_id}")
def delete_artifact(
    repo_id: int,
    body: DeleteArtifactRequest,
    _user: str = Depends(verify_token),
    db: Database = Depends(get_db),
):
    """删除指定 artifact/tag（带安全校验）"""
    try:
        svc = RegistryService(db)
        result = svc.delete_artifact(repo_id, body.tag)
        if not result["ok"]:
            raise HTTPException(409, result.get("error", "删除失败"))
        return {"ok": True, "detail": f"Tag '{body.tag}' 已删除"}
    except HarborUnavailableError as e:
        raise HTTPException(503, str(e))


# ── 同步 ──

@router.post("/sync")
def trigger_sync(
    project: str = "",
    _user: str = Depends(verify_token),
    db: Database = Depends(get_db),
):
    """触发同步：留空全量同步，指定 project 增量同步"""
    try:
        svc = RegistryService(db)
        if project:
            return svc.sync_for_project(project)
        return svc.sync_all()
    except HarborUnavailableError as e:
        raise HTTPException(503, str(e))


# ── 同步配置 ──

@router.get("/config")
def get_sync_config(
    _user: str = Depends(verify_token),
    db: Database = Depends(get_db),
):
    """获取定时同步配置"""
    svc = RegistryService(db)
    interval = svc.get_sync_interval()
    return {"interval": interval}


@router.put("/config")
def update_sync_config(
    body: SyncConfigRequest,
    _user: str = Depends(verify_token),
    db: Database = Depends(get_db),
):
    """更新定时同步间隔（分钟，0=关闭）"""
    if body.interval < 0:
        raise HTTPException(400, "间隔不能为负数")
    svc = RegistryService(db)
    svc.set_sync_interval(body.interval)
    if body.interval <= 0:
        return {"ok": True, "interval": 0, "detail": "定时同步已关闭"}
    return {"ok": True, "interval": body.interval, "detail": f"定时同步已设为每 {body.interval} 分钟"}
