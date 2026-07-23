"""部署编排服务 — 查映射 → 选策略 → 执行 → 记日志 → 通知"""

from datetime import datetime
from app.database import Database
from app.deployers import deployer_registry, DeployTarget
from app.config import settings
from .ci_service import CiService
from .notification import notify_deploy


def _parse_server_ids(server_ids: str) -> list[int]:
    """安全地解析 server_ids，忽略空值和非法内容。"""
    return [int(s) for s in (server_ids or "").split(",") if s.strip().isdigit()]


def _parse_command_options(commands: str) -> dict:
    """解析命令字符串中的 |FILTER| / |INV| 标记。

    格式: <commands>[|FILTER|<filter>][|INV|<inventory>]
    """
    options: dict = {}
    if not commands:
        return options
    cmds = commands
    for marker, key in (("|FILTER|", "filter"), ("|INV|", "inventory")):
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
        conn = self._db.conn()
        try:
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
                    password=r["password"] or "",
                ))
                for r in rows
            ]
        finally:
            conn.close()

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
        bot_id: int = 0,
        callback=None,
    ) -> dict:
        """批量部署到一台或多台服务器"""
        harbor_repo = self._ci.resolve_harbor_repo(project)
        if not harbor_repo:
            raise ValueError(f"项目 '{project}' 未配置 harbor_repository")

        image = f"{settings.harbor_registry}/{harbor_repo}:{tag}"
        project_key = self._ci.resolve_project_key(project) or project
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        options = _parse_command_options(commands) if commands else {}
        if yaml_content: options["yaml_content"] = yaml_content
        if k8s_ns: options["namespace"] = k8s_ns
        if k8s_deploy: options["deployment"] = k8s_deploy
        if k8s_container: options["container"] = k8s_container

        if not deployer_registry.is_registered(deploy_type):
            raise ValueError(f"不支持的部署类型: {deploy_type}")

        targets = self._get_targets(server_ids)
        if not targets:
            raise ValueError("没有可用的目标服务器")

        deployer = deployer_registry.create(deploy_type)
        results = []

        for sid, target in targets:
            target.path = target_path
            target.mode = deploy_mode
            target.options = options

            error = deployer.validate(target)
            if error:
                results.append({"server_id": sid, "host": target.host, "status": "failed", "output": error})
                continue

            try:
                r = deployer.deploy(target, image, project_key, tag, callback=callback)
                results.append({"server_id": sid, "host": target.host, "status": r.status, "output": r.output})
            except Exception as e:
                results.append({"server_id": sid, "host": target.host, "status": "failed", "output": str(e)})

        # 记录日志（生成递增 deploy_id，同一次部署共享）
        conn = self._db.conn()
        try:
            row = conn.execute("SELECT COALESCE(MAX(deploy_id), 0) + 1 AS next_id FROM cd_deploy_logs").fetchone()
            deploy_id = row["next_id"] if row else 1
            for r in results:
                conn.execute(
                    "INSERT INTO cd_deploy_logs (deploy_id,project,tag,image,deploy_type,target,status,output) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (deploy_id, project_key, tag, image, deploy_type, f"#{r['server_id']} {r['host']}",
                     r["status"], r["output"][:settings.log_truncate_chars]),
                )
            conn.commit()
        finally:
            conn.close()

        # 通知
        ok_count = sum(1 for r in results if r["status"] == "ok")
        if ok_count == len(results):
            status = "✅ 部署成功"
        elif ok_count > 0:
            status = f"⚠️ 部分成功 {ok_count}/{len(results)}"
        else:
            status = "❌ 部署失败"
        targets = []
        for r in results:
            label = "docker" if deploy_mode == "docker" else "单机"
            targets.append(f"{label}[{r.get('host', '?')}]")
        notify_deploy(self._db, bot_id, tag, project_key, image, status,
                      deploy_mode or deploy_type, targets)

        return {"success": ok_count == len(results), "deploy_id": deploy_id, "results": results}

    def list_logs(self, project: str = "", page: int = 1, page_size: int = 15) -> dict:
        """查询部署记录（分页）"""
        page = max(page, 1)
        page_size = max(min(page_size, 100), 1)
        offset = (page - 1) * page_size
        conn = self._db.conn()
        try:
            if project:
                total = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM cd_deploy_logs WHERE project=?",
                    (project,),
                ).fetchone()["cnt"]
                rows = conn.execute(
                    "SELECT * FROM cd_deploy_logs WHERE project=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (project, page_size, offset),
                ).fetchall()
            else:
                total = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM cd_deploy_logs"
                ).fetchone()["cnt"]
                rows = conn.execute(
                    "SELECT * FROM cd_deploy_logs ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (page_size, offset),
                ).fetchall()
            return {
                "items": [dict(r) for r in rows],
                "total": total,
                "page": page,
                "page_size": page_size,
                "total_pages": max((total + page_size - 1) // page_size, 1),
            }
        finally:
            conn.close()
