"""部署编排服务 — 查映射 → 选策略 → 执行 → 记日志 → 通知"""

from datetime import datetime
from app.database import Database
from app.deployers import deployer_registry, DeployTarget
from app.config import settings
from .ci_service import CiService
from .notification import send_webhook


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
                ids = [int(x.strip()) for x in server_ids.split(",") if x.strip().isdigit()]
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
    ) -> dict:
        """批量部署到一台或多台服务器"""
        harbor_repo = self._ci.resolve_harbor_repo(project)
        if not harbor_repo:
            raise ValueError(f"项目 '{project}' 未配置 harbor_repository")

        image = f"{settings.harbor_registry}/{harbor_repo}:{tag}"
        project_key = self._ci.resolve_project_key(project) or project
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        options = {}
        if commands:
            cmds = commands
            if "|FILTER|" in cmds:
                fp = cmds.split("|FILTER|", 1)
                cmds = fp[0]
                options["filter"] = fp[1]
            if "|INV|" in cmds:
                parts = cmds.split("|INV|", 1)
                cmds = parts[0]
                options["inventory"] = parts[1]
            if "|VERIFY|" in cmds:
                vp = cmds.split("|VERIFY|", 1)
                cmds = vp[0]
                options["verify"] = vp[1]
            options["commands"] = cmds
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
                r = deployer.deploy(target, image, project_key, tag)
                results.append({"server_id": sid, "host": target.host, "status": r.status, "output": r.output})
            except Exception as e:
                results.append({"server_id": sid, "host": target.host, "status": "failed", "output": str(e)})

        # 记录日志
        conn = self._db.conn()
        try:
            for r in results:
                conn.execute(
                    "INSERT INTO cd_deploy_logs (project,tag,image,deploy_type,target,status,output) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (project_key, tag, image, deploy_type, f"#{r['server_id']} {r['host']}",
                     r["status"], r["output"][:settings.log_truncate_chars]),
                )
            conn.commit()
        finally:
            conn.close()

        # 通知
        ok_count = sum(1 for r in results if r["status"] == "ok")
        servers = ", ".join(r.get("host", "?") for r in results)
        msg = f"[服务器部署] [{now}] {project_key} {tag} → {ok_count}/{len(results)} 成功\n服务器: {servers}\n镜像: {image}"
        if bot_id:
            conn = self._db.conn()
            try:
                bot = conn.execute("SELECT * FROM cd_bots WHERE id=?", (bot_id,)).fetchone()
                if bot: send_webhook(bot["webhook_url"], msg)
            finally:
                conn.close()

        return {"success": ok_count == len(results), "message": msg, "results": results}

    def list_logs(self, project: str = "") -> list[dict]:
        """查询部署记录"""
        conn = self._db.conn()
        try:
            if project:
                rows = conn.execute(
                    "SELECT * FROM cd_deploy_logs WHERE project=? ORDER BY created_at DESC LIMIT 50",
                    (project,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM cd_deploy_logs ORDER BY created_at DESC LIMIT 100"
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
