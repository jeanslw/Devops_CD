"""部署编排服务 — 查映射 → 选策略 → 执行 → 记日志 → 通知"""

import json
import logging
import time
from collections.abc import Callable

from backend.auth import enforce_deploy_perm
from backend.config import settings
from backend.crypto import decrypt
from backend.database import Database
from backend.deploy_log import S
from backend.deploy_run import (
    DeployCancelled,
    clear_cancel_checker,
    deploy_run_manager,
    find_running_deploy,
    finish_deploy_record,
    set_cancel_checker,
    start_deploy_record,
)
from backend.deployers import DeployTarget, deployer_registry

from .ci_service import CiService
from .notification import notify_deploy

logger = logging.getLogger(__name__)


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
                rows = conn.execute(f"SELECT * FROM cd_servers WHERE id IN ({placeholders})", ids).fetchall()
            else:
                rows = conn.execute("SELECT * FROM cd_servers ORDER BY name").fetchall()

            return [
                (
                    r["id"],
                    DeployTarget(
                        host=r["host"],
                        port=r["port"],
                        user=r["user"],
                        password=decrypt(r["password"] or ""),
                        ssh_key=decrypt(r["ssh_key"] or ""),
                    ),
                )
                for r in rows
            ]

    def _build_target_str(self, results: list, is_batch: bool, total: int) -> str:
        """批量时合并 target 描述，单台时返回单条描述。"""
        if is_batch:
            return ", ".join(f"[{i + 1}/{total}] #{r['server_id']} {r['host']}" for i, r in enumerate(results))
        if not results:
            return "(无)"
        r = results[0]
        return f"#{r['server_id']} {r['host']}"

    def _build_output(self, results: list, is_batch: bool, total: int) -> str:
        """合并部署输出，批量时加服务器分隔线。"""
        if is_batch:
            parts = []
            for i, r in enumerate(results):
                parts.append(f"━━━ [{i + 1}/{total}] #{r['server_id']} {r['host']} ({r['status']}) ━━━")
                parts.append(r["output"] or "")
            return "\n".join(parts)
        if not results:
            return ""
        return results[0]["output"] or ""

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
        deploy_note: str = "",
        bot_id: int = 0,
        lang: str = "en",
        callback: Callable | None = None,
        user: dict | None = None,
    ) -> dict:
        """批量部署到一台或多台服务器。
        user 参数：当前登录用户信息 dict（含 role / permissions / username），
                 用于部署前的二次权限校验与审计日志 triggered_by 字段，
                 传入 None 时跳过校验（仅限内部可信调用）。
        v1.3.1 起：部署开始插入 running 记录（并发锁 + 取消依据），结束时更新
                 状态 / 耗时 / 分阶段耗时 / 说明。"""
        # K8S 子模式必须走 _deploy_k8s_core，禁止走 SSH/Compose 路线（API 签名不兼容）
        if deploy_type.startswith("k8s/"):
            raise ValueError(
                f"部署类型 '{deploy_type}' 不属于 SSH/Compose 路线，"
                "请使用 DeployService 之外的 K8S 专用流程（routers/k8s_deploy._deploy_k8s_core）"
            )
        # ── 部署时二次权限校验（防御深度）──
        if user is not None:
            enforce_deploy_perm(user, deploy_type)
        triggered_by = (user or {}).get("username", "")

        harbor_repo = self._ci.resolve_harbor_repo(project)
        if not harbor_repo:
            raise ValueError(f"Project '{project}' has no harbor_repository configured")

        image = f"{settings.harbor_registry}/{harbor_repo}:{tag}"
        project_key = self._ci.resolve_project_key(project) or project

        # ── 并发锁：同一项目同时只允许一个进行中部署 ──
        running = find_running_deploy(self._db, project_key)
        if running:
            raise ValueError(
                f"项目 '{project_key}' 已有部署进行中 (deploy #{running['deploy_id']})，请等待完成或取消后再试"
            )

        options = _parse_command_options(commands) if commands else {}
        if yaml_content:
            options["yaml_content"] = yaml_content
        if k8s_ns:
            options["namespace"] = k8s_ns
        if k8s_deploy:
            options["deployment"] = k8s_deploy
        if k8s_container:
            options["container"] = k8s_container
        if env_file:
            options["env_file"] = env_file

        if not deployer_registry.is_registered(deploy_type):
            raise ValueError(f"Unsupported deploy type: {deploy_type}")

        targets = self._get_targets(server_ids)
        if not targets:
            raise ValueError("No available target servers")

        deployer = deployer_registry.create(deploy_type)

        # ── 参数快照（含 deploy_type 路由判别），供回滚重放 ──
        params_json = json.dumps(
            {
                "deploy_type": deploy_type,
                "project": project,
                "tag": tag,
                "server_ids": server_ids,
                "target_path": target_path,
                "deploy_mode": deploy_mode,
                "commands": commands,
                "yaml_content": yaml_content,
                "k8s_ns": k8s_ns,
                "k8s_deploy": k8s_deploy,
                "k8s_container": k8s_container,
                "env_file": env_file,
                "deploy_note": deploy_note,
                "bot_id": bot_id,
                "lang": lang,
            },
            ensure_ascii=False,
        )

        # ── 插入 running 记录 + 注册取消信号 ──
        deploy_id = start_deploy_record(
            self._db,
            deploy_type=deploy_type,
            project=project_key,
            tag=tag,
            image=image,
            triggered_by=triggered_by,
            deploy_note=deploy_note,
            params_json=params_json,
        )
        deploy_run_manager.register(deploy_id)
        set_cancel_checker(lambda: deploy_run_manager.is_cancelled(deploy_id))

        results = []
        stage_times = []
        total = len(targets)
        is_batch = total > 1
        started = time.time()

        try:
            for i, (sid, target) in enumerate(targets):
                target.path = target_path
                target.mode = deploy_mode
                target.options = options

                t0 = time.time()
                # 批量部署时在 SSE 流中显示服务器分隔线
                if is_batch:
                    host_label = f"#{sid} {target.host}"
                    if callback:
                        callback(S("deploy_log.batch_server_start", current=i + 1, total=total, host=host_label))

                error = deployer.validate(target)
                if error:
                    results.append({"server_id": sid, "host": target.host, "status": "failed", "output": error})
                    stage_times.append(
                        {
                            "server_id": sid,
                            "host": target.host,
                            "status": "failed",
                            "duration_ms": int((time.time() - t0) * 1000),
                        }
                    )
                    if is_batch and callback:
                        callback(
                            S("deploy_log.batch_server_end", current=i + 1, total=total, host=host_label, result="fail")
                        )
                    continue

                try:
                    r = deployer.deploy(target, image, project_key, tag, callback=callback)
                    results.append({"server_id": sid, "host": target.host, "status": r.status, "output": r.output})
                except DeployCancelled:
                    raise
                except Exception as e:
                    logger.error("Deploy service failed", exc_info=e)
                    results.append({"server_id": sid, "host": target.host, "status": "failed", "output": str(e)})

                stage_times.append(
                    {
                        "server_id": sid,
                        "host": target.host,
                        "status": results[-1]["status"],
                        "duration_ms": int((time.time() - t0) * 1000),
                    }
                )
                if is_batch and callback:
                    callback(
                        S(
                            "deploy_log.batch_server_end",
                            current=i + 1,
                            total=total,
                            host=host_label,
                            result=results[-1]["status"],
                        )
                    )

            duration_ms = int((time.time() - started) * 1000)

            # 整体状态
            oks = sum(1 for r in results if r["status"] == "ok")
            if oks == len(results):
                status = "ok"
            elif oks == 0:
                status = "failed"
            else:
                status = "partial"

            finish_deploy_record(
                self._db,
                deploy_id,
                status=status,
                target=self._build_target_str(results, is_batch, total),
                output=self._build_output(results, is_batch, total),
                duration_ms=duration_ms,
                stage_times=stage_times,
            )

            # 通知
            if oks == len(results):
                status_label = "✅ Success"
            elif oks > 0:
                status_label = f"⚠️ Partial success {oks}/{len(results)}"
            else:
                status_label = "❌ Failed"
            notify_targets = []
            for r in results:
                label = "docker" if deploy_mode == "docker" else "ssh"
                notify_targets.append(f"{label}[{r.get('host', '?')}]")
            notify_deploy(
                self._db,
                bot_id,
                tag,
                project_key,
                image,
                status_label,
                deploy_mode or deploy_type,
                notify_targets,
                lang=lang,
            )

            return {"success": oks == len(results), "deploy_id": deploy_id, "results": results, "message": status_label}

        except DeployCancelled:
            duration_ms = int((time.time() - started) * 1000)
            cancelled_output = self._build_output(results, is_batch, total)
            if cancelled_output:
                cancelled_output += "\n\n━━━ Deployment cancelled by user ━━━"
            else:
                cancelled_output = "Deployment cancelled by user"
            finish_deploy_record(
                self._db,
                deploy_id,
                status="terminated",
                target=self._build_target_str(results, is_batch, total),
                output=cancelled_output,
                duration_ms=duration_ms,
                stage_times=stage_times,
            )
            return {
                "success": False,
                "deploy_id": deploy_id,
                "cancelled": True,
                "results": results,
                "message": "❌ Cancelled",
            }
        except Exception as e:
            logger.error("Deploy service unexpected error", exc_info=e)
            duration_ms = int((time.time() - started) * 1000)
            error_output = self._build_output(results, is_batch, total)
            if error_output:
                error_output += f"\n\n━━━ 部署异常中断: {e} ━━━"
            else:
                error_output = f"部署异常中断: {e}"
            finish_deploy_record(
                self._db,
                deploy_id,
                status="failed",
                target=self._build_target_str(results, is_batch, total),
                output=error_output[: settings.log_truncate_chars],
                duration_ms=duration_ms,
                stage_times=stage_times,
            )
            raise
        finally:
            deploy_run_manager.unregister(deploy_id)
            clear_cancel_checker()

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
                    "SELECT *, id AS deploy_id FROM cd_deploy_logs WHERE project=? ORDER BY id DESC LIMIT ? OFFSET ?",
                    (project, page_size, offset),
                ).fetchall()
            else:
                total = conn.execute("SELECT COUNT(*) AS cnt FROM cd_deploy_logs").fetchone()["cnt"]
                rows = conn.execute(
                    "SELECT *, id AS deploy_id FROM cd_deploy_logs ORDER BY id DESC LIMIT ? OFFSET ?",
                    (page_size, offset),
                ).fetchall()
            return {
                "items": [dict(r) for r in rows],
                "total": total,
                "page": page,
                "page_size": page_size,
                "total_pages": max((total + page_size - 1) // page_size, 1),
            }
