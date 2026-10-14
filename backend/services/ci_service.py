"""CI 数据查询服务 - 映射/tag/pipeline 经 CI HTTP API,不直读 ci_ 表"""

import logging
import threading
import time

from backend.database import Database
from backend.services.ci_client import CiClientError, get_ci_client

logger = logging.getLogger(__name__)

# 进程内短 TTL 缓存:Glue 的项目聚合(每项目最新 tag / pipeline)一次要几十到上百毫秒,
# 首页 / 部署页 / 审批页会在同一时间窗内反复拉取完全相同的内容.
# 缓存放在**模块级**:路由每请求都 new CiService()(见 routers/projects.py),实例级缓存
# 永远命中不了 —— 等于没有缓存(2026-10 首页变慢排查中发现:改之前每次请求都要额外等
# Glue 的 100~200ms;这跟本机"被拒连接要 2s"是两回事,别混为一谈).
_PROJECTS_TTL = 30  # 秒
_projects_lock = threading.Lock()
_projects_cache: list[dict] | None = None
_projects_cache_at: float = 0.0


def _load_projects() -> list[dict]:
    """经 Glue GET /api/build/projects 拉取活跃映射 + 每项目最新 tag(最多 30s 复用一次).

    失败(上游不可达 / 解析异常)**不写缓存**,只返回空列表:否则一次瞬时故障会让首页
    空 30s;不缓存则表示"下一个请求立刻再试".
    """
    global _projects_cache, _projects_cache_at
    now = time.time()
    with _projects_lock:
        if _projects_cache is not None and now - _projects_cache_at < _PROJECTS_TTL:
            return _projects_cache
    try:
        data = list(get_ci_client().get_projects() or [])
    except CiClientError as e:
        logger.warning("CI projects 拉取失败: %s", e)
        return []
    except Exception as e:  # noqa: BLE001 - 上游返回结构异常同样降级为空列表
        logger.warning("CI projects 解析失败: %s", e)
        return []
    with _projects_lock:
        _projects_cache = data
        _projects_cache_at = now
    return data


def invalidate_projects_cache() -> None:
    """丢弃项目列表缓存:触发 / 重试 / 取消构建后调用,让首页立刻看到新 tag 与状态."""
    global _projects_cache, _projects_cache_at
    with _projects_lock:
        _projects_cache = None
        _projects_cache_at = 0.0


class CiService:
    """从 CI HTTP API 读取项目,pipeline,tag 信息"""

    def __init__(self, db: Database | None = None):
        # db 参数保留以兼容既有调用方(deploy/k8s/approval 等仍传 db),本服务已不再直读 DB
        self._db = db

    def _projects_via_api(self) -> list[dict]:
        """经 Glue GET /api/build/projects 拉取活跃映射 + 每项目最新 tag(模块级 30s 缓存)."""
        return _load_projects()

    def _find_job(self, project: str) -> dict | None:
        """按 job_name / current_path 匹配映射条目."""
        for m in self._projects_via_api():
            if (m.get("job_name") or "") == project or (m.get("current_path") or "") == project:
                return m
        return None

    def list_projects(self) -> list[dict]:
        """列出所有活跃 CI 项目(含最新 tag)"""
        return self._projects_via_api()

    def get_pipeline_status(self, project_name: str) -> dict | None:
        """获取指定项目的 pipeline 状态(最新 tag)"""
        m = self._find_job(project_name)
        if not m:
            return None
        tag = m.get("latest_tag") or ""
        return {
            "project": m.get("job_name") or project_name,
            "latest_tag": tag,
            "pipeline": {
                "iid": m.get("latest_pipeline") or None,
                "status": "completed" if tag else "unknown",
                "created_at": m.get("tag_time") or "",
            },
        }

    def resolve_harbor_repo(self, project: str) -> str | None:
        """查项目对应的 Harbor 仓库名(映射的 harbor_repository,非 tag 记录)"""
        m = self._find_job(project)
        if m and (m.get("harbor_repository") or ""):
            return m["harbor_repository"]
        return None

    def resolve_project_key(self, project: str) -> str | None:
        """解析为 job_name 作为主标识"""
        m = self._find_job(project)
        if m:
            return m.get("job_name") or None
        return None
