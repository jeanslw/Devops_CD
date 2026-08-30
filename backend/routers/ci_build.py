"""CI 构建管理路由 — 代理 CI API，CD 前端统一入口（对照 CI OpenAPI 实现）"""

import logging
import traceback

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse

from backend.auth import require_perm
from backend.exceptions import ServiceUnavailableError
from backend.models import BuildTriggerRequest
from backend.services.ci_client import CiClientError, get_ci_client

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/ci", tags=["ci-build"])


def _client():
    """获取 CI 客户端，未配置时返回明确错误"""
    try:
        return get_ci_client()
    except CiClientError as e:
        logger.error("CI client unavailable", exc_info=e)
        raise ServiceUnavailableError("CI 服务不可用，请联系管理员", error_key="errors.ci_service_unavailable") from e


# ── 项目列表 ──
@router.get("/projects")
def list_projects(_user: str = Depends(require_perm("cd.build-manage"))):
    """获取 CI 项目列表 → CI GET /api/build/jobs/list"""
    try:
        return _client().list_projects()
    except CiClientError as e:
        logger.error("CI service unavailable", exc_info=e)
        raise ServiceUnavailableError("CI 服务不可用，请联系管理员", error_key="errors.ci_service_unavailable") from e


# ── 构建历史 ──
@router.get("/projects/{project:path}/builds")
def get_builds(project: str, _user: str = Depends(require_perm("cd.build-manage"))):
    """获取项目构建历史 → CI GET /api/build/{path}/pipelines。

    custom_push 项目也统一走该接口：Glue CustomPushBuildProvider::getPipelines
    直读 ci_custom_builds（含 log_url），CD 不再直读该表。
    """
    try:
        return _client().get_builds(project)
    except CiClientError as e:
        logger.error("CI service unavailable", exc_info=e)
        raise ServiceUnavailableError("CI 服务不可用，请联系管理员", error_key="errors.ci_service_unavailable") from e
    except Exception as e:
        tb = traceback.format_exc()
        print(f"[ci_build get_builds] {tb}", flush=True)
        raise ServiceUnavailableError(f"{type(e).__name__}: {e}", error_key="errors.ci_service_unavailable") from e


# ── 触发构建 ──
@router.post("/projects/{project:path}/build")
def trigger_build(project: str, req: BuildTriggerRequest, _user: dict = Depends(require_perm("ci.trigger"))):
    """触发一次构建 → CI POST /api/build/{path}/trigger。
    ref 仅 GitLab CI 模式必填；Jenkins 模式可省略。"""
    try:
        ref = req.ref or ""
        variables = req.variables or None
        return _client().trigger_build(project, ref, variables)
    except CiClientError as e:
        raise ServiceUnavailableError(f"CI 触发失败: {e}", error_key="errors.ci_trigger_failed") from e


# ── 构建日志 ──
@router.get("/projects/{project:path}/builds/{id}/log")
def get_build_log(project: str, id: str, _user: str = Depends(require_perm("cd.build-manage"))):
    """获取构建日志 → CI GET /api/build/{path}/logs/{id}"""
    try:
        log = _client().get_build_log(project, id)
        return PlainTextResponse(log)
    except CiClientError as e:
        raise ServiceUnavailableError(f"CI 日志获取失败: {e}", error_key="errors.ci_log_failed") from e


# ── 构建变量 ──
@router.get("/projects/{project:path}/variables")
def get_variables(project: str, _user: str = Depends(require_perm("cd.build-manage"))):
    """获取项目构建变量 → CI GET /api/build/{path}/variables"""
    try:
        return _client().get_variables(project)
    except CiClientError as e:
        logger.error("CI service unavailable", exc_info=e)
        raise ServiceUnavailableError("CI 服务不可用，请联系管理员", error_key="errors.ci_service_unavailable") from e


# ── 分支列表 ──
@router.get("/projects/{project:path}/branches")
def get_branches(project: str, _user: str = Depends(require_perm("cd.build-manage"))):
    """获取项目分支列表 → CI GET /api/build/{path}/branches（返回纯字符串数组）"""
    try:
        return _client().get_branches(project)
    except CiClientError as e:
        logger.error("CI service unavailable", exc_info=e)
        raise ServiceUnavailableError("CI 服务不可用，请联系管理员", error_key="errors.ci_service_unavailable") from e


# ── 重试 Pipeline（仅 GitLab CI）──
@router.post("/projects/{project:path}/builds/{id}/retry")
def retry_pipeline(project: str, id: str, _user: dict = Depends(require_perm("ci.trigger"))):
    """重试 Pipeline → CI POST /api/build/{path}/pipelines/{id}/retry"""
    try:
        return _client().retry_pipeline(project, id)
    except CiClientError as e:
        raise ServiceUnavailableError(f"CI 重试失败: {e}", error_key="errors.ci_retry_failed") from e


# ── 取消 Pipeline（仅 GitLab CI）──
@router.post("/projects/{project:path}/builds/{id}/cancel")
def cancel_pipeline(project: str, id: str, _user: dict = Depends(require_perm("ci.trigger"))):
    """取消 Pipeline → CI POST /api/build/{path}/pipelines/{id}/cancel"""
    try:
        return _client().cancel_pipeline(project, id)
    except CiClientError as e:
        raise ServiceUnavailableError(f"CI 取消失败: {e}", error_key="errors.ci_cancel_failed") from e


# ── 健康检查 ──
@router.get("/health")
def ci_health(_user: str = Depends(require_perm("cd.build-manage"))):
    """检查 CI 服务连通性"""
    try:
        _client().list_projects()
        return {"status": "ok", "message": "CI 服务连接正常"}
    except CiClientError as e:
        logger.error("CI health check failed", exc_info=e)
        return {"status": "error", "message": "CI 服务连接失败，请联系管理员"}
