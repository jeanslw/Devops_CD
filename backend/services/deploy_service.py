"""部署编排服务 — 查映射 → 选策略 → 执行 → 记日志 → 通知"""

from datetime import datetime
from backend.database import Database
from backend.deployers import deployer_registry, DeployTarget
from backend.config import settings
from backend.deploy_log import S
from backend.crypto import decrypt
from .ci_service import CiService
from .notification import notify_deploy


def _parse_server_ids(server_ids: str) -> list[int]:
    """安全地解析 server_ids，忽略空值和非法内容。"""
    return [int(s) for s in (server_ids or "").split(",") if s.strip().isdigit()]


def _parse_command_options(commands: str) -> dict:
    """解析命令字符串中的 |INV| 标记。

    格式: <commands>[|INV|<inventory>]
    """
    options: dict = {}
    if not commands:
        return options
    cmds = commands
    for marker, key in (("|INV|", "inventory"),):
        if marker in cmds:
            cmds, value = cmds.split(marker, 1)
            options[key] = value
    options["commands"] = cmds
    return options


class DeployService:
    """部署编排：整合 CI 查询 + Deployer 执行 + 日志记录 + 通知"""

    def __init__(self, db: Database):
        self._db = db
        self._ci = CiService(db)

    def _get_targets(self, server_ids: str) -> list[tuple[int, DeployTarget]]:
        """解析 server_ids → [(id, DeployTarget), ...]"""
        with self._db.conn() as conn:
            if server_ids:
                ids = _parse_server_ids(server_ids)
                if not ids:
                    return []
                placeholders = ",".join("?" * len(ids))
                rows = conn.execute(
                    f"SELECT * FROM cd_servers WHERE id IN ({placeholders})", ids
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM cd_servers ORDER BY name").fetchall()

            return [
                (r["id"], DeployTarget(
                    host=r["host"], port=r["port"], user=r["user"],
                    password=decrypt(r["password"] or ""),
                    ssh_key=decrypt(r["ssh_key"] or ""),
                ))
                for r in rows
            ]

    def execute(
        self,
        project: str,
        tag: str,
        deploy_type: str,
        *,
        server_ids: str = "",
        target_path: str = "",
        deploy_mode: str = "",
        commands: str = "",
        yaml_content: str = "",
        k8s_ns: str = "",
        k8s_deploy: str = "",
        k8s_container: str = "",
        env_file: str = "",
        bot_id: int = 0,
        lang: str = "en",
        callback=None,
    ) -> dict:
        """批量部署到一台或多台服务器"""
        harbor_repo = self._ci.resolve_harbor_repo(project)
        if not harbor_repo:
            raise ValueError(f"Project '{project}' has no harbor_repository configured")

        image = f"{settings.harbor_registry}/{harbor_repo}:{tag}"
        project_key = self._ci.resolve_project_key(project) or project
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        options = _parse_command_options(commands) if commands else {}
        if yaml_content: options["yaml_content"] = yaml_content
        if k8s_ns: options["namespace"] = k8s_ns
        if k8s_deploy: options["deployment"] = k8s_deploy
        if k8s_container: options["container"] = k8s_container
        if env_file: options["env_file"] = env_file

        if not deployer_registry.is_registered(deploy_type):
            raise ValueError(f"Unsupported deploy type: {deploy_type}")

        targets = self._get_targets(server_ids)
        if not targets:
            raise ValueError("No available target servers")

        deployer = deployer_registry.create(deploy_type)
        results = []

        total = len(targets)
        is_batch = total > 1

        for i, (sid, target) in enumerate(targets):
            target.path = target_path
            target.mode = deploy_mode
            target.options = options

            # 批量部署时在 SSE 流中显示服务器分隔线
            if is_batch:
                host_label = f"#{sid} {target.host}"
                callback(S("deploy_log.batch_server_start", current=i + 1, total=total, host=host_label))

            error = deployer.validate(target)
            if error:
                results.append({"server_id": sid, "host": target.host, "status": "failed", "output": error})
                if is_batch:
                    callback(S("deploy_log.batch_server_end", current=i + 1, total=total, host=host_label, result="fail"))
                continue

            try:
                r = deployer.deploy(target, image, project_key, tag, callback=callback)
                results.append({"server_id": sid, "host": target.host, "status": r.status, "output": r.output})
            except Exception as e:
                results.append({"server_id": sid, "host": target.host, "status": "failed", "output": str(e)})

            if is_batch:
                callback(S("deploy_log.batch_server_end", current=i + 1, total=total, host=host_label, result=results[-1]["status"]))

        # 记录日志（一次部署一条记录，批量时合并输出和状态）
        with self._db.conn() as conn:
            row = conn.execute("SELECT COALESCE(MAX(deploy_id), 0) + 1 AS next_id FROM cd_deploy_logs FOR UPDATE").fetchone()
            deploy_id = row["next_id"] if row else 1

            if is_batch:
                # 批量：合并为一条记录
                target_str = ", ".join(
                    f"[{i + 1}/{total}] #{r['server_id']} {r['host']}"
                    for i, r in enumerate(results)
                )
                parts = []
                for i, r in enumerate(results):
                    parts.append(f"━━━ [{i + 1}/{total}] #{r['server_id']} {r['host']} ({r['status']}) ━━━")
                    parts.append(r["output"] or "")
                merged_output = "\n".join(parts)[:settings.log_truncate_chars]
                # 整体状态
                oks = sum(1 for r in results if r["status"] == "ok")
                if oks == len(results):
                    status = "ok"
                elif oks == 0:
                    status = "failed"
                else:
                    status = "partial"
                conn.execute(
                    "INSERT INTO cd_deploy_logs (deploy_id,project,tag,image,deploy_type,target,status,output) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (deploy_id, project_key, tag, image, deploy_type, target_str, status, merged_output),
                )
            else:
                r = results[0]
                target_label = f"#{r['server_id']} {r['host']}"
                output = r["output"][:settings.log_truncate_chars] if r["output"] else ""
                conn.execute(
                    "INSERT INTO cd_deploy_logs (deploy_id,project,tag,image,deploy_type,target,status,output) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (deploy_id, project_key, tag, image, deploy_type, target_label, r["status"], output),
                )

        # 通知
        ok_count = sum(1 for r in results if r["status"] == "ok")
        if ok_count == len(results):
            status = "✅ Success"
        elif ok_count > 0:
            status = f"⚠️ Partial success {ok_count}/{len(results)}"
        else:
            status = "❌ Failed"
        targets = []
        for r in results:
            label = "docker" if deploy_mode == "docker" else "ssh"
            targets.append(f"{label}[{r.get('host', '?')}]")
        notify_deploy(self._db, bot_id, tag, project_key, image, status,
                      deploy_mode or deploy_type, targets, lang=lang)

        return {"success": ok_count == len(results), "deploy_id": deploy_id, "results": results, "message": status}

    def list_logs(self, project: str = "", page: int = 1, page_size: int = 15) -> dict:
        """查询部署记录（分页）"""
        page = max(page, 1)
        page_size = max(min(page_size, 100), 1)
        offset = (page - 1) * page_size
        with self._db.conn() as conn:
            if project:
                total = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM cd_deploy_logs WHERE project=?",
                    (project,),
                ).fetchone()["cnt"]
                rows = conn.execute(
                    "SELECT * FROM cd_deploy_logs WHERE project=? ORDER BY deploy_id DESC LIMIT ? OFFSET ?",
                    (project, page_size, offset),
                ).fetchall()
            else:
                total = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM cd_deploy_logs"
                ).fetchone()["cnt"]
                rows = conn.execute(
                    "SELECT * FROM cd_deploy_logs ORDER BY deploy_id DESC LIMIT ? OFFSET ?",
                    (page_size, offset),
                ).fetchall()
            return {
                "items": [dict(r) for r in rows],
                "total": total,
                "page": page,
                "page_size": page_size,
                "total_pages": max((total + page_size - 1) // page_size, 1),
            }
