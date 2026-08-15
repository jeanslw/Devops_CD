"""Harbor API 客户端 — v1/v2 自动探测 + artifacts/scan/delete 操作"""

import logging
import urllib.parse

import requests

from backend.config import settings

logger = logging.getLogger("registry")


class HarborUnavailableError(Exception):
    """Harbor 服务不可达异常"""
    pass


class HarborClient:
    """Harbor API 客户端，自动探测 v1/v2 版本"""

    def __init__(self):
        raw = settings.harbor_registry.strip().rstrip("/")
        self._user = settings.harbor_user
        self._password = settings.harbor_password
        self._version = None  # "v1" | "v2"
        self._base = None

        if raw.startswith("http://") or raw.startswith("https://"):
            # 用户已指定 scheme，直接探测
            candidates = [raw]
        else:
            # 未指定 scheme，先 https 后 http
            candidates = [f"https://{raw}", f"http://{raw}"]

        for base_url in candidates:
            version = self._probe_version(base_url)
            if version:
                self._base = base_url
                self._version = version
                break

        if not self._version:
            raise RuntimeError(f"无法探测 Harbor API 版本: {candidates}")

        logger.info(f"Harbor API 版本: {self._version} ({self._base})")

    def _probe_version(self, base: str) -> str | None:
        """探测 Harbor API 版本，返回 "v1" | "v2" | None"""
        try:
            r = requests.get(f"{base}/api/v2.0/ping", auth=(self._user, self._password), timeout=8)
            if r.ok:
                return "v2"
        except Exception:
            pass
        try:
            r = requests.get(f"{base}/api/ping", auth=(self._user, self._password), timeout=8)
            if r.ok:
                return "v1"
        except Exception:
            pass
        try:
            r = requests.get(f"{base}/api/v2.0/projects?page_size=1", auth=(self._user, self._password), timeout=8)
            if r.status_code < 500:
                return "v2"
        except Exception:
            pass
        try:
            r = requests.get(f"{base}/api/projects?page_size=1", auth=(self._user, self._password), timeout=8)
            if r.status_code < 500:
                return "v1"
        except Exception:
            pass
        return None

    @property
    def version(self) -> str:
        return self._version or "v2"

    @property
    def base_url(self) -> str:
        return self._base or ""

    def _get_raw(self, path: str) -> requests.Response:
        """返回原始响应对象（用于需要读取 headers 的分页场景）"""
        try:
            r = requests.get(f"{self._base}{path}", auth=(self._user, self._password), timeout=20, verify=False)
        except requests.ConnectionError as e:
            raise HarborUnavailableError(f"Harbor 连接失败：{e}") from e
        except requests.Timeout as e:
            raise HarborUnavailableError(f"Harbor 请求超时：{e}") from e
        return r

    def _get(self, path: str) -> dict | list:
        r = self._get_raw(path)
        if r.status_code == 404:
            return [] if path.endswith("s") else {}
        r.raise_for_status()
        return r.json()

    def _post(self, path: str) -> bool:
        try:
            r = requests.post(f"{self._base}{path}", auth=(self._user, self._password), timeout=20, verify=False)
        except requests.ConnectionError as e:
            raise HarborUnavailableError(f"Harbor 连接失败：{e}") from e
        except requests.Timeout as e:
            raise HarborUnavailableError(f"Harbor 请求超时：{e}") from e
        r.raise_for_status()
        return True

    def _delete(self, path: str):
        try:
            r = requests.delete(f"{self._base}{path}", auth=(self._user, self._password), timeout=15, verify=False)
        except requests.ConnectionError as e:
            raise HarborUnavailableError(f"Harbor 连接失败：{e}") from e
        except requests.Timeout as e:
            raise HarborUnavailableError(f"Harbor 请求超时：{e}") from e
        r.raise_for_status()

    # ── 仓库路径拆分 ──

    @staticmethod
    def _split_repo(repo_full: str) -> tuple[str, str]:
        """将 'project/repo/sub' 拆分为 (project, repo/sub)"""
        if "/" in repo_full:
            project, repo = repo_full.split("/", 1)
            return project, repo
        return repo_full, repo_full

    # ── 仓库(tags)列表 ──

    def list_artifacts(self, repo_full: str) -> list[dict]:
        """获取仓库下所有 artifacts/tags（统一返回格式）"""
        if self._version == "v2":
            return self._list_artifacts_v2(repo_full)
        else:
            return self._list_artifacts_v1(repo_full)

    def _list_artifacts_v2(self, repo_full: str) -> list[dict]:
        """v2: /api/v2.0/projects/{project}/repositories/{repo}/artifacts（分页采集全部 tags）"""
        project, repo = self._split_repo(repo_full)
        enc_proj = urllib.parse.quote(project, safe="")
        enc_repo = urllib.parse.quote(repo, safe="")
        base_path = f"/api/v2.0/projects/{enc_proj}/repositories/{enc_repo}/artifacts"

        all_items = []
        page = 1
        page_size = 100
        while True:
            r = self._get_raw(f"{base_path}?page={page}&page_size={page_size}&with_scan_overview=true")
            if r.status_code == 404:
                return []
            r.raise_for_status()
            page_items = r.json() if isinstance(r.json(), list) else []
            all_items.extend(page_items)

            total = int(r.headers.get("X-Total-Count", 0))
            # 最后一页 或 已收齐
            if len(page_items) < page_size or len(all_items) >= total:
                break
            page += 1

        result = []
        for art in all_items:
            # Harbor 不同版本的 scan_overview key 不同，动态取第一个
            scan_overview = art.get("scan_overview") or {}
            scan = {}
            if scan_overview:
                # 尝试常见 key 模式
                for key in scan_overview:
                    if "vulnerability.report" in key or "scanner.adapter" in key:
                        scan = scan_overview[key] or {}
                        break
                # fallback：取第一个 dict 值
                if not scan:
                    scan = next((v for v in scan_overview.values() if isinstance(v, dict)), {})
            summary = scan.get("summary") or {}
            # Harbor severity key 可能是大写或嵌套在第二层 summary 中
            nested = summary.get("summary") or {}
            tags = art.get("tags") or []
            for t in tags:
                result.append({
                    "tag": t.get("name", ""),
                    "digest": art.get("digest", "")[:71],
                    "size_bytes": art.get("size", 0),
                    "size_mb": round(art.get("size", 0) / 1048576, 2),
                    "push_time": t.get("push_time") or art.get("push_time", ""),
                    "pull_time": art.get("pull_time", ""),
                    "scan_status": scan.get("scan_status", ""),
                    "scan_severity": scan.get("severity", ""),
                    "vuln_critical": summary.get("critical", summary.get("Critical", nested.get("Critical", 0))),
                    "vuln_high": summary.get("high", summary.get("High", nested.get("High", 0))),
                    "vuln_medium": summary.get("medium", summary.get("Medium", nested.get("Medium", 0))),
                    "vuln_low": summary.get("low", summary.get("Low", nested.get("Low", 0))),
                    "vuln_fixable": summary.get("fixable", 0),
                })
        return result

    def _list_artifacts_v1(self, repo_full: str) -> list[dict]:
        """v1: /api/repositories/{repo_name}/tags（分页采集全部 tags）"""
        encoded = urllib.parse.quote(repo_full, safe="")
        base_path = f"/api/repositories/{encoded}/tags"

        all_items = []
        page = 1
        page_size = 100
        while True:
            r = self._get_raw(f"{base_path}?page={page}&page_size={page_size}")
            if r.status_code == 404:
                return []
            r.raise_for_status()
            page_items = r.json() if isinstance(r.json(), list) else []
            all_items.extend(page_items)

            total = int(r.headers.get("X-Total-Count", 0))
            # 最后一页 或 已收齐
            if len(page_items) < page_size or (total > 0 and len(all_items) >= total):
                break
            page += 1

        result = []
        for t in all_items:
            scan_ov = t.get("scan_overview") or {}
            scan_status = scan_ov.get("scan_status", "")
            severity_map = {1: "None", 2: "Unknown", 3: "Low", 4: "Medium", 5: "High", 6: "Critical"}
            # v1 scan_overview.components.summary: [{"severity":3,"count":5}, ...]
            summary_list = (scan_ov.get("components") or {}).get("summary") or []
            vulns = {}
            for s in summary_list:
                sev_name = severity_map.get(s.get("severity", 0), "Unknown")
                vulns[sev_name.lower()] = s.get("count", 0)
            result.append({
                "tag": t.get("name", ""),
                "digest": (t.get("digest") or "")[:71],
                "size_bytes": t.get("size", 0),
                "size_mb": round(t.get("size", 0) / 1048576, 2),
                "push_time": t.get("created", ""),
                "pull_time": "",
                "scan_status": scan_status,
                "scan_severity": severity_map.get(scan_ov.get("severity", 1), "None"),
                "vuln_critical": vulns.get("critical", 0),
                "vuln_high": vulns.get("high", 0),
                "vuln_medium": vulns.get("medium", 0),
                "vuln_low": vulns.get("low", 0),
                "vuln_fixable": scan_ov.get("fixable", 0),
            })
        return result

    def delete_artifact(self, repo_full: str, tag: str, digest: str = "") -> bool:
        """删除 artifact/tag，返回是否成功"""
        if self._version == "v2":
            return self._delete_artifact_v2(repo_full, tag, digest)
        else:
            return self._delete_artifact_v1(repo_full, tag)

    def _delete_artifact_v2(self, repo_full: str, tag: str, digest: str) -> bool:
        """v2 按 digest 删除（带 fallback 按 tag:tag_name）"""
        project, repo = self._split_repo(repo_full)
        enc_proj = urllib.parse.quote(project, safe="")
        enc_repo = urllib.parse.quote(repo, safe="")
        try:
            if digest:
                # 按 reference=digest 删除
                encoded_digest = urllib.parse.quote(digest, safe="")
                self._delete(f"/api/v2.0/projects/{enc_proj}/repositories/{enc_repo}/artifacts/{encoded_digest}")
                return True
            else:
                # 按 tag 名删除
                encoded_tag = urllib.parse.quote(tag, safe="")
                self._delete(f"/api/v2.0/projects/{enc_proj}/repositories/{enc_repo}/artifacts/{encoded_tag}")
                return True
        except Exception as e:
            logger.warning(f"v2 删除失败: {e}")
            return False

    def _delete_artifact_v1(self, repo_full: str, tag: str) -> bool:
        """v1 按 tag 名删除"""
        encoded = urllib.parse.quote(repo_full, safe="")
        try:
            self._delete(f"/api/repositories/{encoded}/tags/{urllib.parse.quote(tag, safe='')}")
            return True
        except Exception as e:
            logger.warning(f"v1 删除失败: {e}")
            return False

    def get_scan_report(self, repo_full: str, tag: str) -> dict | None:
        """获取扫描报告（v1/v2 统一）"""
        if self._version == "v2":
            return self._get_scan_v2(repo_full, tag)
        else:
            return self._get_scan_v1(repo_full, tag)

    def _get_scan_v2(self, repo_full: str, tag: str) -> dict | None:
        """v2: 按 tag 查找 artifact，返回 scan_overview 或 additions/vulnerabilities"""
        project, repo = self._split_repo(repo_full)
        enc_proj = urllib.parse.quote(project, safe="")
        enc_repo = urllib.parse.quote(repo, safe="")
        try:
            # 直接获取指定 artifact（带 scan_overview）
            artifacts = self._get(
                f"/api/v2.0/projects/{enc_proj}/repositories/{enc_repo}/artifacts?"
                f"q=tags%3D{urllib.parse.quote(tag, safe='')}&page_size=1&with_scan_overview=true"
            )
            items = artifacts if isinstance(artifacts, list) else []
            if not items:
                return None
            art = items[0]

            # 1) 优先取 scan_overview（包含 summary 和 severity）
            scan_overview = art.get("scan_overview") or {}
            scan = {}
            if scan_overview:
                for key in scan_overview:
                    if "vulnerability.report" in key or "scanner.adapter" in key:
                        scan = scan_overview[key] or {}
                        break
                if not scan:
                    scan = next((v for v in scan_overview.values() if isinstance(v, dict)), {})

            summary = scan.get("summary") or {}
            digest = art.get("digest", "")
            # 构造 Harbor Web UI 链接（v2 格式）
            harbor_url = (
                f"{self._base}/harbor/projects/{urllib.parse.quote(project, safe='')}"
                f"/repositories/{urllib.parse.quote(repo, safe='')}"
                f"/artifacts/{urllib.parse.quote(digest, safe='')}?tab=vulnerabilities"
            )
            # 构造和前端兼容的报告格式
            report = {
                "scan_status": scan.get("scan_status", ""),
                "severity": scan.get("severity", ""),
                "summary": summary,
                "harbor_url": harbor_url,
                "digest": digest,
            }
            # 如果有 vulnerabilities 列表也带上
            if "vulnerabilities" in scan:
                report["vulnerabilities"] = scan["vulnerabilities"]

            # 2) 同时尝试 /additions/vulnerabilities 获取详细列表
            try:
                encoded_tag = urllib.parse.quote(tag, safe="")
                raw = self._get(
                    f"/api/v2.0/projects/{enc_proj}/repositories/{enc_repo}"
                    f"/artifacts/{encoded_tag}/additions/vulnerabilities"
                )
                # Harbor v2 把漏洞数据包在 MIME type key 里，需要解出来（参考 PHP 实现）
                if isinstance(raw, dict):
                    for key, value in raw.items():
                        if "vulnerability" in key and isinstance(value, dict):
                            value["harbor_url"] = harbor_url
                            value["digest"] = digest
                            return value
            except Exception:
                pass

            return report if (summary or scan.get("scan_status")) else None
        except Exception as e:
            logger.warning(f"v2 扫描报告: {e}")
        return None

    def _get_scan_v1(self, repo_full: str, tag: str) -> dict | None:
        """v1: /api/repositories/{repo}/tags/{tag}/scan"""
        encoded = urllib.parse.quote(repo_full, safe="")
        try:
            raw = self._get(f"/api/repositories/{encoded}/tags/{urllib.parse.quote(tag, safe='')}/scan")
            if isinstance(raw, dict):
                return raw
        except Exception as e:
            logger.warning(f"v1 扫描报告: {e}")
        return None
