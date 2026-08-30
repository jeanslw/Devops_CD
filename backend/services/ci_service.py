"""CI 数据查询服务 — 映射/tag/pipeline 经 CI HTTP API，不直读 ci_ 表"""

import logging
import time

from backend.database import Database
from backend.services.ci_client import CiClientError, get_ci_client

logger = logging.getLogger(__name__)


class CiService:
    """从 CI HTTP API 读取项目、pipeline、tag 信息"""

    def __init__(self, db: Database | None = None):
        # db 参数保留以兼容既有调用方（deploy/k8s/approval 等仍传 db），本服务已不再直读 DB
        self._db = db
        self._jobs_cache: list[dict] | None = None
        self._jobs_cache_at: float = 0.0

    def _projects_via_api(self) -> list[dict]:
        """经 Glue GET /api/build/projects 拉取活跃映射 + 每项目最新 tag。

        短 TTL 缓存：同一实例内多次查询只打一次 HTTP；最多 30s 复用。
        """
        now = time.time()
        if self._jobs_cache is not None and now - self._jobs_cache_at < 30:
            return self._jobs_cache
        try:
            self._jobs_cache = list(get_ci_client().get_projects() or [])
        except CiClientError as e:
            logger.warning("CI projects 拉取失败: %s", e)
            self._jobs_cache = []
        except Exception as e:
            logger.warning("CI projects 解析失败: %s", e)
            self._jobs_cache = []
        self._jobs_cache_at = now
        return self._jobs_cache

    def _find_job(self, project: str) -> dict | None:
        """按 job_name / current_path 匹配映射条目。"""
        for m in self._projects_via_api():
            if (m.get("job_name") or "") == project or (m.get("current_path") or "") == project:
                return m
        return None

    def list_projects(self) -> list[dict]:
        """列出所有活跃 CI 项目（含最新 tag）"""
        return self._projects_via_api()

    def get_pipeline_status(self, project_name: str) -> dict | None:
        """获取指定项目的 pipeline 状态（最新 tag）"""
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
        """查项目对应的 Harbor 仓库名（映射的 harbor_repository，非 tag 记录）"""
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
