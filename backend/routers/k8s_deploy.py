"""K8S 部署路由 — kubectl SSH / Argo CD / Flux CD / Helm"""

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from backend.database import Database
from backend.auth import get_db, require_perm
from backend.services.ci_service import CiService
from backend.services.notification import notify_deploy
from backend.crypto import decrypt
from backend.config import settings
from backend.exceptions import ValidationError, NotFoundError, AppException

from backend.deployers.k8s_kubectl import deploy_kubectl
from backend.deployers.k8s_argocd import deploy_argocd
from backend.deployers.k8s_helm import deploy_helm
from backend.deployers.k8s_fluxcd import deploy_fluxcd

router = APIRouter(prefix="/api", tags=["k8s_deploy"])


class K8sDeployRequest(BaseModel):
    project: str
    tag: str
    cd_type: str = "kubectl"   # kubectl | argocd | fluxcd | helm
    cluster_id: int = 0
    path: str = ""              # YAML path for kubectl mode
    api_url: str = ""           # Argo CD / Flux API base
    k8s_ns: str = ""            # 留空不传 -n，namespace 在 YAML 中声明
    bot_id: int = 0
    lang: str = "en"           # 前端当前语言 en/zh


def _resolve_cluster(db, req):
    """解析集群信息，返回 (host, port, user, pwd, ssh_key) 或抛异常"""
    if req.cluster_id:
        with db.conn() as conn:
            srv = conn.execute("SELECT * FROM cd_servers WHERE id=?", (req.cluster_id,)).fetchone()
        if not srv:
            raise NotFoundError("集群不存在", error_key="errors.cluster_not_found")
        host, port, user, pwd = srv["host"], srv["port"], srv["user"], decrypt(srv["password"] or "")
        ssh_key = decrypt(srv["ssh_key"] or "")
        return host, port, user, pwd, ssh_key
    raise ValidationError("请选择目标集群", error_key="errors.select_cluster")


def _resolve_image(db, req):
    """解析镜像地址，返回 (image, project_key, project_short) 或抛异常"""
    svc = CiService(db)
    harbor_repo = svc.resolve_harbor_repo(req.project)
    if not harbor_repo:
        raise ValidationError(f"项目 '{req.project}' 未配置 harbor_repository", error_key="errors.no_harbor_repo", error_params={"project": req.project})
    image = f"{settings.harbor_registry}/{harbor_repo}:{req.tag}"
    project_key = svc.resolve_project_key(req.project) or req.project
    project_short = project_key.split("/")[-1]
    return image, project_key, project_short


def _record_deploy(db, project_key, tag, image, cd_type, host, ok, output):
    """记录部署日志到数据库"""
    with db.conn() as conn:
        conn.execute(
            "INSERT INTO cd_deploy_logs (project,tag,image,deploy_type,target,status,output) VALUES (?,?,?,?,?,?,?)",
            (project_key, tag, image, f"k8s/{cd_type}", host,
             "ok" if ok else "failed",
             output[:settings.log_truncate_chars] if output else ""),
        )


def _notify_k8s(db, bot_id, tag, project_key, host, cd_type, image, ok, lang="en"):
    status = "✅ Success" if ok else "❌ Failed"
    notify_deploy(db, bot_id, tag, project_key, image, status, cd_type, [f"k8s[{host}]"], lang=lang)


@router.post("/deploy-k8s")
def deploy_k8s(
    req: K8sDeployRequest,
    db: Database = Depends(get_db),
    _user: dict = Depends(require_perm("cd.deploy.k8s")),
):
    image, project_key, project_short = _resolve_image(db, req)
    host, port, user, pwd, ssh_key = _resolve_cluster(db, req)

    # 路由到对应 deployer
    if req.cd_type == "argocd":
        result = deploy_argocd(req, image, project_short, host, pwd)
    elif req.cd_type == "fluxcd":
        result = deploy_fluxcd(req, image, project_short, host, pwd, ssh_key)
    elif req.cd_type == "helm":
        result = deploy_helm(req, image, project_short, host, port, user, pwd, ssh_key)
    else:
        result = deploy_kubectl(req, image, project_short, host, port, user, pwd, ssh_key)

    # 记录日志 + 通知
    _record_deploy(db, project_key, req.tag, image, req.cd_type, host, result["success"], result["output"])
    _notify_k8s(db, req.bot_id, req.tag, project_key, host, req.cd_type, image, result["success"], req.lang)

    return result


@router.post("/deploy-k8s-stream")
async def deploy_k8s_stream(
    req: K8sDeployRequest,
    db: Database = Depends(get_db),
    _user: dict = Depends(require_perm("cd.deploy.k8s")),
):
    """K8S 实时部署（SSE 流式推送）"""
    import asyncio
    import queue
    import threading

    log_queue = queue.Queue()
    deploy_result = {}

    try:
        image, project_key, project_short = _resolve_image(db, req)
    except AppException as e:
        async def err_no_repo():
            yield f"data: ERROR:{e.message}\n\n"
        return StreamingResponse(err_no_repo(), media_type="text/event-stream")

    try:
        host, port, user, pwd, ssh_key = _resolve_cluster(db, req)
    except AppException as e:
        async def err_no_cluster():
            yield f"data: ERROR:{e.message}\n\n"
        return StreamingResponse(err_no_cluster(), media_type="text/event-stream")

    def do_deploy():
        nonlocal deploy_result
        try:
            def log_callback(message):
                log_queue.put(message)

            if req.cd_type == "argocd":
                result = deploy_argocd(req, image, project_short, host, pwd, callback=log_callback)
            elif req.cd_type == "fluxcd":
                result = deploy_fluxcd(req, image, project_short, host, pwd, ssh_key, callback=log_callback)
            elif req.cd_type == "helm":
                result = deploy_helm(req, image, project_short, host, port, user, pwd, ssh_key, callback=log_callback)
            else:
                result = deploy_kubectl(req, image, project_short, host, port, user, pwd, ssh_key, callback=log_callback)

            deploy_result = {"success": True, "data": result}

            _record_deploy(db, project_key, req.tag, image, req.cd_type, host, result["success"], result["output"])
            _notify_k8s(db, req.bot_id, req.tag, project_key, host, req.cd_type, image, result["success"], req.lang)
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
            yield f"data: END:{0}:{str(result['success']).lower()}:Deployment complete\n\n"
        else:
            yield f"data: ERROR:{deploy_result.get('error', 'Deploy failed')}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
