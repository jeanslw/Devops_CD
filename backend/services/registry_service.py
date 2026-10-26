"""镜像制品库服务 - 数据库缓存 + 定时同步 + CRUD"""

import logging
import threading
import urllib.parse
from datetime import datetime, timezone

from backend.config import settings
from backend.database import Database
from backend.services.ci_client import CiClientError, get_ci_client
from backend.services.registry_provider import (
    HarborUnavailableError,
    RegistryPermissionDeniedError,
    get_registry_provider,
    registry_capabilities,
    registry_type_hint,
)

logger = logging.getLogger("registry")


class RegistryService:
    """镜像仓库服务:定时同步 + CRUD + 扫描报告"""

    _instance = None
    _lock = threading.Lock()

    def __init__(self, db: Database):
        self._db = db
        self._provider_client = None

    # ── 配置读写 ──

    def get_config(self, key: str) -> str | None:
        """读取配置项"""
        with self._db.conn() as conn:
            try:
                row = conn.execute("SELECT value FROM cd_config WHERE key_name=?", (key,)).fetchone()
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
        """获取同步间隔(分钟),0=关闭.DB 优先,fallback 到环境变量"""
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

    def get_registry_meta(self) -> dict:
        """镜像仓库类型与能力开关(静态判断,不连仓库;前端据此隐藏不可用操作)

        registry_type 为契约下发的原样值:harbor | registry | ""(未取到,前端显示「未知」),
        不替前端默认成 harbor,避免把"没取到"伪装成"契约说是 harbor".
        """
        return {"registry_type": registry_type_hint(), "capabilities": registry_capabilities()}

    @property
    def _provider(self):
        """延迟初始化提供方:按 Glue 契约下发的仓库类型选 HarborClient / RegistryV2Provider:
        纯数据库查询不触发 API 探测"""
        if self._provider_client is None:
            try:
                self._provider_client = get_registry_provider()
            except RuntimeError as e:
                logger.error("Registry provider initialization failed", exc_info=e)
                raise HarborUnavailableError(
                    "镜像仓库不可达，无法连接仓库。请检查：\n"
                    f"1. 仓库地址是否可用（{e}）\n"
                    "2. 网络是否连通\n"
                    "3. 镜像仓库服务是否正常运行\n"
                ) from e
        return self._provider_client

    # ── 工具 ──

    @staticmethod
    def _to_mysql_dt(iso_str: str) -> str | None:
        """ISO 8601 时间戳 → MySQL DATETIME 字符串(统一归一化为 UTC 墙钟时间)

        空值 / 无法解析的值返回 None(写入 NULL):
        MySQL 的 DATETIME 列不接受空字符串(错误 1292),标准 OCI 仓库(registry:2)
        本就不提供推送/拉取时间,同步时该字段必须按 NULL 入库而不是空串.

        Harbor 返回的 push_time/pull_time 是 UTC(如 '2026-07-17T07:17:04.737Z').
        优先用 fromisoformat 解析,可正确处理 Z 与数值时区偏移(如 +08:00);
        3.10 的 fromisoformat 只认 3/6 位小数秒,而 Harbor 用 Go RFC3339Nano
        去尾零后小数位数不定,解析失败时回退字面量截断.
        """
        if not iso_str:
            return None
        try:
            dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                dt = dt.astimezone(timezone.utc)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            s = iso_str.replace("T", " ").replace("Z", "")
            if "." in s:
                s = s[: s.index(".")]
            if not s.strip():
                return None
            return s

    # ── 从 CI 映射获取待同步仓库 ──

    @staticmethod
    def _mapped_repo(p: dict) -> str:
        """容器仓库名:Glue 2.11 起字段改名为 registry_repository,旧版为 harbor_repository,双读兼容"""
        return (p.get("registry_repository") or p.get("harbor_repository") or "").strip()

    def _get_ci_repos(self) -> list[dict]:
        """从 CI 映射获取所有已配置镜像仓库的项目(经 API)"""
        projects = get_ci_client().get_projects() or []
        return [{"project": p["job_name"], "repo": self._mapped_repo(p)} for p in projects if self._mapped_repo(p)]

    # ── 同步逻辑 ──

    def sync_repo(self, conn, project: str, repo: str) -> int:
        """同步单个仓库的 artifacts 到数据库,返回新增/更新的数量"""
        # 先从镜像仓库拉取,成功后才建仓库记录:
        # 拉取失败(无权限/不可达/解析异常)不在列表里留下"0 Tags"的占位行
        try:
            raw = self._provider.list_artifacts(repo)
            logger.info(f"镜像仓库返回 {len(raw)} 条 artifacts: {repo}")
        except HarborUnavailableError:
            # 连接层不可达上抛,由 sync_all / sync_for_project 转 errors.harbor_unavailable
            raise
        except RegistryPermissionDeniedError:
            # 账号无查询权限:同样上抛,由 sync_all / sync_for_project 转 errors.registry_permission_denied
            raise
        except Exception as e:
            # 非连接类错误(404 / 解析异常等)保持容错,不打断其他仓库的同步
            logger.error(f"同步 {repo} 失败: {e}")
            return 0

        # 拉取成功,确保 repo 记录存在
        rows = conn.execute(
            "SELECT id FROM cd_registry_repositories WHERE project_name=? AND repo_name=?", (project, repo)
        ).fetchall()
        if rows:
            repo_id = rows[0]["id"]
        else:
            cur = conn.execute(
                "INSERT INTO cd_registry_repositories(project_name,repo_name) VALUES (?,?)", (project, repo)
            )
            repo_id = cur.lastrowid

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
                (repo_id, art["tag"], art["digest"]),
            ).fetchall()
            if rows:
                conn.execute(
                    """UPDATE cd_registry_artifacts SET size_bytes=?,push_time=?,pull_time=?,
                    scan_status=?,scan_severity=?,vuln_critical=?,vuln_high=?,vuln_medium=?,
                    vuln_low=?,vuln_fixable=?,last_sync=?
                    WHERE id=?""",
                    (
                        art["size_bytes"],
                        push_time,
                        pull_time,
                        art["scan_status"],
                        art["scan_severity"],
                        art["vuln_critical"],
                        art["vuln_high"],
                        art["vuln_medium"],
                        art["vuln_low"],
                        art["vuln_fixable"],
                        now,
                        rows[0]["id"],
                    ),
                )
            else:
                conn.execute(
                    """INSERT INTO cd_registry_artifacts
                    (repo_id,tag,digest,size_bytes,push_time,pull_time,
                     scan_status,scan_severity,vuln_critical,vuln_high,vuln_medium,vuln_low,vuln_fixable,last_sync)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        repo_id,
                        art["tag"],
                        art["digest"],
                        art["size_bytes"],
                        push_time,
                        pull_time,
                        art["scan_status"],
                        art["scan_severity"],
                        art["vuln_critical"],
                        art["vuln_high"],
                        art["vuln_medium"],
                        art["vuln_low"],
                        art["vuln_fixable"],
                        now,
                    ),
                )
            count += 1

        # 清理镜像仓库已不存在的 tag(sync 只做 upsert,需补"删 gone"才对齐真值源)
        # list_artifacts 返回空列表既可能是"仓库确实空"也可能是"404/分页异常",
        # 客户端无法区分,仅在拿到非空 tag 列表时才做差集删除,宁可保守不误删.
        current_tags = {art["tag"] for art in raw if art.get("tag")}
        if current_tags:
            placeholders = ",".join("?" for _ in current_tags)
            conn.execute(
                f"DELETE FROM cd_registry_artifacts WHERE repo_id=? AND tag NOT IN ({placeholders})",  # nosec
                (repo_id, *current_tags),
            )

        return count

    def sync_all(self) -> dict:
        """全量同步所有 CI 映射的仓库"""
        with self._db.conn() as conn:
            try:
                ci_repos = self._get_ci_repos()
                mapped = {(r["project"], r["repo"]) for r in ci_repos}

                # 清理已不在 ci_job_git_map 中的数据
                all_repos = conn.execute("SELECT id, project_name, repo_name FROM cd_registry_repositories").fetchall()
                for r in all_repos:
                    if (r["project_name"], r["repo_name"]) not in mapped:
                        conn.execute("DELETE FROM cd_registry_artifacts WHERE repo_id=?", (r["id"],))
                        conn.execute("DELETE FROM cd_registry_repositories WHERE id=?", (r["id"],))

                # 清理没有任何 Tag 的仓库记录:多为历史同步失败残留的占位行,
                # 拉取成功(即使 0 Tag)的本轮同步会立即重建,无误删风险
                conn.execute(
                    """DELETE FROM cd_registry_repositories WHERE NOT EXISTS
                       (SELECT 1 FROM cd_registry_artifacts WHERE repo_id=cd_registry_repositories.id)"""
                )

                total = 0
                errors = []
                for cr in ci_repos:
                    try:
                        n = self.sync_repo(conn, cr["project"], cr["repo"])
                        total += n
                        logger.info(f"同步 {cr['repo']}: {n} 条")
                    except HarborUnavailableError:
                        raise  # 直接抛出,不要在 errors 里吞掉
                    except RegistryPermissionDeniedError as e:
                        raise e  # 无查询权限必须让界面明确提示,不能伪装成"同步了 0 条"
                    except Exception as e:
                        errors.append(cr["repo"])
                        logger.error(f"同步 {cr['repo']} 失败", exc_info=e)
                return {"ok": True, "total": total, "repos": len(ci_repos), "errors": errors}
            except HarborUnavailableError:
                return {
                    "ok": False,
                    "error": "镜像仓库不可达，无法同步。请检查网络连接和镜像仓库服务状态",
                    "error_key": "errors.harbor_unavailable",
                }
            except RegistryPermissionDeniedError as e:
                logger.error("镜像仓库拒绝访问（账号无查询权限）", exc_info=e)
                return {
                    "ok": False,
                    "error": "镜像仓库账号没有查询权限，无法同步。请检查仓库账号密码，并确认该账号有权访问相关仓库",
                    "error_key": "errors.registry_permission_denied",
                }
            except CiClientError as e:
                logger.error("CI service unavailable while fetching repository mappings", exc_info=e)
                return {
                    "ok": False,
                    "error": "CI 服务不可用，无法获取仓库映射，请联系管理员",
                    "error_key": "errors.ci_service_unavailable",
                }

    def sync_for_project(self, project: str) -> dict:
        """增量同步指定项目/仓库的仓库(支持按 job_name 或 harbor_repository 匹配)"""
        with self._db.conn() as conn:
            try:
                ci_repos = self._get_ci_repos()
            except CiClientError as e:
                logger.error("CI service unavailable while fetching repository mappings", exc_info=e)
                return {
                    "ok": False,
                    "error": "CI 服务不可用，无法获取仓库映射，请联系管理员",
                    "error_key": "errors.ci_service_unavailable",
                }
            matched = [
                cr
                for cr in ci_repos
                if cr["project"] == project or cr["repo"] == project or cr["repo"].startswith(project + "/")
            ]
            if not matched:
                return {"ok": False, "error": "未找到该项目仓库映射"}
            # 清理这些仓库里没有任何 Tag 的残留记录(历史同步失败占位行),拉取成功后本轮重建
            placeholders = ",".join("?" for _ in matched)
            conn.execute(
                f"""DELETE FROM cd_registry_repositories
                    WHERE NOT EXISTS
                      (SELECT 1 FROM cd_registry_artifacts WHERE repo_id=cd_registry_repositories.id)
                      AND (project_name=? OR repo_name IN ({placeholders}))""",
                (project, *(cr["repo"] for cr in matched)),
            )
            try:
                total = 0
                for cr in matched:
                    n = self.sync_repo(conn, cr["project"], cr["repo"])
                    total += n
                return {"ok": True, "total": total}
            except HarborUnavailableError:
                return {
                    "ok": False,
                    "error": "镜像仓库不可达，无法同步。请检查网络连接和镜像仓库服务状态",
                    "error_key": "errors.harbor_unavailable",
                }
            except RegistryPermissionDeniedError as e:
                logger.error("镜像仓库拒绝访问（账号无查询权限）", exc_info=e)
                return {
                    "ok": False,
                    "error": "镜像仓库账号没有查询权限，无法同步。请检查仓库账号密码，并确认该账号有权访问相关仓库",
                    "error_key": "errors.registry_permission_denied",
                }

    # ── 查询 ──

    def get_repositories(self) -> dict:
        """获取仓库列表(来自数据库),返回 {'repositories': [...], 'last_sync': '...'}"""
        with self._db.conn() as conn:
            try:
                ci_repos = self._get_ci_repos()
            except CiClientError:
                return {"repositories": [], "last_sync": ""}
            mapped = {(r["project"], r["repo"]) for r in ci_repos}

            # 全局最后同步时间
            sync_row = conn.execute("SELECT MAX(last_sync) AS last_sync FROM cd_registry_artifacts").fetchone()
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
                result.append(
                    {
                        "id": row["id"],
                        "project": row["project_name"],
                        "repo": row["repo_name"],
                        "tag_count": row["tag_count"] or 0,
                        "latest_push": row["latest_push"] or "",
                    }
                )
            return {"repositories": result, "last_sync": last_sync}

    def get_artifacts(self, repo_id: int, page: int = 1, page_size: int = 20) -> dict:
        """获取指定仓库的 artifact/tag 列表(分页)"""
        with self._db.conn() as conn:
            total = conn.execute(
                "SELECT COUNT(*) as cnt FROM cd_registry_artifacts WHERE repo_id=?", (repo_id,)
            ).fetchone()["cnt"]

            offset = (page - 1) * page_size
            rows = conn.execute(
                """SELECT a.*, r.project_name, r.repo_name
                FROM cd_registry_artifacts a
                JOIN cd_registry_repositories r ON r.id=a.repo_id
                WHERE a.repo_id=?
                ORDER BY a.push_time DESC
                LIMIT ? OFFSET ?""",
                (repo_id, page_size, offset),
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
        """规范化 Harbor summary:支持大小写 key 和嵌套 summary"""
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
        """先读数据库缓存,再实时请求镜像仓库获取详情(优先 Harbor 最新数据)"""
        if not registry_capabilities().get("scan"):
            # 标准 registry 无漏洞扫描接口(能力开关为 False)
            return {
                "error": "当前镜像仓库类型（registry）不支持漏洞扫描",
                "error_key": "errors.scan_unsupported",
            }
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

            c, h, m, low = (
                (row["vuln_critical"] or 0),
                (row["vuln_high"] or 0),
                (row["vuln_medium"] or 0),
                (row["vuln_low"] or 0),
            )
            digest = row["digest"] or ""
            # repo_name="mycode/devops-glue" → harbor_project="mycode", harbor_repo="devops-glue"
            repo_name = row["repo_name"] or ""
            if "/" in repo_name:
                harbor_project, harbor_repo = repo_name.split("/", 1)
            else:
                harbor_project = harbor_repo = repo_name
            harbor_url = (
                f"{self._provider.base_url}/harbor/projects/"
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

            # 实时请求 Harbor 获取最新详情(数据库缓存可能为 0)
            harbor_unavailable = False
            try:
                live = self._provider.get_scan_report(row["repo_name"], tag)
                if live:
                    live_summary = self._norm_summary(live.get("summary") or {})
                    # 优先用 Harbor 的 summary;如果 Harbor summary 全 0,fallback 数据库
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
        """触发镜像仓库重新扫描该 artifact(仅 Harbor 提供该接口)"""
        if not registry_capabilities().get("scan"):
            # 标准 registry 无扫描触发接口(能力开关为 False)
            return {
                "ok": False,
                "error": "当前镜像仓库类型（registry）不支持漏洞扫描",
                "error_key": "errors.scan_unsupported",
            }
        with self._db.conn() as conn:
            try:
                row = conn.execute(
                    """SELECT r.repo_name
                       FROM cd_registry_artifacts a
                       JOIN cd_registry_repositories r ON a.repo_id = r.id
                       WHERE a.repo_id=? AND a.tag=?""",
                    (repo_id, tag),
                ).fetchone()
                if not row:
                    return {"ok": False, "error": "Tag 不存在"}

                self._provider.trigger_scan(row["repo_name"], tag)
                return {"ok": True, "detail": "扫描已触发，请等待 Harbor 完成后再查看报告"}
            except HarborUnavailableError as e:
                logger.warning(f"触发扫描失败（镜像仓库不可达）: {e}")
                return {"ok": False, "error": "镜像仓库不可达，无法触发扫描"}
            except Exception as e:
                logger.error("触发扫描失败", exc_info=e)
                return {"ok": False, "error": "触发扫描失败，请联系管理员"}


# ── 后台定时同步 ──

_sync_thread = None
_sync_stop = threading.Event()
_sync_db_factory = None  # 保存 db_factory 用于重启


def _default_db_factory():
    return Database()


def _sync_worker(db_factory, interval_minutes: int):
    """后台线程:定时全量同步"""
    try:
        while not _sync_stop.is_set():
            _sync_stop.wait(interval_minutes * 60)
            if _sync_stop.is_set():
                break
            try:
                from backend.dlock import acquire

                # 多副本/多 worker 隔离:锁被其他存活实例持有时静默跳过本轮
                if not acquire(db_factory(), "registry_sync", ttl_seconds=max(interval_minutes * 180, 120)):
                    continue
                svc = RegistryService(db_factory())
                result = svc.sync_all()
                if result.get("ok"):
                    logger.info(f"定时同步完成: {result['total']} artifacts, {result['repos']} repos")
                else:
                    # Harbor / CI 不可达时 sync_all 返回 ok=False:打"完成"会与真实状态不符
                    logger.warning(f"定时同步中止: {result.get('error') or '未知原因'}")
            except HarborUnavailableError as e:
                logger.warning(f"定时同步跳过（Harbor 不可达）: {e}")
            except Exception as e:
                logger.error(f"定时同步异常: {e}")
    finally:
        # 线程退出时主动释放锁;否则要等 TTL(最长 interval*180 秒)过期,期间其他实例无法接管同步
        try:
            from backend.dlock import release

            release(db_factory(), "registry_sync")
        except Exception:
            pass


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
