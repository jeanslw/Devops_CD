"""部署路由"""

from fastapi import APIRouter, HTTPException, Depends
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
    sid = int(req.server_ids.split(",")[0])
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
