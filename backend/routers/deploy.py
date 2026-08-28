"""部署路由"""

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from backend.auth import enforce_deploy_perm, get_current_user, get_db, require_perm
from backend.crypto import decrypt
from backend.database import Database
from backend.deploy_run import mark_deploy_cancelled
from backend.deployers import DeployTarget
from backend.deployers.registry import deployer_registry
from backend.exceptions import NotFoundError, ValidationError
from backend.models import CancelRequest, DeployRequest
from backend.services.deploy_service import DeployService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["deploy"])


@router.post("/deploy")
def deploy(
    req: DeployRequest,
    db: Database = Depends(get_db),
    user: dict = Depends(require_perm("cd.deploy-manage")),
):
    # K8S 子模式必须走 /api/deploy-k8s，禁止混进 SSH/Compose 路线（签名不兼容）
    if req.deploy_type.startswith("k8s/"):
        raise ValidationError(
            f"部署类型 '{req.deploy_type}' 请使用 K8S 专用接口 /api/deploy-k8s",
            error_key="errors.wrong_deploy_api",
        )
    # 按具体 deploy_type 做二次权限校验（防御深度：service 层也会再查一次）
    enforce_deploy_perm(user, req.deploy_type)
    svc = DeployService(db)
    try:
        return svc.execute(
            project=req.project,
            tag=req.tag,
            deploy_type=req.deploy_type,
            server_ids=req.server_ids,
            target_path=req.target_path,
            deploy_mode=req.deploy_mode,
            commands=req.commands,
            yaml_content=req.yaml_content,
            k8s_ns=req.k8s_ns,
            k8s_deploy=req.k8s_deploy,
            k8s_container=req.k8s_container,
            env_file=req.env_file,
            deploy_note=req.deploy_note,
            bot_id=req.bot_id,
            lang=req.lang,
            user=user,
        )
    except ValueError as e:
        logger.error("Deploy validation failed", exc_info=e)
        raise ValidationError(str(e), error_key="errors.deploy_validation") from e


@router.post("/deploy/cancel")
def deploy_cancel(
    req: CancelRequest,
    db: Database = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """取消进行中的部署 — 按 deploy_id 或 project 定位 running 记录。"""
    if not req.deploy_id and not req.project:
        raise ValidationError("请提供 deploy_id 或 project", error_key="errors.deploy_validation")

    with db.conn() as conn:
        if req.deploy_id:
            row = conn.execute(
                "SELECT id AS deploy_id, deploy_type FROM cd_deploy_logs "
                "WHERE id=? AND status='running' ORDER BY id DESC LIMIT 1",
                (req.deploy_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id AS deploy_id, deploy_type FROM cd_deploy_logs "
                "WHERE project=? AND status='running' ORDER BY id DESC LIMIT 1",
                (req.project,),
            ).fetchone()

    if not row:
        return {"success": False, "message": "No running deployment found (may already be finished)"}

    # 权限校验：按该部署的 deploy_type 判断（k8s 记录形如 "k8s/kubectl"）
    deploy_type = row["deploy_type"] or "ssh"
    if deploy_type.startswith("k8s/"):
        enforce_deploy_perm(user, "k8s", deploy_type.split("/", 1)[1])
    else:
        enforce_deploy_perm(user, deploy_type)

    return mark_deploy_cancelled(db, row["deploy_id"])


@router.post("/stop")
def stop(
    req: DeployRequest,
    db: Database = Depends(get_db),
    user: dict = Depends(require_perm("cd.deploy-manage")),
):
    """停止服务 — 按 deploy_type 分发到对应 Deployer"""
    enforce_deploy_perm(user, req.deploy_type)
    if not req.server_ids:
        raise ValidationError("请选择目标服务器", error_key="errors.select_server")
    with db.conn() as conn:
        try:
            sid = int(req.server_ids.split(",")[0])
        except (ValueError, IndexError) as e:
            raise ValidationError("请选择目标服务器", error_key="errors.select_server") from e
        srv = conn.execute("SELECT * FROM cd_servers WHERE id=?", (sid,)).fetchone()
    if not srv:
        raise NotFoundError("服务器不存在", error_key="errors.server_not_found")

    target = DeployTarget(
        host=srv["host"],
        port=srv["port"],
        user=srv["user"],
        password=decrypt(srv["password"] or ""),
        ssh_key=decrypt(srv["ssh_key"] or ""),
    )
    deployer = deployer_registry.create(req.deploy_type)
    if deployer is None:
        raise ValidationError(f"不支持的部署类型: {req.deploy_type}", error_key="errors.unsupported_deploy_type")

    return deployer.stop(
        target=target,
        project=req.project,
        tag=req.tag,
        commands=req.commands,
        target_path=req.target_path,
        k8s_ns=req.k8s_ns,
    )


@router.post("/stop-k8s")
def stop_k8s(
    req: DeployRequest,
    db: Database = Depends(get_db),
    user: dict = Depends(require_perm("cd.deploy.k8s")),
):
    """K8S 停止 — 按 cd_type 分发到对应 K8S 子模式 Deployer"""
    enforce_deploy_perm(user, "k8s", req.cd_type)
    if not req.server_ids:
        raise ValidationError("请选择目标集群", error_key="errors.select_cluster")
    try:
        sid = int(req.server_ids.split(",")[0])
    except (ValueError, IndexError) as e:
        raise ValidationError("请选择目标集群", error_key="errors.select_cluster") from e
    with db.conn() as conn:
        srv = conn.execute("SELECT * FROM cd_servers WHERE id=?", (sid,)).fetchone()
    if not srv:
        raise NotFoundError("集群不存在", error_key="errors.cluster_not_found")

    host, port, user_srv, pwd = srv["host"], srv["port"], srv["user"], decrypt(srv["password"] or "")
    ssh_key = decrypt(srv["ssh_key"] or "")

    deployer = deployer_registry.create(f"k8s/{req.cd_type}")
    if deployer is None:
        raise ValidationError(f"不支持的 CD 类型: {req.cd_type}", error_key="errors.unsupported_cd_type")

    return deployer.stop(req=req, project=req.project, host=host, port=port, user=user_srv, pwd=pwd, ssh_key=ssh_key)


@router.post("/deploy-stream")
async def deploy_stream(
    req: DeployRequest,
    db: Database = Depends(get_db),
    user: dict = Depends(require_perm("cd.deploy-manage")),
):
    """实时部署（SSE 流式推送）"""
    # K8S 子模式必须走 /api/deploy-k8s-stream，禁止混进 SSH/Compose 路线（签名不兼容）
    if req.deploy_type.startswith("k8s/"):
        msg = f"部署类型 '{req.deploy_type}' 请使用 K8S 专用接口 /api/deploy-k8s-stream"

        async def _err():
            yield f"retry: 3000\ndata: ERROR:{msg}\n\n"

        return StreamingResponse(_err(), media_type="text/event-stream")
    enforce_deploy_perm(user, req.deploy_type)
    import asyncio
    import queue
    import threading

    log_queue = queue.Queue()
    deploy_result = {}

    def do_deploy():
        nonlocal deploy_result
        svc = DeployService(db)

        def log_callback(message):
            log_queue.put(message)

        try:
            result = svc.execute(
                project=req.project,
                tag=req.tag,
                deploy_type=req.deploy_type,
                server_ids=req.server_ids,
                target_path=req.target_path,
                deploy_mode=req.deploy_mode,
                commands=req.commands,
                yaml_content=req.yaml_content,
                k8s_ns=req.k8s_ns,
                k8s_deploy=req.k8s_deploy,
                k8s_container=req.k8s_container,
                env_file=req.env_file,
                deploy_note=req.deploy_note,
                bot_id=req.bot_id,
                callback=log_callback,
                lang=req.lang,
                user=user,
            )
            deploy_result = {"success": True, "data": result}
        except ValueError as e:
            logger.error("Deploy stream validation failed", exc_info=e)
            deploy_result = {"success": False, "error": f"Deploy parameter error: {e}"}
        except Exception as e:
            logger.error("Deploy stream failed", exc_info=e)
            deploy_result = {"success": False, "error": str(e)}
        finally:
            log_queue.put(None)

    threading.Thread(target=do_deploy, daemon=True).start()

    async def event_stream():
        while True:
            try:
                msg = await asyncio.to_thread(log_queue.get, timeout=30)
                if msg is None:
                    break
                # 多行消息需要每行都加 data: 前缀，否则 SSE 只解析第一行
                safe_msg = msg.replace("\n", "\ndata: ")
                yield f"data: {safe_msg}\n\n"
            except queue.Empty:
                yield "data: .\n\n"
                await asyncio.sleep(1)

        if deploy_result.get("success"):
            # 实时日志已通过 STATUS: 事件流式发出，END 只携带成功标志
            result = deploy_result["data"]
            yield f"data: END:{str(result.get('success', False)).lower()}\n\n"
        else:
            yield f"data: ERROR:{deploy_result.get('error', 'Deploy failed')}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
