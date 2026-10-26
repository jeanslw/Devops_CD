"""Harbor 提供方 - 走 Glue /api/harbor 代理端点(cd-service token)"""

import logging
import urllib.parse
from typing import Any

from backend.services.ci_client import CiClientError, get_ci_client
from backend.services.registry_provider import (
    CAPABILITIES,
    HarborUnavailableError,
    RegistryPermissionDeniedError,
    RegistryProvider,
    split_repo,
)

logger = logging.getLogger("registry")


class HarborClient(RegistryProvider):
    """Harbor 提供方:经 Glue /api/harbor/* 代理(凭据为 cd-service 服务 token,已含 registry.read)."""

    type = "harbor"
    CAPABILITIES = CAPABILITIES["harbor"]

    def __init__(self):
        self._ci = get_ci_client()
        self._base = self._ci._base

    @property
    def version(self) -> str:
        return "v2"

    @property
    def base_url(self) -> str:
        return self._base

    def _get(self, path: str) -> Any:
        """GET Glue /api/harbor/* :401/403→权限错误,404→空,其他→不可用"""
        try:
            return self._ci.get_json(path)
        except CiClientError as e:
            code = e.status_code
            if code in (401, 403):
                raise RegistryPermissionDeniedError(f"镜像仓库拒绝访问（HTTP {code}）: {path}")
            if code == 404:
                return []
            raise HarborUnavailableError(f"镜像仓库不可用：{e}") from e

    def _post(self, path: str) -> bool:
        try:
            self._ci.post_json(path)
            return True
        except CiClientError as e:
            code = e.status_code
            if code in (401, 403):
                raise RegistryPermissionDeniedError(f"镜像仓库拒绝访问（HTTP {code}）: {path}")
            raise HarborUnavailableError(f"镜像仓库不可用：{e}") from e

    def get_projects(self) -> list[str]:
        data = self._get("/api/harbor/projects")
        if not isinstance(data, list):
            return []
        return [d.get("name", d) if isinstance(d, dict) else d for d in data]

    def get_repositories(self, project: str) -> list[str]:
        enc = urllib.parse.quote(project, safe="")
        data = self._get(f"/api/harbor/{enc}/repositories")
        if not isinstance(data, list):
            return []
        return [d.get("name", d) if isinstance(d, dict) else d for d in data]

    def list_artifacts(self, repo_full: str) -> list[dict]:
        project, repo = split_repo(repo_full)
        enc_proj = urllib.parse.quote(project, safe="")
        enc_repo = urllib.parse.quote(repo, safe="")
        tags = self._get(f"/api/harbor/{enc_proj}/repositories/{enc_repo}/tags")
        if not isinstance(tags, list):
            return []
        result = []
        for tag in tags:
            # Glue tags 端点:Harbor v2 返回详情对象,v1 / 旧版 Glue 返回纯名称(详情字段留空)
            if isinstance(tag, dict):
                name = tag.get("name") or ""
                if not name:
                    continue
                size_bytes = int(tag.get("size_bytes") or 0)
                result.append(
                    {
                        "tag": name,
                        "digest": tag.get("digest") or "",
                        "size_bytes": size_bytes,
                        "size_mb": round(size_bytes / 1048576, 2),
                        "push_time": tag.get("push_time") or "",
                        "pull_time": tag.get("pull_time") or "",
                        "scan_status": tag.get("scan_status") or "",
                        "scan_severity": tag.get("scan_severity") or "",
                        "vuln_critical": int(tag.get("vuln_critical") or 0),
                        "vuln_high": int(tag.get("vuln_high") or 0),
                        "vuln_medium": int(tag.get("vuln_medium") or 0),
                        "vuln_low": int(tag.get("vuln_low") or 0),
                        "vuln_fixable": int(tag.get("vuln_fixable") or 0),
                    }
                )
                continue
            if not tag:
                continue
            result.append(
                {
                    "tag": tag,
                    "digest": "",
                    "size_bytes": 0,
                    "size_mb": 0.0,
                    "push_time": "",
                    "pull_time": "",
                    "scan_status": "",
                    "scan_severity": "",
                    "vuln_critical": 0,
                    "vuln_high": 0,
                    "vuln_medium": 0,
                    "vuln_low": 0,
                    "vuln_fixable": 0,
                }
            )
        return result

    def get_scan_report(self, repo_full: str, tag: str) -> dict | None:
        project, repo = split_repo(repo_full)
        enc_proj = urllib.parse.quote(project, safe="")
        enc_repo = urllib.parse.quote(repo, safe="")
        enc_tag = urllib.parse.quote(tag, safe="")
        try:
            data = self._get(f"/api/harbor/{enc_proj}/repositories/{enc_repo}/tags/{enc_tag}/scan-report")
        except (HarborUnavailableError, RegistryPermissionDeniedError):
            return None
        if not data:
            return None
        return data if isinstance(data, dict) else None

    def trigger_scan(self, repo_full: str, tag: str) -> bool:
        project, repo = split_repo(repo_full)
        enc_proj = urllib.parse.quote(project, safe="")
        enc_repo = urllib.parse.quote(repo, safe="")
        enc_tag = urllib.parse.quote(tag, safe="")
        return self._post(f"/api/harbor/{enc_proj}/repositories/{enc_repo}/tags/{enc_tag}/scan")
