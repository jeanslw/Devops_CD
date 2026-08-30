"""K8S 部署执行核心 — 从 routers/k8s_deploy.py 抽取，供路由与审批/回滚执行器共用。

避免 services 层反向依赖 routers 层。包含：
- K8sDeployRequest 请求模型
- 集群 / 镜像解析
- 部署核心流程（并发锁 + running 记录 + 取消 + 耗时 + 参数快照）
- 通知
"""

import json
import logging
import time

from pydantic import BaseModel

from backend.config import settings
from backend.crypto import decrypt
from backend.deploy_run import (
    DeployCancelled,
    clear_cancel_checker,
    deploy_run_manager,
    find_running_deploy,
    finish_deploy_record,
    set_cancel_checker,
    start_deploy_record,
)
from backend.deployers.registry import deployer_registry
from backend.exceptions import NotFoundError, ValidationError
from backend.services.ci_service import CiService
from backend.services.notification import notify_deploy

logger = logging.getLogger(__name__)


class K8sDeployRequest(BaseModel):
    project: str
    tag: str
    cd_type: str = "kubectl"  # kubectl | argocd | fluxcd | helm
    cluster_id: int = 0
    path: str = ""  # YAML path for kubectl mode
    api_url: str = ""  # Argo CD / Flux API base
    k8s_ns: str = ""  # 留空不传 -n，namespace 在 YAML 中声明
    deploy_note: str = ""  # 部署说明（记录到 cd_deploy_logs.deploy_note）
    bot_id: int = 0
    lang: str = "en"  # 前端当前语言 en/zh


def _resolve_cluster(db, req) -> tuple[str, int, str, str, str]:
    """解析集群信息，返回 (host, port, user, pwd, ssh_key) 或抛异常"""
    if req.cluster_id:
        with db.conn() as conn:
            srv = conn.execute("SELECT * FROM cd_servers WHERE id=?", (req.cluster_id,)).fetchone()
        if not srv:
            raise NotFoundError("集群不存在", error_key="errors.cluster_not_found")
        host = str(srv["host"])
        port = int(srv["port"])
        user = str(srv["user"])
        pwd = decrypt(srv["password"] or "")
        ssh_key = decrypt(srv["ssh_key"] or "")
        return host, port, user, pwd, ssh_key
    raise ValidationError("请选择目标集群", error_key="errors.select_cluster")


def _resolve_image(db, req):
    """解析镜像地址，返回 (image, project_key, project_short) 或抛异常"""
    svc = CiService(db)
    harbor_repo = svc.resolve_harbor_repo(req.project)
    if not harbor_repo:
        raise ValidationError(
            f"项目 '{req.project}' 未配置 harbor_repository",
            error_key="errors.no_harbor_repo",
            error_params={"project": req.project},
        )
    image = f"{settings.harbor_registry}/{harbor_repo}:{req.tag}"
    project_key = svc.resolve_project_key(req.project) or req.project
    project_short = project_key.split("/")[-1]
    return image, project_key, project_short


def _deploy_k8s_core(
    db, req, user, image, project_key, project_short, host, port, user_srv, pwd, ssh_key, callback=None, rollback=False
):
    """执行 K8S 部署核心流程：并发锁 + running 记录 + 取消 + 耗时 + 参数快照。

    返回 deployer 的结果 dict（含 deploy_id）；被取消时返回 {"success": False, "cancelled": True}。
    通知由调用方负责（区分同步/流式），避免阻塞 SSE 结束信号。

    rollback=True 时走 deployer 原生回滚（kubectl rollout undo / helm rollback），
    而非重放旧 tag 的普通部署。
    """
    # ── 并发锁：同一项目同时只允许一个进行中部署 ──
    running = find_running_deploy(db, project_key)
    if running:
        raise ValidationError(
            f"项目 '{project_key}' 已有部署进行中 (deploy #{running['deploy_id']})，请等待完成或取消后再试",
            error_key="errors.deploy_busy",
        )

    deployer = deployer_registry.create(f"k8s/{req.cd_type}")
    if deployer is None:
        raise ValidationError(f"不支持的 CD 类型: {req.cd_type}", error_key="errors.unsupported_cd_type")

    deploy_type = f"k8s/{req.cd_type}"
    # 参数快照（含 deploy_type 路由判别），供回滚重放
    params_json = json.dumps({"deploy_type": deploy_type, **req.model_dump()}, ensure_ascii=False)
    try:
        deploy_id = start_deploy_record(
            db,
            deploy_type=deploy_type,
            project=project_key,
            tag=req.tag,
            image=image,
            triggered_by=user.get("username", ""),
            deploy_note=req.deploy_note,
            target=host,
            params_json=params_json,
        )
    except ValueError as e:
        raise ValidationError(str(e), error_key="errors.deploy_busy") from e
    deploy_run_manager.register(deploy_id)
    set_cancel_checker(lambda: deploy_run_manager.is_cancelled(deploy_id))
    started = time.time()

    try:
        if rollback:
            result = deployer.rollback(req, project_short, host, port, user_srv, pwd, ssh_key, callback=callback)
        else:
            result = deployer.deploy(req, image, project_short, host, port, user_srv, pwd, ssh_key, callback=callback)
        ok = bool(result.get("success"))
        status = "ok" if ok else "failed"
        duration_ms = int((time.time() - started) * 1000)
        finish_deploy_record(
            db,
            deploy_id,
            status=status,
            target=host,
            output=result.get("output", "") or "",
            duration_ms=duration_ms,
            stage_times=[{"host": host, "status": status, "duration_ms": duration_ms}],
        )
        result["deploy_id"] = deploy_id
        return result
    except DeployCancelled:
        duration_ms = int((time.time() - started) * 1000)
        finish_deploy_record(
            db,
            deploy_id,
            status="terminated",
            target=host,
            output="Deployment cancelled by user",
            duration_ms=duration_ms,
            stage_times=[{"host": host, "status": "terminated", "duration_ms": duration_ms}],
        )
        return {"success": False, "output": "Deployment cancelled by user", "cancelled": True, "deploy_id": deploy_id}
    except Exception as e:
        logger.error("K8s deploy core failed", exc_info=e)
        duration_ms = int((time.time() - started) * 1000)
        finish_deploy_record(
            db,
            deploy_id,
            status="failed",
            target=host,
            output=str(e)[: settings.log_truncate_chars],
            duration_ms=duration_ms,
            stage_times=[{"host": host, "status": "failed", "duration_ms": duration_ms}],
        )
        raise
    finally:
        deploy_run_manager.unregister(deploy_id)
        clear_cancel_checker()


def _notify_k8s(db, bot_id, tag, project_key, host, cd_type, image, ok, lang="en"):
    status = "✅ Success" if ok else "❌ Failed"
    notify_deploy(db, bot_id, tag, project_key, image, status, cd_type, [f"k8s[{host}]"], lang=lang)
