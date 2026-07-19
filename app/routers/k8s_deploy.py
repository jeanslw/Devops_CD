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
    k8s_ns: str = ""            # 留空不传 -n，namespace 在 YAML 中声明
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
    project_short = project_key.split("/")[-1]  # php/devops-glue → devops-glue

    # 查集群
    if req.cluster_id:
        conn = db.conn()
        srv = conn.execute("SELECT * FROM cd_servers WHERE id=?", (req.cluster_id,)).fetchone()
        if not srv:
            raise HTTPException(400, "集群不存在")
        host, port, user, pwd = srv["host"], srv["port"], srv["user"], srv["password"] or ""
        cluster_type = srv["type"]
    else:
        raise HTTPException(400, "请选择目标集群")

    # 路由到对应 deployer
    if req.cd_type == "argocd":
        result = deploy_argocd(req, image, project_short, host, pwd)
    elif req.cd_type == "fluxcd":
        result = deploy_fluxcd(req, image, project_short, host, pwd)
    elif req.cd_type == "helm":
        result = deploy_helm(req, image, project_short, host, port, user, pwd)
    else:
        result = deploy_kubectl(req, image, project_short, host, port, user, pwd)

    # 记录日志
    conn = db.conn()
    conn.execute(
        "INSERT INTO cd_deploy_logs (project,tag,image,deploy_type,target,status,output) VALUES (?,?,?,?,?,?,?)",
        (project_key, req.tag, image, f"k8s/{req.cd_type}", host,
         "ok" if result["success"] else "failed",
         result["output"][:settings.log_truncate_chars] if result["output"] else ""),
    )
    conn.commit()

    return result


def _kubectl_pods(ssh, project=""):
    """获取 K8S pod 列表，可选按项目名过滤"""
    cmd = "kubectl get pods -o custom-columns=NAME:.metadata.name,IMAGE:.spec.containers[*].image,STATUS:.status.phase --no-headers 2>/dev/null"
    if project:
        cmd += f" | grep '{project}'"
    _, stdout, stderr = ssh.exec_command(cmd)
    out = stdout.read().decode().strip()
    return out or stderr.read().decode().strip()


def deploy_kubectl(req, image, project, host, port, user, pwd):
    target = DeployTarget(host=host, port=port, user=user, password=pwd)

    tag = req.tag
    filter_name = project.split("/")[-1]

    # YAML 来源：master 本地路径 或 远程 URL
    yaml_content = ""
    if not req.path:
        raise HTTPException(400, "kubectl 模式需要 YAML 路径或 URL")
    if req.path.startswith("http"):
        # CD 拉取远程 YAML
        import requests
        r = requests.get(req.path, timeout=10)
        if r.status_code != 200:
            raise HTTPException(400, f"无法获取远程 YAML: {req.path}")
        yaml_content = r.text
    else:
        # master 本地路径
        try:
            ssh = ssh_connect(target, settings.ssh_timeout)
            _, stdout, _ = ssh.exec_command(f"cat {req.path}")
            yaml_content = stdout.read().decode()
            ssh.close()
        except Exception:
            raise HTTPException(400, f"无法读取远程 YAML: {req.path}")

    # 替换 {TAG} 和 {IMAGE}
    yaml_content = yaml_content.replace("{TAG}", tag).replace("{IMAGE}", image)

    tmp = f"/tmp/k8s-{filter_name}.yaml"

    # SFTP 写临时 YAML
    try:
        ssh2 = ssh_connect(target, settings.ssh_timeout)
        sftp = ssh2.open_sftp()
        with sftp.file(tmp, "w") as f:
            f.write(yaml_content)
        sftp.close()
        ssh2.close()
    except Exception as e:
        raise HTTPException(400, f"YAML 上传失败: {e}")

    cmds = [
        f"kubectl apply -f {tmp} && kubectl rollout restart deployment/{filter_name}",
        "sleep 10",
        f"kubectl get pods -o wide | grep '{filter_name}'",
    ]

    ssh = ssh_connect(target, settings.ssh_timeout)
    deploy_log = []
    try:
        # 部署前查看当前版本
        before = _kubectl_pods(ssh, filter_name)
        before_text = f"当前运行版本:\n{before or '(无)'}" if before.strip() else "当前运行版本: (无)"

        # 部署
        for c in cmds:
            _, stdout, stderr = ssh.exec_command(c)
            o = stdout.read().decode().strip()
            e = stderr.read().decode().strip()
            if o: deploy_log.append(o)
            elif e: deploy_log.append(e)

        # 部署后查看新版本
        after = _kubectl_pods(ssh, filter_name)

        ssh.close()

        # 验证：after 中包含目标 tag 且状态为 Running
        matched = 1 if (after and tag in after and "Running" in after) else 0

        result = f"{before_text}\n\n开始部署:\n" + "\n".join(deploy_log) + f"\n\n部署完成！\n\n当前运行新版本:\n{after or '(无)'}"
        result += f"\n\n验证部署: {'✅ 部署成功！' if matched > 0 else '❌ 部署失败！(版本不匹配)'}"
        return {"success": matched > 0, "output": result[:settings.log_truncate_chars]}
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
        output.append(f"更新镜像配置: {'✅ 成功' if r.status_code == 200 else '❌ 失败 (' + str(r.status_code) + ')'}")

        # 3. Sync
        r = requests.post(f"{base}/api/v1/applications/{project}/sync", json={}, headers=headers, timeout=10, verify=False)
        output.append(f"触发同步 (Sync): {'✅ 已触发' if r.status_code == 200 else '❌ 失败 (' + str(r.status_code) + ')'}")

        # 4. 等待 healthy
        import time
        for _ in range(30):
            time.sleep(2)
            r = requests.get(f"{base}/api/v1/applications/{project}", headers=headers, timeout=10, verify=False)
            a = r.json()
            health = a.get("status", {}).get("health", {}).get("status", "")
            sync = a.get("status", {}).get("sync", {}).get("status", "")
            if health == "Healthy":
                output.append(f"部署状态: 🟢 Healthy | 同步状态: {sync}")
                break
        else:
            output.append(f"部署状态: {health or '未知'} | 同步状态: {sync or '未知'}")

        return {"success": True, "output": "\n".join(output)}
    except Exception as e:
        return {"success": False, "output": str(e)}


def deploy_helm(req, image, project, host, port, user, pwd):
    """Helm: helm upgrade --install"""
    target = DeployTarget(host=host, port=port, user=user, password=pwd)
    tag = req.tag
    chart = req.path or f"/opt/helm/{project}"
    ns = req.k8s_ns
    ns_flag = f" -n {ns}" if ns else ""
    cmds = [
        f"kubectl delete svc/{project}{ns_flag} --ignore-not-found 2>/dev/null; kubectl delete deploy/{project}{ns_flag} --ignore-not-found 2>/dev/null; sleep 2",
        f"helm upgrade --install {project} {chart} --set image.tag={tag} --set image.repository={image.split(':')[0]}{ns_flag} --wait --timeout 120s --recreate-pods",
        "sleep 5",
        f"kubectl get pods -o wide | grep '{project.split('/')[-1]}'"
    ]
    ssh = ssh_connect(target, settings.ssh_timeout)
    try:
        before = _kubectl_pods(ssh, project.split("/")[-1])
        deploy_log = []
        for c in cmds:
            _, stdout, stderr = ssh.exec_command(c)
            o = stdout.read().decode().strip()
            e = stderr.read().decode().strip()
            if o: deploy_log.append(o)
            elif e: deploy_log.append(e)
        after = _kubectl_pods(ssh, project.split("/")[-1])
        ssh.close()
        matched = 1 if (after and tag in after and "Running" in after) else 0
        before_text = f"当前运行版本:\n{before or '(无)'}" if before.strip() else "当前运行版本: (无)"
        result = f"{before_text}\n\n开始部署:\n" + "\n".join(deploy_log) + f"\n\n部署完成！\n\n当前运行新版本:\n{after or '(无)'}"
        result += f"\n\n验证部署: {'✅ 部署成功！' if matched > 0 else '❌ 部署失败！(版本不匹配)'}"
        return {"success": matched > 0, "output": result[:settings.log_truncate_chars]}
    except Exception as e:
        return {"success": False, "output": str(e)}


def deploy_fluxcd(req, image, project, host, pwd):
    """Flux CD: patch HelmRelease/Kustomization + watch status"""
    target = DeployTarget(host=host, port=22, user="root", password=pwd)
    tag = req.tag
    filter_name = project.split("/")[-1]

    img_name = image.split(":")[0]  # hub.abc.com/mycode/devops-glue
    cmds = [
        f"kubectl patch helmrelease.helm.toolkit.fluxcd.io {project} -n flux-system --type=merge -p '{{\"spec\":{{\"values\":{{\"image\":{{\"tag\":\"{tag}\"}}}}}}}}' 2>/dev/null || kubectl patch kustomization.kustomize.toolkit.fluxcd.io {project} -n flux-system --type=merge -p '{{\"spec\":{{\"images\":[{{\"name\":\"{img_name}\",\"newTag\":\"{tag}\"}}]}}}}'",
        f"kubectl annotate kustomization {project} -n flux-system reconcile.fluxcd.io/requestedAt=\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\" --overwrite 2>/dev/null",
        "sleep 15",
        f"kubectl get pods -o wide | grep '{filter_name}'",
    ]

    try:
        ssh = ssh_connect(target, settings.ssh_timeout)
        before = _kubectl_pods(ssh, filter_name)
        before_text = f"当前运行版本:\n{before or '(无)'}" if before.strip() else "当前运行版本: (无)"

        deploy_log = []
        for c in cmds:
            _, stdout, stderr = ssh.exec_command(c)
            o = stdout.read().decode().strip()
            e = stderr.read().decode().strip()
            if o: deploy_log.append(o)
            elif e: deploy_log.append(e)

        after = _kubectl_pods(ssh, filter_name)
        ssh.close()

        # 只比较新 pod（排除旧 pod 残留）
        new_pods = [l for l in after.split("\n") if not before or l.split()[0] not in [b.split()[0] for b in before.split("\n")]]
        new_after = "\n".join(new_pods) if new_pods else after
        matched = 1 if (new_after and tag in new_after and "Running" in new_after) else 0
        result = f"{before_text}\n\n开始部署:\n" + "\n".join(deploy_log) + f"\n\n部署完成！\n\n当前运行新版本:\n{new_after or '(无)'}"
        result += f"\n\n验证部署: {'✅ 部署成功！' if matched > 0 else '❌ 部署失败！(版本不匹配)'}"
        return {"success": matched > 0, "output": result[:settings.log_truncate_chars]}
    except Exception as e:
        return {"success": False, "output": str(e)}
