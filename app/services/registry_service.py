"""镜像制品库服务 — Harbor v1/v2 兼容 + 本地数据库缓存 + 定时同步"""

import urllib.parse
import threading
import time
import logging
from datetime import datetime, timezone

import requests

from app.config import settings
from app.database import Database

logger = logging.getLogger("registry")


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

    def _get(self, path: str) -> dict | list:
        r = requests.get(f"{self._base}{path}", auth=(self._user, self._password), timeout=20, verify=False)
        if r.status_code == 404:
            return [] if path.endswith("s") else {}
        r.raise_for_status()
        return r.json()

    def _post(self, path: str) -> bool:
        r = requests.post(f"{self._base}{path}", auth=(self._user, self._password), timeout=20, verify=False)
        r.raise_for_status()
        return True

    def _delete(self, path: str):
        r = requests.delete(f"{self._base}{path}", auth=(self._user, self._password), timeout=15, verify=False)
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
        """v2: /api/v2.0/projects/{project}/repositories/{repo}/artifacts"""
        project, repo = self._split_repo(repo_full)
        enc_proj = urllib.parse.quote(project, safe="")
        enc_repo = urllib.parse.quote(repo, safe="")
        raw = self._get(f"/api/v2.0/projects/{enc_proj}/repositories/{enc_repo}/artifacts?page_size=100&with_scan_overview=true")
        items = raw if isinstance(raw, list) else []
        result = []
        for art in items:
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
        """v1: /api/repositories/{repo_name}/tags"""
        encoded = urllib.parse.quote(repo_full, safe="")
        raw = self._get(f"/api/repositories/{encoded}/tags")
        items = raw if isinstance(raw, list) else []
        result = []
        for t in items:
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
            # repo_full="mycode/devops-glue" → project="mycode", repo="devops-glue"
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


class RegistryService:
    """镜像仓库服务：定时同步 + CRUD + 扫描报告
    注意：建表由 database.py (SQLite) 和 database/init_mysql.sql (MySQL) 统一管理。
    """

    _instance = None
    _lock = threading.Lock()

    def __init__(self, db: Database):
        self._db = db
        self._harbor_client = None

    @property
    def _harbor(self):
        """延迟初始化 HarborClient，避免纯数据库查询时触发 API 探测"""
        if self._harbor_client is None:
            self._harbor_client = HarborClient()
        return self._harbor_client

    # ── 工具 ──

    @staticmethod
    def _to_mysql_dt(iso_str: str) -> str:
        """ISO 8601 → MySQL DATETIME: '2026-07-17T07:17:04.737Z' → '2026-07-17 07:17:04'"""
        if not iso_str:
            return ""
        s = iso_str.replace("T", " ").replace("Z", "")
        if "." in s:
            s = s[:s.index(".")]
        return s

    # ── 从 CI 映射获取待同步仓库 ──

    def _get_ci_repos(self, conn) -> list[dict]:
        """从 ci_job_git_map 获取所有已配置 Harbor 仓库的项目"""
        rows = conn.execute(
            "SELECT job_name, harbor_repository FROM ci_job_git_map "
            "WHERE status='active' AND harbor_repository IS NOT NULL AND harbor_repository!=''"
        ).fetchall()
        return [{"project": r["job_name"], "repo": r["harbor_repository"]} for r in rows]

    # ── 同步逻辑 ──

    def sync_repo(self, conn, project: str, repo: str) -> int:
        """同步单个仓库的 artifacts 到数据库，返回新增/更新的数量"""
        # 确保 repo 记录存在
        rows = conn.execute(
            "SELECT id FROM cd_registry_repositories WHERE project_name=? AND repo_name=?",
            (project, repo)
        ).fetchall()
        if rows:
            repo_id = rows[0]["id"]
        else:
            cur = conn.execute(
                "INSERT INTO cd_registry_repositories(project_name,repo_name) VALUES (?,?)",
                (project, repo)
            )
            repo_id = cur.lastrowid
            conn.commit()

        # 从 Harbor 拉取
        try:
            raw = self._harbor.list_artifacts(repo)
            logger.info(f"Harbor 返回 {len(raw)} 条 artifacts: {repo}")
        except Exception as e:
            logger.error(f"同步 {repo} 失败: {e}")
            return 0

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        count = 0
        for art in raw:
            if not art.get("tag"):
                continue
            push_time = self._to_mysql_dt(art.get("push_time", ""))
            pull_time = self._to_mysql_dt(art.get("pull_time", ""))
            # upsert
            rows = conn.execute(
                "SELECT id FROM cd_registry_artifacts WHERE repo_id=? AND tag=? AND digest=?",
                (repo_id, art["tag"], art["digest"])
            ).fetchall()
            if rows:
                conn.execute(
                    """UPDATE cd_registry_artifacts SET size_bytes=?,push_time=?,pull_time=?,
                    scan_status=?,scan_severity=?,vuln_critical=?,vuln_high=?,vuln_medium=?,
                    vuln_low=?,vuln_fixable=?,last_sync=?
                    WHERE id=?""",
                    (art["size_bytes"], push_time, pull_time,
                     art["scan_status"], art["scan_severity"],
                     art["vuln_critical"], art["vuln_high"], art["vuln_medium"],
                     art["vuln_low"], art["vuln_fixable"], now, rows[0]["id"])
                )
            else:
                conn.execute(
                    """INSERT INTO cd_registry_artifacts
                    (repo_id,tag,digest,size_bytes,push_time,pull_time,
                     scan_status,scan_severity,vuln_critical,vuln_high,vuln_medium,vuln_low,vuln_fixable,last_sync)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (repo_id, art["tag"], art["digest"], art["size_bytes"],
                     push_time, pull_time,
                     art["scan_status"], art["scan_severity"],
                     art["vuln_critical"], art["vuln_high"], art["vuln_medium"],
                     art["vuln_low"], art["vuln_fixable"], now)
                )
            count += 1
        conn.commit()
        return count

    def sync_all(self) -> dict:
        """全量同步所有 CI 映射的仓库"""
        conn = self._db.conn()
        try:
            ci_repos = self._get_ci_repos(conn)
            mapped = {(r["project"], r["repo"]) for r in ci_repos}

            # 清理已不在 ci_job_git_map 中的历史脏数据
            all_repos = conn.execute(
                "SELECT id, project_name, repo_name FROM cd_registry_repositories"
            ).fetchall()
            for r in all_repos:
                if (r["project_name"], r["repo_name"]) not in mapped:
                    conn.execute(
                        "DELETE FROM cd_registry_artifacts WHERE repo_id=?",
                        (r["id"],)
                    )
                    conn.execute(
                        "DELETE FROM cd_registry_repositories WHERE id=?",
                        (r["id"],)
                    )

            total = 0
            errors = []
            for cr in ci_repos:
                try:
                    n = self.sync_repo(conn, cr["project"], cr["repo"])
                    total += n
                    logger.info(f"同步 {cr['repo']}: {n} 条")
                except Exception as e:
                    errors.append(f"{cr['repo']}: {e}")
                    logger.error(f"同步 {cr['repo']}: {e}")
            conn.commit()
            return {"ok": True, "total": total, "repos": len(ci_repos), "errors": errors}
        finally:
            conn.close()

    def sync_for_project(self, project: str) -> dict:
        """增量同步指定项目/仓库的仓库（支持按 job_name 或 harbor_repository 匹配）"""
        conn = self._db.conn()
        try:
            ci_repos = self._get_ci_repos(conn)
            matched = [
                cr for cr in ci_repos
                if cr["project"] == project or cr["repo"] == project or cr["repo"].startswith(project + "/")
            ]
            if not matched:
                return {"ok": False, "error": "未找到该项目仓库映射"}
            total = 0
            for cr in matched:
                n = self.sync_repo(conn, cr["project"], cr["repo"])
                total += n
            return {"ok": True, "total": total}
        finally:
            conn.close()

    # ── 查询 ──

    def get_repositories(self) -> dict:
        """获取仓库列表（来自数据库），返回 {"repositories": [...], "last_sync": "..."} """
        conn = self._db.conn()
        try:
            ci_repos = self._get_ci_repos(conn)
            mapped = {(r["project"], r["repo"]) for r in ci_repos}

            # 全局最后同步时间
            sync_row = conn.execute(
                "SELECT MAX(last_sync) AS last_sync FROM cd_registry_artifacts"
            ).fetchone()
            last_sync = sync_row["last_sync"] or ""

            if not mapped:
                return {"repositories": [], "last_sync": last_sync}

            rows = conn.execute(
                """SELECT r.id, r.project_name, r.repo_name,
                    COUNT(a.id) AS tag_count,
                    MAX(a.push_time) AS latest_push
                FROM cd_registry_repositories r
                LEFT JOIN cd_registry_artifacts a ON a.repo_id=r.id
                GROUP BY r.id
                ORDER BY r.project_name, r.repo_name"""
            ).fetchall()

            result = []
            for row in rows:
                key = (row["project_name"], row["repo_name"])
                if key not in mapped:
                    continue
                result.append({
                    "id": row["id"],
                    "project": row["project_name"],
                    "repo": row["repo_name"],
                    "tag_count": row["tag_count"] or 0,
                    "latest_push": row["latest_push"] or "",
                })
            return {"repositories": result, "last_sync": last_sync}
        finally:
            conn.close()

    def get_artifacts(self, repo_id: int) -> list[dict]:
        """获取指定仓库的 artifact/tag 列表"""
        conn = self._db.conn()
        try:
            rows = conn.execute(
                """SELECT a.*, r.project_name, r.repo_name
                FROM cd_registry_artifacts a
                JOIN cd_registry_repositories r ON r.id=a.repo_id
                WHERE a.repo_id=?
                ORDER BY a.push_time DESC""",
                (repo_id,)
            ).fetchall()
            return [
                {
                    "id": row["id"],
                    "tag": row["tag"],
                    "digest": row["digest"],
                    "size_bytes": row["size_bytes"],
                    "size_mb": round((row["size_bytes"] or 0) / 1048576, 2),
                    "push_time": row["push_time"],
                    "pull_time": row["pull_time"],
                    "scan_status": row["scan_status"],
                    "scan_severity": row["scan_severity"],
                    "vuln_critical": row["vuln_critical"],
                    "vuln_high": row["vuln_high"],
                    "vuln_medium": row["vuln_medium"],
                    "vuln_low": row["vuln_low"],
                    "vuln_fixable": row["vuln_fixable"],
                    "project": row["project_name"],
                    "repo": row["repo_name"],
                }
                for row in rows
            ]
        finally:
            conn.close()

    @staticmethod
    def _norm_summary(raw_summary: dict) -> dict:
        """规范化 Harbor summary：支持大小写 key 和嵌套 summary"""
        if not raw_summary:
            return {}
        nested = raw_summary.get("summary") or {}
        c = raw_summary.get("critical", raw_summary.get("Critical", nested.get("Critical", 0)))
        h = raw_summary.get("high", raw_summary.get("High", nested.get("High", 0)))
        m = raw_summary.get("medium", raw_summary.get("Medium", nested.get("Medium", 0)))
        l = raw_summary.get("low", raw_summary.get("Low", nested.get("Low", 0)))
        total = raw_summary.get("total", c + h + m + l)
        fixable = raw_summary.get("fixable", 0)
        return {"critical": c, "high": h, "medium": m, "low": l, "total": total, "fixable": fixable}

    def get_scan_report(self, repo_id: int, tag: str) -> dict | None:
        """先读数据库缓存，再实时请求 Harbor 获取详情（优先 Harbor 最新数据）"""
        conn = self._db.conn()
        try:
            row = conn.execute(
                """SELECT a.scan_status, a.scan_severity, a.vuln_critical, a.vuln_high,
                          a.vuln_medium, a.vuln_low, a.vuln_fixable, a.digest,
                          r.repo_name, r.project_name
                   FROM cd_registry_artifacts a
                   JOIN cd_registry_repositories r ON a.repo_id = r.id
                   WHERE a.repo_id=? AND a.tag=?""",
                (repo_id, tag),
            ).fetchone()
            if not row:
                return {"error": "Tag 不存在"}

            c, h, m, l = (row["vuln_critical"] or 0), (row["vuln_high"] or 0), (row["vuln_medium"] or 0), (row["vuln_low"] or 0)
            digest = row["digest"] or ""
            # repo_name="mycode/devops-glue" → harbor_project="mycode", harbor_repo="devops-glue"
            repo_name = row["repo_name"] or ""
            if "/" in repo_name:
                harbor_project, harbor_repo = repo_name.split("/", 1)
            else:
                harbor_project = harbor_repo = repo_name
            harbor_url = (
                f"{self._harbor.base_url}/harbor/projects/"
                f"{urllib.parse.quote(harbor_project, safe='')}"
                f"/repositories/{urllib.parse.quote(harbor_repo, safe='')}"
                f"/artifacts/{urllib.parse.quote(digest, safe='')}?tab=vulnerabilities"
            )
            db_summary = {
                "critical": c,
                "high": h,
                "medium": m,
                "low": l,
                "total": c + h + m + l,
                "fixable": row["vuln_fixable"] or 0,
            }

            # 实时请求 Harbor 获取最新详情（不依赖数据库数量，解决旧缓存数据为 0 的问题）
            try:
                live = self._harbor.get_scan_report(row["repo_name"], tag)
                if live:
                    live_summary = self._norm_summary(live.get("summary") or {})
                    # 优先用 Harbor 的 summary；如果 Harbor summary 全 0，fallback 数据库
                    has_live_data = live.get("vulnerabilities") or live_summary["total"] > 0
                    if has_live_data:
                        live["summary"] = live_summary if live_summary["total"] > 0 else db_summary
                        live["harbor_url"] = harbor_url
                        live["scan_status"] = row["scan_status"] or live.get("scan_status", "")
                        live["severity"] = row["scan_severity"] or live.get("severity", "")
                        return live
            except Exception as e:
                logger.warning(f"实时获取扫描详情失败，回退到汇总: {e}")

            return {
                "scan_status": row["scan_status"] or "",
                "severity": row["scan_severity"] or "",
                "summary": db_summary,
                "harbor_url": harbor_url,
                "digest": digest,
            }
        finally:
            conn.close()

    def trigger_scan(self, repo_id: int, tag: str) -> dict:
        """触发 Harbor 重新扫描该 artifact（v2 用 tag，参照 PHP 实现）"""
        conn = self._db.conn()
        try:
            row = conn.execute(
                """SELECT r.repo_name, a.digest
                   FROM cd_registry_artifacts a
                   JOIN cd_registry_repositories r ON a.repo_id = r.id
                   WHERE a.repo_id=? AND a.tag=?""",
                (repo_id, tag),
            ).fetchone()
            if not row:
                return {"ok": False, "error": "Tag 不存在"}

            if self._harbor.version == "v2":
                project, repo = HarborClient._split_repo(row["repo_name"])
                enc_proj = urllib.parse.quote(project, safe="")
                enc_repo = urllib.parse.quote(repo, safe="")
                encoded_tag = urllib.parse.quote(tag, safe="")
                self._harbor._post(
                    f"/api/v2.0/projects/{enc_proj}/repositories/{enc_repo}/artifacts/{encoded_tag}/scan"
                )
            else:
                encoded = urllib.parse.quote(row["repo_name"], safe="")
                self._harbor._post(f"/api/repositories/{encoded}/tags/{urllib.parse.quote(tag, safe='')}/scan")
            return {"ok": True, "detail": "扫描已触发，请等待 Harbor 完成后再查看报告"}
        except Exception as e:
            logger.warning(f"触发扫描失败: {e}")
            return {"ok": False, "error": str(e)}
        finally:
            conn.close()

    # ── 删除 ──

    def delete_artifact(self, repo_id: int, tag: str) -> dict:
        """删除 artifact（Harbor v1/v2 通用 + 更新数据库）"""
        conn = self._db.conn()
        try:
            # 获取仓库信息
            repo_row = conn.execute(
                "SELECT project_name, repo_name FROM cd_registry_repositories WHERE id=?",
                (repo_id,)
            ).fetchone()
            if not repo_row:
                return {"ok": False, "error": "仓库不存在"}

            artifact_row = conn.execute(
                "SELECT id, digest FROM cd_registry_artifacts WHERE repo_id=? AND tag=?",
                (repo_id, tag)
            ).fetchone()
            if not artifact_row:
                return {"ok": False, "error": "Tag 不存在"}

            # 安全校验 1：是否在线运行
            running = self._check_running(conn, repo_row["project_name"], tag)
            if running:
                return {"ok": False, "error": f"该 Tag 正在线上运行（{running}），请先回滚到其他版本"}

            # 安全校验 2：最近 3 个 Tag 保护
            recent = conn.execute(
                "SELECT tag FROM cd_registry_artifacts WHERE repo_id=? ORDER BY push_time DESC LIMIT 3",
                (repo_id,)
            ).fetchall()
            protected = {r["tag"] for r in recent}
            if tag in protected:
                return {"ok": False, "error": "最近 3 个 Tag 受保护，禁止删除"}

            # 调 Harbor API 删除
            success = self._harbor.delete_artifact(
                repo_row["repo_name"], tag, artifact_row["digest"]
            )
            if not success:
                return {"ok": False, "error": "Harbor 删除失败，请检查权限或配置"}

            # 更新数据库
            conn.execute("DELETE FROM cd_registry_artifacts WHERE id=?", (artifact_row["id"],))
            conn.commit()
            return {"ok": True}
        finally:
            conn.close()

    def _check_running(self, conn, project: str, tag: str) -> str | None:
        """检查 Tag 是否在线上运行"""
        rows = conn.execute(
            "SELECT target, deploy_type, created_at FROM cd_deploy_logs "
            "WHERE project=? AND tag=? AND status='success' "
            "ORDER BY created_at DESC LIMIT 5",
            (project, tag),
        ).fetchall()
        if rows:
            targets = {f"{r['deploy_type']}@{r['target']}" for r in rows}
            return "、".join(sorted(targets)[:3])
        return None


# ── 后台定时同步 ──

_sync_thread = None
_sync_stop = threading.Event()


def _sync_worker(db_factory, interval_minutes: int):
    """后台线程：定时全量同步"""
    while not _sync_stop.is_set():
        _sync_stop.wait(interval_minutes * 60)
        if _sync_stop.is_set():
            break
        try:
            svc = RegistryService(db_factory())
            result = svc.sync_all()
            logger.info(f"定时同步完成: {result['total']} artifacts, {result['repos']} repos")
        except Exception as e:
            logger.error(f"定时同步异常: {e}")


def start_background_sync(db_factory):
    """启动后台定时同步线程"""
    interval = getattr(settings, "registry_sync_interval", 30)
    if interval <= 0:
        logger.info("定时同步已关闭 (REGISTRY_SYNC_INTERVAL=0)")
        return
    global _sync_thread, _sync_stop
    _sync_stop.clear()
    _sync_thread = threading.Thread(
        target=_sync_worker,
        args=(db_factory, interval),
        daemon=True,
        name="registry-sync",
    )
    _sync_thread.start()
    logger.info(f"定时同步已启动 (每 {interval} 分钟)")


def stop_background_sync():
    global _sync_stop
    _sync_stop.set()
