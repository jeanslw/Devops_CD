"""K8S 部署路由 — kubectl SSH / Argo CD / Flux CD"""

from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from app.database import Database
from app.auth import get_db, verify_token
from app.services.ci_service import CiService

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
    _username: str = Depends(verify_token),
):
    svc = CiService(db)
    harbor_repo = svc.resolve_harbor_repo(req.project)
    if not harbor_repo:
        raise HTTPException(400, f"项目 '{req.project}' 未配置 harbor_repository")

    image = f"{settings.harbor_registry}/{harbor_repo}:{req.tag}"
    project_key = svc.resolve_project_key(req.project) or req.project
    project_short = project_key.split("/")[-1]

    # 查集群
    if req.cluster_id:
        conn = db.conn()
        srv = conn.execute("SELECT * FROM cd_servers WHERE id=?", (req.cluster_id,)).fetchone()
        if not srv:
            raise HTTPException(400, "集群不存在")
        host, port, user, pwd = srv["host"], srv["port"], srv["user"], srv["password"] or ""
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
    cmd = (
        "kubectl get pods -o custom-columns=NAME:.metadata.name,IMAGE:.spec.containers[*].image,"
        "STATUS:.status.phase,REASON:.status.reason,DELETING:.metadata.deletionTimestamp --no-headers 2>/dev/null"
    )
    if project:
        cmd += f" | grep '{project}'"
    _, stdout, stderr = ssh.exec_command(cmd)
    out = stdout.read().decode().strip()
    return out or stderr.read().decode().strip()


def _parse_pod_line(line: str) -> dict | None:
    parts = line.split()
    if not parts:
        return None

    name = parts[0]
    if len(parts) < 4:
        return None

    deleting = parts[-1] if len(parts) > 4 else ""
    reason = parts[-2] if len(parts) > 3 else ""
    status = parts[-3] if len(parts) > 2 else ""
    image = " ".join(parts[1:-3]) if len(parts) > 4 else ""

    if deleting and deleting not in {"<none>", "None", "null", ""}:
        status = "Terminating"
        reason = deleting
    elif reason in {"<none>", "None", "null", ""}:
        reason = ""

    return {
        "name": name,
        "image": image,
        "status": status,
        "reason": reason,
    }


def _render_k8s_yaml(yaml_content: str, image: str, tag: str) -> str:
    image_parts = image.rsplit(":", 1)
    image_name = image_parts[0]
    if "{IMAGE}:{TAG}" in yaml_content:
        yaml_content = yaml_content.replace("{IMAGE}:{TAG}", f"{image_name}:{tag}")
    return yaml_content.replace("{TAG}", tag).replace("{IMAGE}", image).replace("{IMAGE_NAME}", image_name)


def _poll_k8s_pods(ssh, filter_name: str, desired_image: str, expected_replicas: int, max_wait: int = 20, interval: int = 3) -> dict:
    import time

    start_ts = time.monotonic()
    all_ready = False
    has_failed = False
    pod_details = []
    pod_errors = []
    correct_ready = 0
    after = ""
    failed_states = [
        "InvalidImageName", "ErrImagePull", "ImagePullBackOff", "CrashLoopBackOff",
        "RunContainerError", "CreateContainerError", "CreateContainerConfigError",
    ]

    for _ in range(max_wait):
        time.sleep(interval)
        after = _kubectl_pods(ssh, filter_name)
        pods = []
        for line in after.split("\n"):
            if not line.strip():
                continue
            parsed = _parse_pod_line(line)
            if parsed:
                pods.append(parsed)

        if not pods:
            continue

        correct_ready = 0
        pod_details = []
        pod_errors = []

        for pod in pods:
            image_text = pod["image"]
            status = pod["status"]
            reason = pod["reason"]
            if desired_image in image_text and status == "Running":
                correct_ready += 1

            detail = f"{pod['name']}: {image_text} | {status}" + (f" ({reason})" if reason else "")
            pod_details.append(detail)

            error_reason = (reason or "").strip().lower()
            normalized_status = (status or "").strip().lower()
            is_true_failure = normalized_status in {"failed", "unknown", "terminating"}
            is_image_failure = any(fs.lower() in error_reason for fs in failed_states)
            if is_true_failure or is_image_failure:
                pod_errors.append(detail)

        if pod_errors:
            has_failed = True
            break
        if correct_ready >= expected_replicas:
            all_ready = True
            break

    elapsed = int(time.monotonic() - start_ts)
    return {
        "all_ready": all_ready,
        "has_failed": has_failed,
        "correct_ready": correct_ready,
        "pod_details": pod_details,
        "pod_errors": pod_errors,
        "after": after,
        "elapsed": elapsed,
        "max_wait_seconds": max_wait * interval,
    }


def _log(callback, message):
    if callable(callback):
        callback(message)

def deploy_kubectl(req, image, project, host, port, user, pwd, callback=None):
    target = DeployTarget(host=host, port=port, user=user, password=pwd)

    tag = req.tag
    filter_name = project.split("/")[-1]

    yaml_content = ""
    if not req.path:
        raise HTTPException(400, "kubectl 模式需要 YAML 路径或 URL")
    if req.path.startswith("http"):
        _log(callback, "正在下载远程 YAML...")
        import requests
        r = requests.get(req.path, timeout=10)
        if r.status_code != 200:
            raise HTTPException(400, f"无法获取远程 YAML: {req.path}")
        yaml_content = r.text
        _log(callback, "✅ YAML 下载成功")
    else:
        _log(callback, "正在读取远程 YAML...")
        try:
            ssh = ssh_connect(target, settings.ssh_timeout)
            _, stdout, _ = ssh.exec_command(f"cat {req.path}")
            yaml_content = stdout.read().decode()
            ssh.close()
        except Exception:
            raise HTTPException(400, f"无法读取远程 YAML: {req.path}")
        _log(callback, "✅ YAML 读取成功")

    yaml_content = _render_k8s_yaml(yaml_content, image, tag)

    tmp = f"/tmp/k8s-{filter_name}.yaml"

    _log(callback, "正在上传 YAML 到服务器...")
    try:
        ssh2 = ssh_connect(target, settings.ssh_timeout)
        sftp = ssh2.open_sftp()
        with sftp.file(tmp, "w") as f:
            f.write(yaml_content)
        sftp.close()
        ssh2.close()
    except Exception as e:
        raise HTTPException(400, f"YAML 上传失败: {e}")
    _log(callback, "✅ YAML 上传成功")

    ssh = ssh_connect(target, settings.ssh_timeout)
    deploy_log = []
    try:
        _log(callback, "正在查看当前运行版本...")
        before = _kubectl_pods(ssh, filter_name)
        before_text = f"当前运行版本:\n{before or '(无)'}" if before.strip() else "当前运行版本: (无)"
        before_pods = set(b.split()[0] for b in before.split("\n") if b.strip()) if before else set()

        _log(callback, "\n正在验证应用一致性...")
        if not before.strip():
            all_pods = _kubectl_pods(ssh, "")
            running_pods = all_pods.strip()
            if running_pods:
                _log(callback, f"❌ 部署失败：未找到应用 [{filter_name}]，当前运行的 Pod：\n{running_pods}")
                ssh.close()
                return {"success": False, "output": f"{before_text}\n\n部署失败：未找到应用 [{filter_name}]，当前运行的 Pod：\n{running_pods}"}
            else:
                _log(callback, f"⚠️ 未检测到运行中的 Pod，将首次部署 [{filter_name}]")
        else:
            _log(callback, f"✅ 应用 [{filter_name}] 验证通过")
        _log(callback, before_text)

        _log(callback, "\n开始部署...")
        cmds = [
            f"kubectl apply -f {tmp}",
            f"kubectl rollout restart deployment/{filter_name}",
        ]
        for i, c in enumerate(cmds):
            _log(callback, f"\n执行命令 {i+1}: {c}")
            _, stdout, stderr = ssh.exec_command(c)
            o = stdout.read().decode().strip()
            e = stderr.read().decode().strip()
            if o:
                deploy_log.append(o)
                _log(callback, o)
            elif e:
                deploy_log.append(e)
                _log(callback, e)

        _log(callback, "\n等待 Pod 启动...")
        _, stdout, _ = ssh.exec_command(f"kubectl get deployment/{filter_name} -o jsonpath='{{.spec.replicas}}' 2>/dev/null || echo 1")
        expected_replicas = int(stdout.read().decode().strip() or "1")

        poll_result = _poll_k8s_pods(ssh, filter_name, image, expected_replicas)
        after = poll_result["after"]
        ssh.close()

        wait_text = f"轮询耗时: {poll_result['elapsed']}s, 最大等待: {poll_result['max_wait_seconds']}s"
        status_text = f"已部署: {poll_result['correct_ready']}/{expected_replicas} 个正确版本 Pod"
        pod_summary = "\n".join(poll_result["pod_details"]) if poll_result["pod_details"] else after

        if poll_result["all_ready"]:
            _log(callback, "✅ Pod 启动完成！")
            result = (
                f"{before_text}\n\n开始部署:\n" + "\n".join(deploy_log)
                + f"\n\n部署后运行版本:\n{after}\n\n{wait_text}\n{status_text}\n\n验证部署: ✅ 部署成功！"
            )
            _log(callback, "\n部署后运行版本:\n" + after)
            _log(callback, "\n验证部署: ✅ 部署成功！")
        elif poll_result["has_failed"]:
            _log(callback, "❌ Pod 启动失败！")
            result = (
                f"{before_text}\n\n开始部署:\n" + "\n".join(deploy_log)
                + f"\n\n部署后运行版本:\n{after}\n\n{wait_text}\n{status_text}\n\n错误 Pod:\n" + "\n".join(poll_result["pod_errors"])
                + f"\n\n验证部署: ❌ 部署失败！(Pod 状态异常)"
            )
            _log(callback, "\n部署后运行版本:\n" + after)
            _log(callback, "\n验证部署: ❌ 部署失败！(Pod 状态异常)")
        else:
            _log(callback, "❌ Pod 启动失败！")
            result = (
                f"{before_text}\n\n开始部署:\n" + "\n".join(deploy_log)
                + f"\n\n部署后运行版本:\n{after}\n\n{wait_text}\n{status_text}\n\nPod 状态:\n{pod_summary}"
                + f"\n\n验证部署: ❌ 部署失败！(超时未就绪)"
            )
            _log(callback, "\n部署后运行版本:\n" + after)
            _log(callback, "\n验证部署: ❌ 部署失败！(超时未就绪)")

        return {"success": poll_result["all_ready"], "output": result[:settings.log_truncate_chars]}
    except Exception as e:
        _log(callback, f"\n❌ 部署失败: {e}")
        return {"success": False, "output": str(e)}


def deploy_argocd(req, image, project, host, token, callback=None):
    """Argo CD: patch image + sync"""
    import requests
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    import time

    base = req.api_url or f"https://{host}"
    output = []
    success = False

    def log(msg):
        if callable(callback):
            callback(msg)
        output.append(msg)

    try:
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        log("正在连接 Argo CD API...")
        r = requests.get(f"{base}/api/v1/applications/{project}", headers=headers, timeout=10, verify=False)
        if r.status_code != 200:
            msg = f"Argo CD 获取应用失败: {r.status_code} {r.text[:200]}"
            log(msg)
            return {"success": False, "output": msg}
        app = r.json()

        log("正在准备镜像更新参数...")
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

        log("正在向 Argo CD 发送更新请求...")
        r = requests.put(f"{base}/api/v1/applications/{project}", json=patch, headers=headers, timeout=10, verify=False)
        if r.status_code != 200:
            log(f"更新镜像配置: ❌ 失败 ({r.status_code}) {r.text[:200]}")
            return {"success": False, "output": "\n".join(output)}
        log("更新镜像配置: ✅ 成功")

        log("正在触发 Argo CD Sync...")
        r = requests.post(f"{base}/api/v1/applications/{project}/sync", json={}, headers=headers, timeout=10, verify=False)
        if r.status_code != 200:
            log(f"触发同步 (Sync): ❌ 失败 ({r.status_code}) {r.text[:200]}")
            return {"success": False, "output": "\n".join(output)}
        log("触发同步 (Sync): ✅ 已触发")

        health = ""
        sync = ""
        for i in range(30):
            time.sleep(2)
            r = requests.get(f"{base}/api/v1/applications/{project}", headers=headers, timeout=10, verify=False)
            a = r.json()
            health = a.get("status", {}).get("health", {}).get("status", "")
            sync = a.get("status", {}).get("sync", {}).get("status", "")
            log(f"等待 Argo CD 就绪... 第 {i+1}/30 次轮询 | health={health or '未知'} | sync={sync or '未知'}")
            if health == "Healthy":
                log(f"部署状态: 🟢 Healthy | 同步状态: {sync}")
                success = True
                break
        else:
            log(f"部署状态: {health or '未知'} | 同步状态: {sync or '未知'} | ⚠️ 超时未就绪")

        return {"success": success, "output": "\n".join(output)}
    except Exception as e:
        msg = str(e)
        log(msg)
        return {"success": False, "output": msg}


def deploy_helm(req, image, project, host, port, user, pwd, callback=None):
    """Helm: helm upgrade --install"""
    target = DeployTarget(host=host, port=port, user=user, password=pwd)
    tag = req.tag
    chart = req.path or f"/opt/helm/{project}"
    ns = req.k8s_ns
    ns_flag = f" -n {ns}" if ns else ""

    def log(msg):
        if callable(callback):
            callback(msg)

    cmds = [
        f"kubectl delete svc/{project}{ns_flag} --ignore-not-found 2>/dev/null; kubectl delete deploy/{project}{ns_flag} --ignore-not-found 2>/dev/null; sleep 2",
        f"helm upgrade --install {project} {chart} --set image.tag={tag} --set image.repository={image.split(':')[0]}{ns_flag} --wait --timeout 120s --recreate-pods",
        "sleep 5",
        f"kubectl get pods -o wide | grep '{project.split('/')[-1]}'"
    ]
    try:
        log("正在连接集群...")
        ssh = ssh_connect(target, settings.ssh_timeout)

        log("正在获取当前运行版本...")
        before = _kubectl_pods(ssh, project.split("/")[-1])
        before_text = f"当前运行版本:\n{before or '(无)'}" if before.strip() else "当前运行版本: (无)"
        log(before_text)

        log("\n开始 Helm 部署...")
        deploy_log = []
        for i, c in enumerate(cmds):
            log(f"执行命令 {i+1}: {c}")
            _, stdout, stderr = ssh.exec_command(c)
            o = stdout.read().decode().strip()
            e = stderr.read().decode().strip()
            if o:
                deploy_log.append(o)
                log(o)
            elif e:
                deploy_log.append(e)
                log(e)

        log("\n正在获取部署后运行版本...")
        after = _kubectl_pods(ssh, project.split("/")[-1])
        ssh.close()

        matched = 1 if (after and tag in after and "Running" in after) else 0
        result = f"{before_text}\n\n开始部署:\n" + "\n".join(deploy_log) + f"\n\n部署完成！\n\n当前运行新版本:\n{after or '(无)'}"
        result += f"\n\n验证部署: {'✅ 部署成功！' if matched > 0 else '❌ 部署失败！(版本不匹配)'}"
        log(f"\n验证部署: {'✅ 部署成功！' if matched > 0 else '❌ 部署失败！(版本不匹配)'}")
        return {"success": matched > 0, "output": result[:settings.log_truncate_chars]}
    except Exception as e:
        log(f"\n❌ Helm 部署失败: {e}")
        return {"success": False, "output": str(e)}


def deploy_fluxcd(req, image, project, host, pwd, callback=None):
    """Flux CD: patch HelmRelease/Kustomization + poll rollout status + verify pods"""
    import time

    target = DeployTarget(host=host, port=22, user="root", password=pwd)
    tag = req.tag
    filter_name = project.split("/")[-1]
    img_name = image.split(":")[0]

    def log(msg):
        if callable(callback):
            callback(msg)

    def _ssh_cmd(ssh, cmd):
        """执行 SSH 命令并返回 stdout+stderr"""
        _, stdout, stderr = ssh.exec_command(cmd)
        o = stdout.read().decode().strip()
        e = stderr.read().decode().strip()
        return o or e

    def _check_flux_error(ssh, project):
        """检查 Flux 资源 (HelmRelease/Kustomization) 是否报错。返回错误描述或 None"""
        # 取 Ready 条件的 status|reason|message，先查 HelmRelease，再查 Kustomization
        for kind in ("helmrelease", "kustomization"):
            raw = _ssh_cmd(
                ssh,
                f"kubectl get {kind} {project} -n flux-system "
                f"-o jsonpath='{{.status.conditions[?(@.type==\"Ready\")].status}}|{{.status.conditions[?(@.type==\"Ready\")].reason}}|{{.status.conditions[?(@.type==\"Ready\")].message}}' 2>/dev/null"
            )
            if not raw or "|" not in raw:
                continue
            parts = raw.split("|", 2)
            cond_status = parts[0]
            reason = parts[1] if len(parts) > 1 else ""
            message = parts[2] if len(parts) > 2 else ""

            # True 或 Unknown/空（协调中）不算错误；False + 有原因才算
            if cond_status == "False" and reason and reason not in ("Progressing",):
                return f"[{kind}] {reason}: {message}" if message else f"[{kind}] {reason}"
        return None

    try:
        log("正在连接集群...")
        ssh = ssh_connect(target, settings.ssh_timeout)

        # 0. 前置检查：Flux 资源是否存在
        log("正在检查 Flux 资源...")
        hr = _ssh_cmd(ssh, f"kubectl get helmrelease {project} -n flux-system -o name 2>/dev/null")
        ks = _ssh_cmd(ssh, f"kubectl get kustomization {project} -n flux-system -o name 2>/dev/null")
        resource_type = None
        if hr:
            resource_type = "HelmRelease"
        elif ks:
            resource_type = "Kustomization"
        else:
            log(f"❌ 在 flux-system 命名空间下未找到 HelmRelease 或 Kustomization [{project}]")
            ssh.close()
            return {
                "success": False,
                "output": f"Flux 资源 [{project}] 不存在！请确认 flux-system 下是否有对应的 HelmRelease 或 Kustomization。",
            }
        log(f"✅ 检测到 Flux 资源: {resource_type} [{project}]")

        # 1. 获取部署前状态
        log("正在获取当前运行版本...")
        before = _kubectl_pods(ssh, filter_name)
        before_text = f"当前运行版本:\n{before or '(无)'}" if before.strip() else "当前运行版本: (无)"
        before_pod_names = set(b.split()[0] for b in before.split("\n") if b.strip()) if before else set()
        log(before_text)

        # 2. Patch kustomization/helmrelease
        log("\n开始部署 Flux CD...")
        log("正在更新镜像配置...")
        patch_cmd = (
            f"kubectl patch helmrelease.helm.toolkit.fluxcd.io {project} -n flux-system --type=merge "
            f"-p '{{\"spec\":{{\"values\":{{\"image\":{{\"tag\":\"{tag}\"}}}}}}}}' 2>/dev/null "
            f"|| kubectl patch kustomization.kustomize.toolkit.fluxcd.io {project} -n flux-system --type=merge "
            f"-p '{{\"spec\":{{\"images\":[{{\"name\":\"{img_name}\",\"newTag\":\"{tag}\"}}]}}}}'"
        )
        result = _ssh_cmd(ssh, patch_cmd)
        log(f"镜像配置: {result or '✅ 已更新'}")

        # 3. 触发 Flux 立即协调（HelmRelease / Kustomization 都支持此 annotation）
        log("正在触发 Flux 协调...")
        annotate_cmd = (
            f"kubectl annotate helmrelease {project} -n flux-system "
            f"reconcile.fluxcd.io/requestedAt=\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\" --overwrite 2>/dev/null "
            f"|| kubectl annotate kustomization {project} -n flux-system "
            f"reconcile.fluxcd.io/requestedAt=\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\" --overwrite 2>/dev/null"
        )
        result = _ssh_cmd(ssh, annotate_cmd)
        log(f"触发协调: {result or '✅ 已触发'}")

        # 4. 等待 Flux 开始滚动更新（轮询检测新 Pod + 检查 Flux 资源报错，最长 90s）
        log("\n等待 Flux CD 开始部署...")
        flux_reacted = False
        for i in range(9):  # 9 × 10s = 90s
            time.sleep(10)

            # 从第 4 轮开始检查 Flux 资源是否报错（前 3 轮给 Flux 协调启动时间）
            if i >= 3:
                flux_err = _check_flux_error(ssh, project)
                if flux_err:
                    log(f"❌ Flux 资源报错: {flux_err}")
                    ssh.close()
                    return {
                        "success": False,
                        "output": f"{before_text}\n\n开始部署:\n镜像已更新，Flux 协调已触发\n\nFlux 部署失败: {flux_err}",
                    }

            after = _kubectl_pods(ssh, filter_name)
            current_pod_names = set(l.split()[0] for l in after.split("\n") if l.strip()) if after else set()

            # Flux 已反应：出现了新 Pod 或旧 Pod 正在被替换
            new_names = current_pod_names - before_pod_names
            terminating = any(
                "Terminating" in l for l in after.split("\n")
            ) if after else False

            if new_names or terminating:
                flux_reacted = True
                status = f"新 Pod: {new_names}" if new_names else "旧 Pod 正在终止"
                log(f"Flux 已开始部署，第 {i+1} 次轮询 | {status}")
                break
            log(f"等待 Flux 协调... 第 {i+1}/9 次轮询 | 尚未检测到新 Pod")

        if not flux_reacted:
            flux_err = _check_flux_error(ssh, project)
            if flux_err:
                log(f"❌ Flux 资源报错: {flux_err}")
                ssh.close()
                return {
                    "success": False,
                    "output": f"{before_text}\n\n开始部署:\n镜像已更新，Flux 协调已触发\n\nFlux 部署失败: {flux_err}",
                }
            log("⚠️ 90 秒内未检测到 Flux 协调，继续等待滚动更新...")

        # 5. 使用 rollout status 等待部署完成（最长 120s）
        deploy_name = _ssh_cmd(ssh, f"kubectl get deploy -o name 2>/dev/null | grep '{filter_name}' | head -1 | cut -d'/' -f2")
        if deploy_name:
            log(f"\n等待滚动更新完成 [{deploy_name}]（最长 120 秒）...")
            rollout_result = _ssh_cmd(
                ssh, f"kubectl rollout status deployment/{deploy_name} --timeout=120s 2>&1"
            )
            log(rollout_result or "滚动更新完成")
        else:
            log("\n⚠️ 未找到对应 Deployment，跳过 rollout status")

        # 6. 最终验证：用 _poll_k8s_pods 确认 Pod 状态
        log("\n最终验证 Pod 状态...")
        _, stdout, _ = ssh.exec_command(
            f"kubectl get deployment/{filter_name} -o jsonpath='{{.spec.replicas}}' 2>/dev/null || echo 1"
        )
        expected_replicas = int(stdout.read().decode().strip() or "1")

        poll_result = _poll_k8s_pods(ssh, filter_name, image, expected_replicas)
        after = poll_result["after"]
        ssh.close()

        # 7. 构建结果
        status_text = f"已部署: {poll_result['correct_ready']}/{expected_replicas} 个正确版本 Pod"
        wait_text = f"轮询耗时: {poll_result['elapsed']}s | 最大等待: {poll_result['max_wait_seconds']}s"

        if poll_result["all_ready"]:
            log(f"✅ 部署成功！{status_text}")
            log(f"\n部署后运行版本:\n{after}")
            result = (
                f"{before_text}\n\n开始部署:\n镜像已更新，Flux 协调已触发"
                + f"\n\n部署后运行版本:\n{after}\n\n{wait_text}\n{status_text}"
                + f"\n\n验证部署: ✅ 部署成功！"
            )
            success = True
        elif poll_result["has_failed"]:
            log(f"❌ Pod 启动异常！")
            error_pods = "\n".join(poll_result["pod_errors"])
            log(f"\n部署后运行版本:\n{after}\n\n错误 Pod:\n{error_pods}")
            result = (
                f"{before_text}\n\n开始部署:\n镜像已更新，Flux 协调已触发"
                + f"\n\n部署后运行版本:\n{after}\n\n{wait_text}\n{status_text}"
                + f"\n\n错误 Pod:\n{error_pods}"
                + f"\n\n验证部署: ❌ 部署失败！(Pod 状态异常)"
            )
            success = False
        else:
            log(f"⚠️ 超时未就绪！{status_text}")
            pod_summary = "\n".join(poll_result["pod_details"]) if poll_result["pod_details"] else after
            log(f"\n部署后运行版本:\n{after}")
            result = (
                f"{before_text}\n\n开始部署:\n镜像已更新，Flux 协调已触发"
                + f"\n\n部署后运行版本:\n{after}\n\n{wait_text}\n{status_text}"
                + f"\n\nPod 状态:\n{pod_summary}"
                + f"\n\n验证部署: ❌ 部署失败！(超时未就绪)"
            )
            success = False

        return {"success": success, "output": result[:settings.log_truncate_chars]}
    except Exception as e:
        log(f"\n❌ Flux CD 部署失败: {e}")
        return {"success": False, "output": str(e)}


@router.post("/deploy-k8s-stream")
async def deploy_k8s_stream(
    req: K8sDeployRequest,
    db: Database = Depends(get_db),
    _username: str = Depends(verify_token),
):
    """K8S 实时部署（SSE 流式推送）"""
    import asyncio
    import queue
    import threading

    log_queue = queue.Queue()
    deploy_result = {}

    svc = CiService(db)
    harbor_repo = svc.resolve_harbor_repo(req.project)
    if not harbor_repo:
        async def err_no_repo():
            yield f"data: ERROR:项目 '{req.project}' 未配置 harbor_repository\n\n"
        return StreamingResponse(err_no_repo(), media_type="text/event-stream")

    image = f"{settings.harbor_registry}/{harbor_repo}:{req.tag}"
    project_key = svc.resolve_project_key(req.project) or req.project
    project_short = project_key.split("/")[-1]

    if req.cluster_id:
        conn = db.conn()
        srv = conn.execute("SELECT * FROM cd_servers WHERE id=?", (req.cluster_id,)).fetchone()
        conn.close()
        if not srv:
            async def err_no_cluster():
                yield "data: ERROR:集群不存在\n\n"
            return StreamingResponse(err_no_cluster(), media_type="text/event-stream")
        host, port, user, pwd = srv["host"], srv["port"], srv["user"], srv["password"] or ""
    else:
        async def err_no_cluster_id():
            yield "data: ERROR:请选择目标集群\n\n"
        return StreamingResponse(err_no_cluster_id(), media_type="text/event-stream")

    def do_deploy():
        nonlocal deploy_result
        try:
            def log_callback(message):
                log_queue.put(message)

            if req.cd_type == "argocd":
                result = deploy_argocd(req, image, project_short, host, pwd, callback=log_callback)
            elif req.cd_type == "fluxcd":
                result = deploy_fluxcd(req, image, project_short, host, pwd, callback=log_callback)
            elif req.cd_type == "helm":
                result = deploy_helm(req, image, project_short, host, port, user, pwd, callback=log_callback)
            else:
                result = deploy_kubectl(req, image, project_short, host, port, user, pwd, callback=log_callback)

            deploy_result = {"success": True, "data": result}

            conn = db.conn()
            conn.execute(
                "INSERT INTO cd_deploy_logs (project,tag,image,deploy_type,target,status,output) VALUES (?,?,?,?,?,?,?)",
                (project_key, req.tag, image, f"k8s/{req.cd_type}", host,
                 "ok" if result["success"] else "failed",
                 result["output"][:settings.log_truncate_chars] if result["output"] else ""),
            )
            conn.commit()
            conn.close()
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
            yield f"data: END:{0}:{str(result['success']).lower()}:部署完成\n\n"
        else:
            yield f"data: ERROR:{deploy_result.get('error', '部署失败')}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
