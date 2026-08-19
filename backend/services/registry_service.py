"""镜像制品库服务 — 数据库缓存 + 定时同步 + CRUD"""

import logging
import threading
import urllib.parse
from datetime import datetime, timezone

from backend.config import settings
from backend.database import Database
from backend.services.harbor_client import HarborClient, HarborUnavailableError

logger = logging.getLogger("registry")


class RegistryService:
    """镜像仓库服务：定时同步 + CRUD + 扫描报告
    注意：建表由 database.py (SQLite) 和 database/init_mysql.sql (MySQL) 统一管理。
    """

    _instance = None
    _lock = threading.Lock()

    def __init__(self, db: Database):
        self._db = db
        self._harbor_client = None

    # ── 配置读写 ──

    def get_config(self, key: str) -> str | None:
        """读取配置项"""
        with self._db.conn() as conn:
            try:
                row = conn.execute(
                    "SELECT value FROM cd_config WHERE key_name=?", (key,)
                ).fetchone()
                return row["value"] if row else None
            except Exception:
                return None

    def set_config(self, key: str, value: str):
        """写入配置项"""
        with self._db.conn() as conn:
            conn.execute(
                "REPLACE INTO cd_config (key_name, value) VALUES (?, ?)",
                (key, value),
            )

    def get_sync_interval(self) -> int:
        """获取同步间隔（分钟），0=关闭。DB 优先，fallback 到环境变量"""
        val = self.get_config("registry_sync_interval")
        if val is not None:
            try:
                return int(val)
            except ValueError:
                pass
        return getattr(settings, "registry_sync_interval", 30)

    def set_sync_interval(self, interval_minutes: int):
        """设置同步间隔并按需重启线程"""
        self.set_config("registry_sync_interval", str(interval_minutes))
        restart_background_sync(interval_minutes)

    @property
    def _harbor(self):
        """延迟初始化 HarborClient，避免纯数据库查询时触发 API 探测"""
        if self._harbor_client is None:
            try:
                self._harbor_client = HarborClient()
            except RuntimeError as e:
                logger.error("Harbor client initialization failed", exc_info=e)
                raise HarborUnavailableError(
                    "Harbor 镜像仓库不可达。请检查：\n"
                    "1. HARBOR_REGISTRY 地址是否正确\n"
                    "2. 网络是否连通\n"
                    "3. Harbor 服务是否正常运行\n"
                ) from e
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
        return count

    def sync_all(self) -> dict:
        """全量同步所有 CI 映射的仓库"""
        with self._db.conn() as conn:
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
                    except HarborUnavailableError:
                        raise  # 直接抛出，不要在 errors 里吞掉
                    except Exception as e:
                        errors.append(f"{cr['repo']}: {e}")
                        logger.error(f"同步 {cr['repo']}: {e}")
                return {"ok": True, "total": total, "repos": len(ci_repos), "errors": errors}
            except HarborUnavailableError:
                return {"ok": False, "error": "Harbor 镜像仓库不可达，无法同步。请检查网络连接和 Harbor 服务状态"}

    def sync_for_project(self, project: str) -> dict:
        """增量同步指定项目/仓库的仓库（支持按 job_name 或 harbor_repository 匹配）"""
        with self._db.conn() as conn:
            ci_repos = self._get_ci_repos(conn)
            matched = [
                cr for cr in ci_repos
                if cr["project"] == project or cr["repo"] == project or cr["repo"].startswith(project + "/")
            ]
            if not matched:
                return {"ok": False, "error": "未找到该项目仓库映射"}
            try:
                total = 0
                for cr in matched:
                    n = self.sync_repo(conn, cr["project"], cr["repo"])
                    total += n
                return {"ok": True, "total": total}
            except HarborUnavailableError:
                return {"ok": False, "error": "Harbor 镜像仓库不可达，无法同步。请检查网络连接和 Harbor 服务状态"}

    # ── 查询 ──

    def get_repositories(self) -> dict:
        """获取仓库列表（来自数据库），返回 {"repositories": [...], "last_sync": "..."} """
        with self._db.conn() as conn:
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

    def get_artifacts(self, repo_id: int, page: int = 1, page_size: int = 20) -> dict:
        """获取指定仓库的 artifact/tag 列表（分页）"""
        with self._db.conn() as conn:
            total = conn.execute(
                "SELECT COUNT(*) as cnt FROM cd_registry_artifacts WHERE repo_id=?",
                (repo_id,)
            ).fetchone()["cnt"]

            offset = (page - 1) * page_size
            rows = conn.execute(
                """SELECT a.*, r.project_name, r.repo_name
                FROM cd_registry_artifacts a
                JOIN cd_registry_repositories r ON r.id=a.repo_id
                WHERE a.repo_id=?
                ORDER BY a.push_time DESC
                LIMIT ? OFFSET ?""",
                (repo_id, page_size, offset)
            ).fetchall()
            items = [
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
            total_pages = max(1, (total + page_size - 1) // page_size)
            return {
                "items": items,
                "total": total,
                "page": page,
                "page_size": page_size,
                "total_pages": total_pages,
            }

    @staticmethod
    def _norm_summary(raw_summary: dict) -> dict:
        """规范化 Harbor summary：支持大小写 key 和嵌套 summary"""
        if not raw_summary:
            return {}
        nested = raw_summary.get("summary") or {}
        c = raw_summary.get("critical", raw_summary.get("Critical", nested.get("Critical", 0)))
        h = raw_summary.get("high", raw_summary.get("High", nested.get("High", 0)))
        m = raw_summary.get("medium", raw_summary.get("Medium", nested.get("Medium", 0)))
        low = raw_summary.get("low", raw_summary.get("Low", nested.get("Low", 0)))
        total = raw_summary.get("total", c + h + m + low)
        fixable = raw_summary.get("fixable", 0)
        return {"critical": c, "high": h, "medium": m, "low": low, "total": total, "fixable": fixable}

    def get_scan_report(self, repo_id: int, tag: str) -> dict | None:
        """先读数据库缓存，再实时请求 Harbor 获取详情（优先 Harbor 最新数据）"""
        with self._db.conn() as conn:
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

            c, h, m, low = (row["vuln_critical"] or 0), (row["vuln_high"] or 0), (row["vuln_medium"] or 0), (row["vuln_low"] or 0)
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
                "low": low,
                "total": c + h + m + low,
                "fixable": row["vuln_fixable"] or 0,
            }

            # 实时请求 Harbor 获取最新详情（不依赖数据库数量，解决旧缓存数据为 0 的问题）
            harbor_unavailable = False
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
            except HarborUnavailableError as e:
                logger.warning(f"Harbor 不可达，回退到数据库缓存: {e}")
                harbor_unavailable = True
            except Exception as e:
                logger.warning(f"实时获取扫描详情失败，回退到汇总: {e}")

            result = {
                "scan_status": row["scan_status"] or "",
                "severity": row["scan_severity"] or "",
                "summary": db_summary,
                "harbor_url": harbor_url,
                "digest": digest,
            }
            if harbor_unavailable:
                result["warning"] = "Harbor 不可达，以下为历史缓存数据，可能不是最新"
            return result

    def trigger_scan(self, repo_id: int, tag: str) -> dict:
        """触发 Harbor 重新扫描该 artifact（v2 用 tag，参照 PHP 实现）"""
        with self._db.conn() as conn:
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
            except HarborUnavailableError as e:
                logger.warning(f"触发扫描失败（Harbor 不可达）: {e}")
                return {"ok": False, "error": "Harbor 镜像仓库不可达，无法触发扫描"}
            except Exception as e:
                logger.error("触发扫描失败", exc_info=e)
                return {"ok": False, "error": "触发扫描失败，请联系管理员"}

    # ── 删除 ──

    def delete_artifact(self, repo_id: int, tag: str) -> dict:
        """删除 artifact（Harbor v1/v2 通用 + 更新数据库）"""
        with self._db.conn() as conn:
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
            return {"ok": True}

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
_sync_db_factory = None  # 保存 db_factory 用于重启


def _default_db_factory():
    return Database()


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
        except HarborUnavailableError as e:
            logger.warning(f"定时同步跳过（Harbor 不可达）: {e}")
        except Exception as e:
            logger.error(f"定时同步异常: {e}")


def start_background_sync(db_factory, interval: int | None = None):
    """启动后台定时同步线程"""
    global _sync_thread, _sync_stop, _sync_db_factory
    _sync_db_factory = db_factory
    interval = int(getattr(settings, "registry_sync_interval", 30)) if interval is None else int(interval)
    if interval <= 0:
        logger.info("定时同步已关闭 (interval=0)")
        return
    _sync_stop.clear()
    _sync_thread = threading.Thread(
        target=_sync_worker,
        args=(db_factory, interval),
        daemon=True,
        name="registry-sync",
    )
    _sync_thread.start()
    logger.info(f"定时同步已启动 (每 {interval} 分钟)")


def restart_background_sync(interval: int):
    """根据新间隔重启定时同步线程"""
    global _sync_thread, _sync_stop, _sync_db_factory
    if _sync_thread and _sync_thread.is_alive():
        _sync_stop.set()
        _sync_thread.join(timeout=5)
        _sync_stop.clear()
    if _sync_db_factory is None:
        _sync_db_factory = _default_db_factory
    if interval > 0:
        start_background_sync(_sync_db_factory, interval)
        logger.info(f"定时同步间隔已更新为 {interval} 分钟")
    else:
        _sync_thread = None
        logger.info("定时同步已关闭")


def stop_background_sync():
    global _sync_stop
    _sync_stop.set()
