"""K8S Flux CD 部署模式 — patch HelmRelease/Kustomization + trigger reconcile + verify pods"""

from backend.deployers.base import ssh_connect, DeployTarget
from backend.deployers.k8s_utils import _ssh_cmd, _kubectl_pods, _poll_k8s_pods, _log
from backend.config import settings
from backend.deploy_log import S


def _discover_flux_resource(ssh, project_fallback, image_name):
    """发现 Flux CD 资源名（HelmRelease / Kustomization），不盲猜等于项目名"""
    # 先尝试精确匹配
    for kind in ("helmrelease", "kustomization"):
        r = _ssh_cmd(ssh, f"kubectl get {kind} {project_fallback} -n flux-system -o name 2>/dev/null")
        if r:
            return project_fallback, kind

    # 搜索 flux-system 下所有资源，按镜像名匹配
    for kind in ("helmrelease", "kustomization"):
        r = _ssh_cmd(
            ssh,
            f"kubectl get {kind} -n flux-system -o custom-columns=NAME:.metadata.name --no-headers 2>/dev/null",
        )
        if not r:
            continue
        for name in r.split("\n"):
            name = name.strip()
            if not name:
                continue
            spec = _ssh_cmd(ssh, f"kubectl get {kind} {name} -n flux-system -o yaml 2>/dev/null")
            if image_name in spec or project_fallback in spec:
                return name, kind

    # 没找到，fallback 到项目短名
    return project_fallback, ""


def deploy_fluxcd(req, image, project, host, pwd, ssh_key="", callback=None):
    """Flux CD: patch HelmRelease/Kustomization + poll rollout status + verify pods"""
    import time

    target = DeployTarget(host=host, port=22, user="root", password=pwd, ssh_key=ssh_key)
    tag = req.tag
    img_name = image.split(":")[0]

    def log(msg):
        if callable(callback):
            callback(msg)

    def _check_flux_error(ssh, resource_name, resource_kind):
        """检查 Flux 资源 (HelmRelease/Kustomization) 是否报错。返回错误描述或 None"""
        if resource_kind not in ("helmrelease", "kustomization"):
            return None
        raw = _ssh_cmd(
            ssh,
            f"kubectl get {resource_kind} {resource_name} -n flux-system "
            f"-o jsonpath='{{.status.conditions[?(@.type==\"Ready\")].status}}|{{.status.conditions[?(@.type==\"Ready\")].reason}}|{{.status.conditions[?(@.type==\"Ready\")].message}}' 2>/dev/null",
        )
        if not raw or "|" not in raw:
            return None
        parts = raw.split("|", 2)
        cond_status = parts[0]
        reason = parts[1] if len(parts) > 1 else ""
        message = parts[2] if len(parts) > 2 else ""
        if cond_status == "False" and reason and reason not in ("Progressing",):
            return f"[{resource_kind}] {reason}: {message}" if message else f"[{resource_kind}] {reason}"
        return None

    try:
        log(S("deploy_log.flux_connecting"))
        ssh = ssh_connect(target, settings.ssh_timeout)

        # ── 发现 Flux 资源名，不盲猜等于项目名 ──
        flux_name, flux_kind = _discover_flux_resource(ssh, project.split("/")[-1], img_name)
        if not flux_kind:
            log(S("deploy_log.flux_resource_not_found", image=img_name))
            ssh.close()
            return {
                "success": False,
                "output": f"未找到引用镜像 [{img_name}] 的 Flux 资源！请确认 flux-system 下有对应的 HelmRelease 或 Kustomization。",
            }
        if flux_name != project.split("/")[-1]:
            log(S("deploy_log.flux_name_diff", name=flux_name, project=project.split('/')[-1]))
        log(S("deploy_log.flux_detected", kind=flux_kind, name=flux_name))

        # 1. 获取部署前状态
        log(S("deploy_log.helm_getting_current"))
        before = _kubectl_pods(ssh, flux_name)
        before_text = f"当前运行版本:\n{before or '(无)'}" if before.strip() else "当前运行版本: (无)"
        before_pod_names = set(b.split()[0] for b in before.split("\n") if b.strip()) if before else set()
        log(before_text)

        # 2. Patch flux 资源
        log(S("deploy_log.flux_start"))
        log(S("deploy_log.flux_update"))
        patch_cmd = (
            f"kubectl patch {flux_kind} {flux_name} -n flux-system --type=merge "
            f"-p '{{\"spec\":{{\"values\":{{\"image\":{{\"tag\":\"{tag}\"}}}}}}}}' 2>/dev/null "
            if flux_kind == "helmrelease" else
            f"kubectl patch {flux_kind} {flux_name} -n flux-system --type=merge "
            f"-p '{{\"spec\":{{\"images\":[{{\"name\":\"{img_name}\",\"newTag\":\"{tag}\"}}]}}}}'"
        )
        result = _ssh_cmd(ssh, patch_cmd)
        log(S("deploy_log.flux_update_ok"))

        # 3. 触发 Flux 立即协调
        log(S("deploy_log.flux_reconcile"))
        annotate_cmd = (
            f"kubectl annotate {flux_kind} {flux_name} -n flux-system "
            f"reconcile.fluxcd.io/requestedAt=\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\" --overwrite 2>/dev/null"
        )
        result = _ssh_cmd(ssh, annotate_cmd)
        log(S("deploy_log.flux_reconcile_ok"))

        # 4. 等待 Flux 开始滚动更新（轮询检测新 Pod + 检查 Flux 资源报错，最长 90s）
        log(S("deploy_log.flux_wait"))
        flux_reacted = False
        for i in range(9):  # 9 × 10s = 90s
            time.sleep(10)

            # 从第 4 轮开始检查 Flux 资源是否报错
            if i >= 3:
                flux_err = _check_flux_error(ssh, flux_name, flux_kind)
                if flux_err:
                    log(S("deploy_log.flux_resource_error", error=flux_err))
                    ssh.close()
                    return {
                        "success": False,
                        "output": f"{before_text}\n\n开始部署:\n镜像已更新，Flux 协调已触发\n\nFlux 部署失败: {flux_err}",
                    }

            after = _kubectl_pods(ssh, flux_name)
            current_pod_names = set(l.split()[0] for l in after.split("\n") if l.strip()) if after else set()
            new_names = current_pod_names - before_pod_names
            terminating = any("Terminating" in l for l in after.split("\n")) if after else False

            if new_names or terminating:
                flux_reacted = True
                status = f"新 Pod: {new_names}" if new_names else "旧 Pod 正在终止"
                log(S("deploy_log.flux_reacted", n=i+1, status=status))
                break
            log(S("deploy_log.flux_polling", n=i+1, total=9))

        if not flux_reacted:
            flux_err = _check_flux_error(ssh, flux_name, flux_kind)
            if flux_err:
                log(S("deploy_log.flux_resource_error", error=flux_err))
                ssh.close()
                return {
                    "success": False,
                    "output": f"{before_text}\n\n开始部署:\n镜像已更新，Flux 协调已触发\n\nFlux 部署失败: {flux_err}",
                }
            log(S("deploy_log.flux_no_reaction"))

        # 5. 用 deployment 名进行 rollout status（从集群提取，不盲猜）
        deploy_name = _ssh_cmd(
            ssh,
            f"kubectl get deploy -o name 2>/dev/null | grep -E '^{flux_name}-' | head -1 | cut -d'/' -f2 || "
            f"kubectl get deploy -o name 2>/dev/null | grep '{flux_name}' | head -1 | cut -d'/' -f2",
        )
        if deploy_name:
            log(S("deploy_log.flux_rollout", deploy=deploy_name))
            rollout_result = _ssh_cmd(ssh, f"kubectl rollout status deployment/{deploy_name} --timeout=120s 2>&1")
            log(rollout_result or S("deploy_log.rollout_done"))
        else:
            log(S("deploy_log.deploy_no_deploy"))

        # 6. 最终验证 Pod 状态
        log(S("deploy_log.flux_verify"))
        _, stdout, _ = ssh.exec_command(
            f"kubectl get deployment/{deploy_name or flux_name} -o jsonpath='{{.spec.replicas}}' 2>/dev/null || echo 1"
        )
        expected_replicas = int(stdout.read().decode().strip() or "1")

        poll_result = _poll_k8s_pods(ssh, flux_name, image, expected_replicas)
        after = poll_result["after"]
        ssh.close()

        # 7. 构建结果
        status_text = f"已部署: {poll_result['correct_ready']}/{expected_replicas} 个正确版本 Pod"
        wait_text = f"轮询耗时: {poll_result['elapsed']}s | 最大等待: {poll_result['max_wait_seconds']}s"

        if poll_result["all_ready"]:
            log(S("deploy_log.flux_success", status=status_text))
            log(S("deploy_log.after_version"))
            log(after)
            result = (
                f"{before_text}\n\n开始部署:\n镜像已更新，Flux 协调已触发"
                + f"\n\n部署后运行版本:\n{after}\n\n{wait_text}\n{status_text}"
                + f"\n\n验证部署: ✅ 部署成功！"
            )
            success = True
        elif poll_result["has_failed"]:
            log(S("deploy_log.flux_pod_error"))
            error_pods = "\n".join(poll_result["pod_errors"])
            log(S("deploy_log.after_version"))
            log(f"{after}\n\n错误 Pod:\n{error_pods}")
            result = (
                f"{before_text}\n\n开始部署:\n镜像已更新，Flux 协调已触发"
                + f"\n\n部署后运行版本:\n{after}\n\n{wait_text}\n{status_text}"
                + f"\n\n错误 Pod:\n{error_pods}"
                + f"\n\n验证部署: ❌ 部署失败！(Pod 状态异常)"
            )
            success = False
        else:
            log(S("deploy_log.flux_timeout", status=status_text))
            pod_summary = "\n".join(poll_result["pod_details"]) if poll_result["pod_details"] else after
            log(S("deploy_log.after_version"))
            log(after)
            result = (
                f"{before_text}\n\n开始部署:\n镜像已更新，Flux 协调已触发"
                + f"\n\n部署后运行版本:\n{after}\n\n{wait_text}\n{status_text}"
                + f"\n\nPod 状态:\n{pod_summary}"
                + f"\n\n验证部署: ❌ 部署失败！(超时未就绪)"
            )
            success = False

        return {"success": success, "output": result[:settings.log_truncate_chars]}
    except Exception as e:
        log(S("deploy_log.flux_fail_error", error=str(e)))
        return {"success": False, "output": str(e)}
