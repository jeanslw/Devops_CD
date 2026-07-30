"""部署路由"""

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from backend.database import Database
from backend.auth import get_db, verify_token, require_perm
from backend.models import DeployRequest
from backend.services.deploy_service import DeployService
from backend.deployers import DeployTarget
from backend.deployers.base import ssh_connect
from backend.crypto import decrypt
from backend.config import settings
from backend.exceptions import ValidationError, NotFoundError

router = APIRouter(prefix="/api", tags=["deploy"])


@router.post("/deploy")
def deploy(
    req: DeployRequest,
    db: Database = Depends(get_db),
    _user: dict = Depends(require_perm("cd.deploy-manage")),
):
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
            bot_id=req.bot_id,
            lang=req.lang,
        )
    except ValueError as e:
        raise ValidationError(str(e))


@router.post("/stop")
def stop(
    req: DeployRequest,
    db: Database = Depends(get_db),
    _user: dict = Depends(require_perm("cd.deploy-manage")),
):
    """停止服务"""
    if not req.server_ids:
        raise ValidationError("请选择目标服务器")
    with db.conn() as conn:
        try:
            sid = int(req.server_ids.split(",")[0])
        except (ValueError, IndexError):
            raise ValidationError("请选择目标服务器")
        srv = conn.execute("SELECT * FROM cd_servers WHERE id=?", (sid,)).fetchone()
    if not srv:
        raise NotFoundError("服务器不存在")
    target = DeployTarget(
        host=srv["host"], port=srv["port"], user=srv["user"],
        password=decrypt(srv["password"] or ""), ssh_key=decrypt(srv["ssh_key"] or ""), path=req.target_path,
    )

    if req.deploy_type == "compose":
        cmd = f"cd {req.target_path} && docker-compose down"
    elif req.deploy_type == "k8s":
        ns = req.k8s_ns or "default"
        cmd = f"kubectl delete deployment/{req.project} -n {ns}"
    elif req.commands:
        # SSH 自定义停止命令
        image = f"{req.project}:{req.tag}"
        image_name = req.project.split("/")[-1]
        cmd = req.commands.replace("{image}", image).replace("{image_name}", image_name).replace("{tag}", req.tag).replace("{project}", req.project)
    else:
        raise ValidationError("SSH 模式需要填写停止命令，或改用 compose/k8s 部署类型")

    try:
        ssh = ssh_connect(target, settings.ssh_timeout)
        _, stdout, stderr = ssh.exec_command(cmd)
        out = stdout.read().decode().strip()
        err = stderr.read().decode().strip()
        ssh.close()
        return {"success": True, "output": (err or out)[:settings.log_truncate_chars]}
    except Exception as e:
        return {"success": False, "output": str(e)}


@router.post("/stop-k8s")
def stop_k8s(
    req: DeployRequest,
    db: Database = Depends(get_db),
    _user: dict = Depends(require_perm("cd.deploy.k8s")),
):
    """K8S 停止: kubectl delete -f YAML 或 kubectl delete deployment"""
    if not req.server_ids:
        raise ValidationError("请选择目标集群")
    try:
        sid = int(req.server_ids.split(",")[0])
    except (ValueError, IndexError):
        raise ValidationError("请选择目标集群")
    with db.conn() as conn:
        srv = conn.execute("SELECT * FROM cd_servers WHERE id=?", (sid,)).fetchone()
    if not srv:
        raise NotFoundError("集群不存在")

    target = DeployTarget(host=srv["host"], port=srv["port"], user=srv["user"], password=decrypt(srv["password"] or ""), ssh_key=decrypt(srv["ssh_key"] or ""))
    project = req.project

    cmd = f"kubectl delete -f {req.target_path}" if req.target_path else f"kubectl delete deployment/{project}"
    try:
        ssh = ssh_connect(target, settings.ssh_timeout)
        _, stdout, stderr = ssh.exec_command(cmd)
        out = stdout.read().decode().strip()
        err = stderr.read().decode().strip()
        ssh.close()
        return {"success": True, "output": (err or out)[:settings.log_truncate_chars]}
    except Exception as e:
        return {"success": False, "output": str(e)}


@router.post("/deploy-stream")
async def deploy_stream(
    req: DeployRequest,
    db: Database = Depends(get_db),
    _user: dict = Depends(require_perm("cd.deploy-manage")),
):
    """实时部署（SSE 流式推送）"""
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
                bot_id=req.bot_id,
                callback=log_callback,
                lang=req.lang,
            )
            deploy_result = {"success": True, "data": result}
        except ValueError as e:
            deploy_result = {"success": False, "error": str(e)}
        except Exception as e:
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
                yield f"data: {msg}\n\n"
            except queue.Empty:
                yield "data: .\n\n"
                await asyncio.sleep(1)

        if deploy_result.get("success"):
            result = deploy_result["data"]
            yield f"data: END:{result['deploy_id']}:{str(result['success']).lower()}:{result['message']}\n\n"
        else:
            yield f"data: ERROR:{deploy_result.get('error', 'Deploy failed')}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
