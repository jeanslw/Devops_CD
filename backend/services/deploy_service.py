"""部署编排服务 - 查映射 → 选策略 → 执行 → 记日志 → 通知"""

import json
import logging
import time
from collections.abc import Callable

from backend.auth import enforce_deploy_perm
from backend.config import settings
from backend.crypto import decrypt_strict, DecryptError
from backend.database import Database
from backend.deploy_log import S
from backend.exceptions import AppException, ValidationError
from backend.deploy_run import (
    DeployCancelled,
    cancel_checker_for,
    claim_pending_deploy_record,
    clear_cancel_checker,
    deploy_run_manager,
    find_running_deploy,
    finish_deploy_record,
    set_cancel_checker,
    start_deploy_record,
)
from backend.deployers import DeployTarget, deployer_registry
from backend.deployers.base import run_remote_probe, validate_tag
from backend.services.registry_provider import (
    RegistryConfigError,
    registry_base_url,
    resolve_artifact_digest,
)

from .ci_service import CiService
from .notification import notify_deploy

logger = logging.getLogger(__name__)


def _parse_server_ids(server_ids: str) -> list[int]:
    """严格解析逗号分隔的正整数 ID;任何非法片段都拒绝,不静默缩小目标范围."""
    raw = (server_ids or "").strip()
    if not raw:
        return []
    ids = []
    for part in raw.split(","):
        value = part.strip()
        if not value.isdigit() or int(value) <= 0:
            raise ValidationError(f"server_ids 无效: {server_ids}", error_key="errors.deploy_validation")
        server_id = int(value)
        if server_id not in ids:
            ids.append(server_id)
    return ids


def resolve_target_servers(db: Database, server_ids: str = "", deploy_type: str = "") -> list[dict]:
    """解析并校验部署目标;显式 ID 必须全部存在且类型匹配,顺序与请求一致."""
    ids = _parse_server_ids(server_ids)
    allowed_types = ("k8s",) if deploy_type.startswith("k8s/") else (("ssh", "docker") if deploy_type else None)
    with db.conn() as conn:
        if ids:
            placeholders = ",".join("?" * len(ids))
            rows = conn.execute(f"SELECT * FROM cd_servers WHERE id IN ({placeholders})", ids).fetchall()  # nosec
            by_id = {row["id"]: row for row in rows}
            invalid = [
                server_id
                for server_id in ids
                if server_id not in by_id
                or (allowed_types is not None and by_id[server_id]["type"] not in allowed_types)
            ]
            if invalid:
                raise ValidationError(
                    f"server_ids 包含不存在或类型不匹配的目标: {','.join(map(str, invalid))}",
                    error_key="errors.deploy_validation",
                )
            return [by_id[server_id] for server_id in ids]
        if allowed_types is None:
            return conn.execute("SELECT * FROM cd_servers").fetchall()
        placeholders = ",".join("?" * len(allowed_types))
        return conn.execute(
            f"SELECT * FROM cd_servers WHERE type IN ({placeholders}) ORDER BY name, id",  # nosec
            allowed_types,
        ).fetchall()


def _parse_command_options(commands: str) -> dict:
    """解析命令字符串中的 |INV| 标记.

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
    """部署编排:整合 CI 查询 + Deployer 执行 + 日志记录 + 通知"""

    def __init__(self, db: Database):
        self._db = db
        self._ci = CiService(db)

    def _get_targets(self, server_ids: str, deploy_type: str = "ssh") -> list[tuple[int, DeployTarget]]:
        """解析 server_ids → [(id, DeployTarget), ...],不忽略失效或错误类型的目标."""
        rows = resolve_target_servers(self._db, server_ids, deploy_type)

        def _build_target(r):
            try:
                password = decrypt_strict(r["password"] or "")
                ssh_key = decrypt_strict(r["ssh_key"] or "")
            except DecryptError as e:
                raise AppException(
                    f"服务器「{r['name']}」凭据解密失败，请检查口令/私钥是否完好，及 SECRET_KEY 是否与加密时一致：{e}",
                    status_code=500,
                ) from e
            return (
                r["id"],
                DeployTarget(
                    host=r["host"],
                    port=r["port"],
                    user=r["user"],
                    password=password,
                    ssh_key=ssh_key,
                ),
            )

        return [_build_target(r) for r in rows]

    def _build_target_str(self, results: list, is_batch: bool, total: int) -> str:
        """批量时合并 target 描述,单台时返回单条描述."""
        if is_batch:
            return ", ".join(f"[{i + 1}/{total}] #{r['server_id']} {r['host']}" for i, r in enumerate(results))
        if not results:
            return "(无)"
        r = results[0]
        return f"#{r['server_id']} {r['host']}"

    def _build_output(self, results: list, is_batch: bool, total: int) -> str:
        """合并部署输出,批量时加服务器分隔线."""
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
        use_digest: bool = False,
        fail_fast: bool = False,
        health_cmd: str = "",
        deploy_note: str = "",
        rollback_type: str = "manual",
        bot_id: int = 0,
        lang: str = "en",
        callback: Callable | None = None,
        user: dict | None = None,
        approval_id: int = 0,
    ) -> dict:
        """批量部署到一台或多台服务器."""
        if deploy_type.startswith("k8s/"):
            raise ValueError(
                f"部署类型 '{deploy_type}' 不属于 SSH/Compose 路线，"
                "请使用 DeployService 之外的 K8S 专用流程（routers/k8s_deploy._deploy_k8s_core）"
            )
        if user is not None:
            enforce_deploy_perm(user, deploy_type)
        validate_tag(tag)
        triggered_by = (user or {}).get("username", "")

        harbor_repo = self._ci.resolve_harbor_repo(project)
        if not harbor_repo:
            raise ValueError(f"Project '{project}' has no registry_repository configured")

        try:
            registry = registry_base_url()
        except RegistryConfigError as e:
            raise ValueError(str(e)) from e
        digest = resolve_artifact_digest(harbor_repo, tag) if use_digest else ""
        if use_digest:
            if deploy_type != "ssh":
                raise ValidationError(
                    "digest 固定当前仅支持 SSH 部署；Compose 的 IMAGE/TAG 变量无法携带 digest",
                    error_key="errors.deploy_validation",
                )
            if not digest:
                raise ValidationError(
                    f"未能解析 tag '{tag}' 的 digest（仓库未同步或不可达），无法按 digest 部署",
                    error_key="errors.deploy_validation",
                )
            image = f"{registry}/{harbor_repo}@{digest}"
        else:
            image = f"{registry}/{harbor_repo}:{tag}"
        project_key = self._ci.resolve_project_key(project) or project

        # ── 并发锁:同一项目同时只允许一个进行中部署 ──
        running = find_running_deploy(self._db, project_key)
        if running:
            raise ValidationError(
                f"项目 '{project_key}' 已有部署进行中 (deploy #{running['deploy_id']})，请等待完成或取消后再试",
                error_key="errors.deploy_busy",
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

        targets = self._get_targets(server_ids, deploy_type)
        if not targets:
            raise ValueError("No available target servers")

        deployer = deployer_registry.create(deploy_type)

        # ── 参数快照(含 deploy_type 路由判别),供回滚重放 ──
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
                "use_digest": use_digest,
                "fail_fast": fail_fast,
                "health_cmd": health_cmd,
                "deploy_note": deploy_note,
                "rollback_type": rollback_type,
                "bot_id": bot_id,
                "lang": lang,
            },
            ensure_ascii=False,
        )

        # ── 领取/插入部署记录 + 注册取消信号 ──
        # 经审批单执行:复用申请阶段的 pending 记录(无记录时回退新建)
        try:
            deploy_id = claim_pending_deploy_record(self._db, project=project_key, approval_id=approval_id)
            if not deploy_id:
                deploy_id = start_deploy_record(
                    self._db,
                    deploy_type=deploy_type,
                    project=project_key,
                    tag=tag,
                    image=image,
                    triggered_by=triggered_by,
                    deploy_note=deploy_note,
                    params_json=params_json,
                    rollback_type=rollback_type,
                    approval_id=approval_id,
                    artifact_digest=digest,
                )
        except ValueError as e:
            # 唯一索引冲突(同项目已有 running)= busy,包装为与 K8S 路径一致的语义异常
            raise ValidationError(str(e), error_key="errors.deploy_busy") from e
        deploy_run_manager.register(deploy_id)
        set_cancel_checker(cancel_checker_for(self._db, deploy_id))

        results = []
        stage_times = []
        total = len(targets)
        is_batch = total > 1
        started = time.time()

        def _skip_remaining(start_index: int) -> None:
            """失败即停:把未执行的目标标记为 skipped,保留完整的目标清单."""
            for sid2, t2 in targets[start_index:]:
                results.append(
                    {
                        "server_id": sid2,
                        "host": t2.host,
                        "status": "skipped",
                        "output": "Skipped: fail_fast is enabled and an earlier target failed.",
                    }
                )
                stage_times.append({"server_id": sid2, "host": t2.host, "status": "skipped", "duration_ms": 0})
            if callback:
                callback(S("deploy_log.fail_fast_skip", count=total - start_index))

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
                    if fail_fast and i + 1 < total:
                        _skip_remaining(i + 1)
                        break
                    continue

                try:
                    r = deployer.deploy(target, image, project_key, tag, callback=callback)
                    results.append({"server_id": sid, "host": target.host, "status": r.status, "output": r.output})
                except DeployCancelled:
                    raise
                except Exception as e:
                    logger.error("Deploy service failed", exc_info=e)
                    results.append({"server_id": sid, "host": target.host, "status": "failed", "output": str(e)})

                # ── 可选:目标机侧验收命令,失败即视为该目标部署失败 ──
                probe_cmd = (health_cmd or "").strip()
                if probe_cmd and results[-1]["status"] == "ok":
                    if callback:
                        callback(S("deploy_log.probe_start", cmd=probe_cmd))
                    probe_ok, probe_out = run_remote_probe(target, probe_cmd)
                    if probe_ok:
                        if callback:
                            callback(S("deploy_log.probe_ok"))
                    else:
                        results[-1]["status"] = "failed"
                        results[-1]["output"] = (f"{results[-1]['output']}\n\n[probe] {probe_out}")[
                            : settings.log_truncate_chars
                        ]
                        if callback:
                            callback(S("deploy_log.probe_failed", output=probe_out))

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
                if fail_fast and results[-1]["status"] != "ok" and i + 1 < total:
                    _skip_remaining(i + 1)
                    break

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
                project=project_key,
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
                label = "docker" if deploy_type == "compose" else "ssh"
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
                project=project_key,
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
                project=project_key,
            )
            raise
        finally:
            deploy_run_manager.unregister(deploy_id)
            clear_cancel_checker()

    def list_logs(self, project: str = "", page: int = 1, page_size: int = 15) -> dict:
        """查询部署记录(分页)"""
        page = max(page, 1)
        page_size = max(min(page_size, 100), 1)
        offset = (page - 1) * page_size
        cols = (
            "id, project, tag, image, deploy_type, target, status, output, triggered_by, "
            "deploy_note, duration_ms, lock_key, rollback_type, artifact_id, artifact_digest, "
            "approval_id, runner, heartbeat_at, created_at"
        )
        with self._db.conn() as conn:
            if project:
                total = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM cd_deploy_logs WHERE project=?",
                    (project,),
                ).fetchone()["cnt"]
                rows = conn.execute(
                    f"SELECT {cols}, id AS deploy_id FROM cd_deploy_logs WHERE project=? ORDER BY id DESC LIMIT ? OFFSET ?",  # nosec
                    (project, page_size, offset),
                ).fetchall()
            else:
                total = conn.execute("SELECT COUNT(*) AS cnt FROM cd_deploy_logs").fetchone()["cnt"]
                rows = conn.execute(
                    f"SELECT {cols}, id AS deploy_id FROM cd_deploy_logs ORDER BY id DESC LIMIT ? OFFSET ?",  # nosec
                    (page_size, offset),
                ).fetchall()
            items = [dict(r) for r in rows]
            # 批量挂载审批信息(approval_id>0 的记录),一次查询代替逐行查
            approval_ids = list({r["approval_id"] for r in items if r.get("approval_id")})
            if approval_ids:
                ph = ",".join("?" * len(approval_ids))
                arows = conn.execute(
                    f"SELECT id, approver, approved_at, status FROM cd_approvals WHERE id IN ({ph})",  # nosec
                    approval_ids,
                ).fetchall()
                # 带 status:前端日志页审批徽章按状态渲染(已批准/待审批/已撤销/已驳回)
                amap = {
                    r["id"]: {
                        "approver": r.get("approver", ""),
                        "approved_at": r.get("approved_at", ""),
                        "status": r.get("status", ""),
                    }
                    for r in arows
                }
                for r in items:
                    if r.get("approval_id") and r["approval_id"] in amap:
                        r["approval"] = amap[r["approval_id"]]
            return {
                "items": items,
                "total": total,
                "page": page,
                "page_size": page_size,
                "total_pages": max((total + page_size - 1) // page_size, 1),
            }
