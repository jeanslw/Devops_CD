"""K8S 部署路由 — kubectl SSH / Argo CD / Flux CD"""

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from app.database import Database
from app.auth import get_db, verify_token
from app.services.ci_service import CiService
from app.services.notification import send_webhook
from app.deployers.base import ssh_connect, DeployTarget
from app.config import settings

router = APIRouter(prefix="/api", tags=["k8s_deploy"])


class K8sDeployRequest(BaseModel):
    project: str
    tag: str
    cd_type: str = "kubectl"   # kubectl | argocd | fluxcd
    cluster_id: int = 0
    path: str = ""              # YAML path for kubectl mode
    api_url: str = ""           # Argo CD / Flux API base
    bot_id: int = 0


@router.post("/deploy-k8s")
def deploy_k8s(
    req: K8sDeployRequest,
    db: Database = Depends(get_db),
    username: str = Depends(verify_token),
):
    svc = CiService(db)
    harbor_repo = svc.resolve_harbor_repo(req.project)
    if not harbor_repo:
        raise HTTPException(400, f"项目 '{req.project}' 未配置 harbor_repository")

    image = f"{settings.harbor_registry}/{harbor_repo}:{req.tag}"
    project_key = svc.resolve_project_key(req.project) or req.project

    # 查集群
    if req.cluster_id:
        conn = db.conn()
        srv = conn.execute("SELECT * FROM servers WHERE id=?", (req.cluster_id,)).fetchone()
        if not srv:
            raise HTTPException(400, "集群不存在")
        host, port, user, pwd = srv["host"], srv["port"], srv["user"], srv["password"] or ""
        cluster_type = srv["type"]
    else:
        raise HTTPException(400, "请选择目标集群")

    # 路由到对应 deployer
    if req.cd_type == "argocd":
        result = deploy_argocd(req, image, project_key, host, pwd)
    elif req.cd_type == "fluxcd":
        result = deploy_fluxcd(req, image, project_key, host, pwd)
    else:
        result = deploy_kubectl(req, image, project_key, host, port, user, pwd)

    # 记录日志
    conn = db.conn()
    conn.execute(
        "INSERT INTO deploy_logs (project,tag,image,deploy_type,target,status,output) VALUES (?,?,?,?,?,?,?)",
        (project_key, req.tag, image, f"k8s/{req.cd_type}", host,
         "ok" if result["success"] else "failed",
         result["output"][:settings.log_truncate_chars] if result["output"] else ""),
    )
    conn.commit()

    return result


def deploy_kubectl(req, image, project, host, port, user, pwd):
    if not req.path:
        raise HTTPException(400, "kubectl 模式需要 YAML 路径")

    target = DeployTarget(host=host, port=port, user=user, password=pwd)

    cmds = [
        f"kubectl apply -f {req.path}",
        "sleep 5",
        f"kubectl get pods -o wide",
    ]

    ssh = ssh_connect(target, settings.ssh_timeout)
    output = []
    try:
        for c in cmds:
            _, stdout, stderr = ssh.exec_command(c)
            o = stdout.read().decode().strip()
            e = stderr.read().decode().strip()
            if o: output.append(o)
            elif e: output.append(e)
        ssh.close()
        text = "\n".join(output)
        return {"success": "Running" in text or "created" in text.lower(),
                "output": text[:settings.log_truncate_chars]}
    except Exception as e:
        return {"success": False, "output": str(e)}


def deploy_argocd(req, image, project, host, token):
    """Argo CD: patch image + sync"""
    import requests
    base = req.api_url or f"https://{host}"
    output = []
    ok = True

    try:
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        # 1. 获取 application
        r = requests.get(f"{base}/api/v1/applications/{project}", headers=headers, timeout=10, verify=False)
        if r.status_code != 200:
            return {"success": False, "output": f"Argo CD 获取应用失败: {r.status_code} {r.text[:200]}"}
        app = r.json()

        # 2. 获取当前参数，做 patch
        params = app.get("spec", {}).get("source", {}).get("helm", {}).get("parameters", [])
        kustomize = app.get("spec", {}).get("source", {}).get("kustomize", {})
        if kustomize:
            # Kustomize: set image via kustomize images
            new_images = [{"name": project, "newName": image.split(":")[0], "newTag": image.split(":")[1] if ":" in image else "latest"}]
            patch = {"spec": {"source": {"kustomize": {"images": new_images}}}}
        else:
            # Helm: set image tag parameter
            found = False
            for p in params:
                if p.get("name") == "image.tag":
                    p["value"] = image.split(":")[-1] if ":" in image else "latest"
                    found = True
                    break
            if not found:
                params.append({"name": "image.tag", "value": image.split(":")[-1] if ":" in image else "latest"})
            patch = {"spec": {"source": {"helm": {"parameters": params}}}}

        r = requests.put(f"{base}/api/v1/applications/{project}", json=patch, headers=headers, timeout=10, verify=False)
        output.append(f"Patch: {r.status_code}")

        # 3. Sync
        r = requests.post(f"{base}/api/v1/applications/{project}/sync", json={}, headers=headers, timeout=10, verify=False)
        output.append(f"Sync: {r.status_code}")

        # 4. 等待 healthy
        import time
        for _ in range(30):
            time.sleep(2)
            r = requests.get(f"{base}/api/v1/applications/{project}", headers=headers, timeout=10, verify=False)
            a = r.json()
            health = a.get("status", {}).get("health", {}).get("status", "")
            sync = a.get("status", {}).get("sync", {}).get("status", "")
            if health == "Healthy":
                output.append(f"Status: Healthy, Sync: {sync}")
                break
        else:
            output.append(f"Status: {health}, Sync: {sync}")

        return {"success": True, "output": "\n".join(output)}
    except Exception as e:
        return {"success": False, "output": str(e)}


def deploy_fluxcd(req, image, project, host, pwd):
    """Flux CD: patch HelmRelease/Kustomization + watch status"""
    target = DeployTarget(host=host, port=22, user="root", password=pwd)

    cmds = [
        f"kubectl patch helmrelease {project} -n flux-system --type=merge -p '{{\"spec\":{{\"values\":{{\"image\":{{\"tag\":\"{image.split(':')[-1] if ':' in image else 'latest'}\"}}}}}}}}' 2>/dev/null || kubectl patch kustomization {project} -n flux-system --type=merge -p '{{\"spec\":{{\"images\":[{{\"name\":\"{project}\",\"newTag\":\"{image.split(':')[-1] if ':' in image else 'latest'}\"}}]}}}}'",
        f"kubectl wait helmrelease/{project} -n flux-system --for=condition=ready --timeout=120s 2>/dev/null || kubectl wait kustomization/{project} -n flux-system --for=condition=ready --timeout=120s",
        f"sleep 3",
        f"kubectl get pods -o wide | grep {project} || kubectl get pods -o wide",
    ]

    try:
        ssh = ssh_connect(target, settings.ssh_timeout)
        output = []
        for c in cmds:
            _, stdout, stderr = ssh.exec_command(c)
            o = stdout.read().decode().strip()
            e = stderr.read().decode().strip()
            if o: output.append(o)
            elif e: output.append(e)
        ssh.close()
        text = "\n".join(output)
        return {"success": "Running" in text or "ready" in text.lower(),
                "output": text[:settings.log_truncate_chars]}
    except Exception as e:
        return {"success": False, "output": str(e)}
