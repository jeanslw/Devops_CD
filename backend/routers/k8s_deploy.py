"""K8S 部署路由 — kubectl SSH / Argo CD / Flux CD / Helm"""

import shlex

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from backend.database import Database
from backend.auth import get_db, require_perm, enforce_deploy_perm
from backend.services.ci_service import CiService
from backend.services.notification import notify_deploy
from backend.crypto import decrypt
from backend.config import settings
from backend.exceptions import ValidationError, NotFoundError, AppException

from backend.deployers.registry import deployer_registry

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


def _record_deploy(db, project_key, tag, image, cd_type, host, ok, output, triggered_by: str = ""):
    """记录部署日志到数据库"""
    with db.conn() as conn:
        conn.execute(
            "INSERT INTO cd_deploy_logs (project,tag,image,deploy_type,target,status,output,triggered_by) VALUES (?,?,?,?,?,?,?,?)",
            (project_key, tag, image, f"k8s/{cd_type}", host,
             "ok" if ok else "failed",
             output[:settings.log_truncate_chars] if output else "",
             triggered_by or ""),
        )


def _notify_k8s(db, bot_id, tag, project_key, host, cd_type, image, ok, lang="en"):
    status = "✅ Success" if ok else "❌ Failed"
    notify_deploy(db, bot_id, tag, project_key, image, status, cd_type, [f"k8s[{host}]"], lang=lang)


# ── 预检：部署前校验 YAML 名称与 K8S 存量 ──

class K8sDeployCheckRequest(BaseModel):
    project: str
    cd_type: str = "kubectl"
    cluster_id: int = 0
    path: str = ""
    api_url: str = ""
    k8s_ns: str = ""


def _read_yaml(path: str, ssh=None) -> str:
    """读取 YAML 内容：HTTP URL 直接拉，文件路径走 SSH cat"""
    if path.startswith("http://") or path.startswith("https://"):
        try:
            import requests
            r = requests.get(path, timeout=10)
            if r.ok:
                return r.text
        except Exception:
            pass
    elif ssh:
        try:
            from backend.deployers.k8s_utils import _exec_exit
            out, err, ec = _exec_exit(ssh, f"cat {shlex.quote(path)} 2>/dev/null")
            if ec == 0:
                return out
        except Exception:
            pass
    return ""


def _parse_deployment_name(yaml_content: str) -> str:
    """从多文档 YAML 中提取第一个 Deployment 的 metadata.name"""
    try:
        import yaml as yaml_lib
        for doc in yaml_lib.safe_load_all(yaml_content):
            if isinstance(doc, dict) and doc.get("kind") == "Deployment":
                return doc.get("metadata", {}).get("name", "") or ""
    except Exception:
        pass
    return ""


def _k8s_deployment_exists(ssh, name: str, namespace: str = "") -> bool:
    """SSH 上 kubectl get deployment/{name} 是否存在"""
    try:
        from backend.deployers.k8s_utils import _exec_exit
        ns_flag = f"-n {namespace}" if namespace else ""
        out, err, ec = _exec_exit(
            ssh,
            f"kubectl get deployment/{shlex.quote(name)} {ns_flag} -o name 2>/dev/null",
        )
        return ec == 0 and bool(out)
    except Exception:
        return False


@router.post("/deploy-k8s-check")
def deploy_k8s_check(
    req: K8sDeployCheckRequest,
    db: Database = Depends(get_db),
    user: dict = Depends(require_perm("cd.deploy.k8s")),
):
    """部署前预检：YAML 名称 vs 项目名 + K8S 存量"""
    enforce_deploy_perm(user, "k8s", req.cd_type)
    filter_name = req.project.split("/")[-1]
    result = {
        "ok": True, "exists": False,
        "yaml_deploy_name": "", "project_name": filter_name,
        "warning": "",
    }

    from backend.deployers.base import ssh_connect, DeployTarget

    if not req.path or req.cd_type == "helm":
        if req.cd_type != "fluxcd":
            return result  # Helm chart 目录/无 YAML，不需要预检

        # FluxCD: SSH 发现 Flux 资源名，用于名称对比
        from backend.deployers.k8s_fluxcd import _discover_flux_resource
        try:
            host, port, user, pwd, ssh_key = _resolve_cluster(db, req)
            ssh = ssh_connect(
                DeployTarget(host=host, port=port, user=user, password=pwd, ssh_key=ssh_key),
                settings.ssh_timeout,
            )
            flux_name, flux_kind = _discover_flux_resource(ssh, filter_name, "")
            ssh.close()
        except AppException:
            return result

        if not flux_kind:
            return result  # 没有 Flux 资源，跳过预检

        deploy_name = flux_name
        result["yaml_deploy_name"] = deploy_name

        if deploy_name == filter_name:
            return result  # 完全匹配

        # 查 K8S 存量（用 flux 资源名查 deployment）
        try:
            host, port, user, pwd, ssh_key = _resolve_cluster(db, req)
            ssh = ssh_connect(
                DeployTarget(host=host, port=port, user=user, password=pwd, ssh_key=ssh_key),
                settings.ssh_timeout,
            )
            result["exists"] = _k8s_deployment_exists(ssh, deploy_name, req.k8s_ns)
            ssh.close()
        except AppException:
            pass

        # 构造警告（复用现有逻辑）
        name_low = filter_name.lower()
        yaml_low = deploy_name.lower()
        if name_low in yaml_low or yaml_low in name_low:
            result["warning"] = "warning_soft"
        elif result["exists"]:
            result["warning"] = "warning_severe_existing"
        else:
            result["warning"] = "warning_severe_new"

        return result

    # ── 读 YAML ──
    ssh = None
    try:
        if not (req.path.startswith("http://") or req.path.startswith("https://")):
            host, port, user, pwd, ssh_key = _resolve_cluster(db, req)
            ssh = ssh_connect(
                DeployTarget(host=host, port=port, user=user, password=pwd, ssh_key=ssh_key),
                settings.ssh_timeout,
            )
        yaml_content = _read_yaml(req.path, ssh)
    except AppException:
        return result
    finally:
        if ssh:
            try:
                ssh.close()
            except Exception:
                pass

    if not yaml_content:
        return result

    # ── 解析 Deployment name ──
    # 模板变量 {IMAGE}:{TAG} 会被 yaml 当成 flow mapping，先替换为占位符避免解析失败
    safe_yaml = yaml_content.replace("{IMAGE}:{TAG}", "dummy:dummy")
    safe_yaml = safe_yaml.replace("{IMAGE_NAME}", "dummy")
    safe_yaml = safe_yaml.replace("{IMAGE}", "dummy")
    safe_yaml = safe_yaml.replace("{TAG}", "dummy")
    deploy_name = _parse_deployment_name(safe_yaml)
    if not deploy_name:
        return result

    result["yaml_deploy_name"] = deploy_name

    # 精确匹配 → 无警告
    if deploy_name == filter_name:
        return result

    # ── 查 K8S 存量 ──
    if req.cd_type == "argocd":
        if req.api_url:
            try:
                import requests
                host, _, _, pwd, _ = _resolve_cluster(db, req)
                r = requests.get(
                    f"{req.api_url.rstrip('/')}/api/v1/applications/{deploy_name}",
                    headers={"Authorization": f"Bearer {pwd}"},
                    timeout=10, verify=False,
                )
                result["exists"] = r.status_code == 200
            except Exception:
                pass
    else:
        try:
            host, port, user, pwd, ssh_key = _resolve_cluster(db, req)
            ssh = ssh_connect(
                DeployTarget(host=host, port=port, user=user, password=pwd, ssh_key=ssh_key),
                settings.ssh_timeout,
            )
            result["exists"] = _k8s_deployment_exists(ssh, deploy_name, req.k8s_ns)
            ssh.close()
        except AppException:
            pass

    # ── 构造警告 ──
    name_low = filter_name.lower()
    yaml_low = deploy_name.lower()
    if name_low in yaml_low or yaml_low in name_low:
        result["warning"] = "warning_soft"
    elif result["exists"]:
        result["warning"] = "warning_severe_existing"
    else:
        result["warning"] = "warning_severe_new"

    return result


@router.post("/deploy-k8s")
def deploy_k8s(
    req: K8sDeployRequest,
    db: Database = Depends(get_db),
    user: dict = Depends(require_perm("cd.deploy.k8s")),
):
    enforce_deploy_perm(user, "k8s", req.cd_type)
    image, project_key, project_short = _resolve_image(db, req)
    host, port, user_srv, pwd, ssh_key = _resolve_cluster(db, req)

    # 路由到对应 deployer（统一通过注册表）
    deployer = deployer_registry.create(f"k8s/{req.cd_type}")
    if deployer is None:
        raise ValidationError(f"不支持的 CD 类型: {req.cd_type}", error_key="errors.unsupported_cd_type")
    result = deployer.deploy(req, image, project_short, host, port, user_srv, pwd, ssh_key)

    # 记录日志 + 通知
    _record_deploy(db, project_key, req.tag, image, req.cd_type, host, result["success"], result["output"], user.get("username", ""))
    _notify_k8s(db, req.bot_id, req.tag, project_key, host, req.cd_type, image, result["success"], req.lang)

    return result


@router.post("/deploy-k8s-stream")
async def deploy_k8s_stream(
    req: K8sDeployRequest,
    db: Database = Depends(get_db),
    user: dict = Depends(require_perm("cd.deploy.k8s")),
):
    """K8S 实时部署（SSE 流式推送）"""
    enforce_deploy_perm(user, "k8s", req.cd_type)
    import asyncio
    import queue
    import threading

    log_queue = queue.Queue()
    deploy_result = {}

    try:
        image, project_key, project_short = _resolve_image(db, req)
    except AppException as e:
        async def err_no_repo():
            yield f"retry: 3000\ndata: ERROR:{e.message}\n\n"
        return StreamingResponse(err_no_repo(), media_type="text/event-stream")

    try:
        host, port, user_srv, pwd, ssh_key = _resolve_cluster(db, req)
    except AppException as e:
        async def err_no_cluster():
            yield f"retry: 3000\ndata: ERROR:{e.message}\n\n"
        return StreamingResponse(err_no_cluster(), media_type="text/event-stream")

    def do_deploy():
        nonlocal deploy_result
        try:
            def log_callback(message):
                log_queue.put(message)

            deployer = deployer_registry.create(f"k8s/{req.cd_type}")
            if deployer is None:
                raise ValidationError(f"不支持的 CD 类型: {req.cd_type}", error_key="errors.unsupported_cd_type")
            result = deployer.deploy(req, image, project_short, host, port, user_srv, pwd, ssh_key, callback=log_callback)

            deploy_result = {"success": True, "data": result}
            # 立即通知 SSE 流部署完成，避免 DB/通知操作阻塞 UI
            log_queue.put(None)

            _record_deploy(db, project_key, req.tag, image, req.cd_type, host, result["success"], result["output"], user.get("username", ""))
            _notify_k8s(db, req.bot_id, req.tag, project_key, host, req.cd_type, image, result["success"], req.lang)
        except Exception as e:
            deploy_result = {"success": False, "error": str(e)}
            try:
                log_queue.put(None)
            except Exception:
                pass

    threading.Thread(target=do_deploy, daemon=True).start()

    async def event_stream():
        msg_id = 0
        while True:
            try:
                msg = await asyncio.to_thread(log_queue.get, timeout=30)
                if msg is None:
                    break
                msg_id += 1
                # 多行消息需要每行都加 data: 前缀，否则 SSE 只解析第一行
                safe_msg = msg.replace("\n", "\ndata: ")
                yield f"id: {msg_id}\nretry: 3000\ndata: {safe_msg}\n\n"
            except queue.Empty:
                yield "retry: 3000\ndata: .\n\n"
                await asyncio.sleep(1)

        if deploy_result.get("success"):
            # 部署流程已正常返回（result.success 表示实际部署结果）
            # 实时日志已通过 STATUS: 事件流式发出，END 只携带成功标志
            result = deploy_result["data"]
            yield f"retry: 3000\ndata: END:{str(result.get('success', False)).lower()}\n\n"
        else:
            yield f"retry: 3000\ndata: ERROR:{deploy_result.get('error', 'Deploy failed')}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
