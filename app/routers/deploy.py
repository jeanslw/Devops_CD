"""部署路由"""

from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import StreamingResponse
from app.database import Database
from app.auth import get_db, verify_token
from app.models import DeployRequest
from app.services.deploy_service import DeployService
from app.deployers import DeployTarget
from app.deployers.base import ssh_connect
from app.config import settings

router = APIRouter(prefix="/api", tags=["deploy"])


@router.post("/deploy")
def deploy(
    req: DeployRequest,
    db: Database = Depends(get_db),
    username: str = Depends(verify_token),
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
            bot_id=req.bot_id,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/stop")
def stop(
    req: DeployRequest,
    db: Database = Depends(get_db),
    username: str = Depends(verify_token),
):
    """停止服务"""
    if not req.server_ids:
        raise HTTPException(400, "请选择目标服务器")
    conn = db.conn()
    try:
        sid = int(req.server_ids.split(",")[0])
    except (ValueError, IndexError):
        raise HTTPException(400, "请选择目标服务器")
    srv = conn.execute("SELECT * FROM cd_servers WHERE id=?", (sid,)).fetchone()
    if not srv:
        raise HTTPException(400, "服务器不存在")
    target = DeployTarget(
        host=srv["host"], port=srv["port"], user=srv["user"],
        password=srv["password"] or "", path=req.target_path,
    )

    if req.deploy_type == "compose":
        cmd = f"cd {req.target_path} && docker compose down"
    elif req.deploy_type == "k8s":
        ns = "default"
        cmd = f"kubectl delete deployment/{req.project} -n {ns}"
    else:
        cmd = f"docker stop {req.project} 2>/dev/null; docker rm {req.project} 2>/dev/null"

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
    username: str = Depends(verify_token),
):
    """K8S 停止: kubectl delete -f YAML 或 kubectl delete deployment"""
    if not req.server_ids:
        raise HTTPException(400, "请选择目标集群")
    try:
        sid = int(req.server_ids.split(",")[0])
    except (ValueError, IndexError):
        raise HTTPException(400, "请选择目标集群")
    conn = db.conn()
    srv = conn.execute("SELECT * FROM cd_servers WHERE id=?", (sid,)).fetchone()
    if not srv:
        raise HTTPException(400, "集群不存在")

    target = DeployTarget(host=srv["host"], port=srv["port"], user=srv["user"], password=srv["password"] or "")
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
    username: str = Depends(verify_token),
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
                bot_id=req.bot_id,
                callback=log_callback,
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
            yield f"data: ERROR:{deploy_result.get('error', '部署失败')}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
