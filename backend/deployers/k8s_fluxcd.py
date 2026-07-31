"""K8S Flux CD 部署模式 — patch HelmRelease/Kustomization + trigger reconcile + verify pods"""

import json
import shlex

from backend.deployers.base import ssh_connect, DeployTarget
from backend.deployers.k8s_base import K8sSubDeployer
from backend.deployers.k8s_utils import _ssh_cmd, _kubectl_pods, _log, _exec_exit
from backend.config import settings
from backend.deploy_log import S


def _discover_flux_resource(ssh, project_fallback, image_name):
    """发现 Flux CD 资源名（HelmRelease / Kustomization），不盲猜等于项目名"""
    # 先尝试精确匹配
    for kind in ("helmrelease", "kustomization"):
        r = _ssh_cmd(ssh, f"kubectl get {kind} {project_fallback} -n {settings.flux_namespace} -o name 2>/dev/null")
        if r:
            return project_fallback, kind

    # 搜索 flux-system 下所有资源，按镜像名匹配
    for kind in ("helmrelease", "kustomization"):
        r = _ssh_cmd(
            ssh,
            f"kubectl get {kind} -n {settings.flux_namespace} -o custom-columns=NAME:.metadata.name --no-headers 2>/dev/null",
        )
        if not r:
            continue
        for name in r.split("\n"):
            name = name.strip()
            if not name:
                continue
            spec = _ssh_cmd(ssh, f"kubectl get {kind} {name} -n {settings.flux_namespace} -o yaml 2>/dev/null")
            if (image_name and image_name in spec) or project_fallback in spec:
                return name, kind

    # 没找到，fallback 到项目短名
    return project_fallback, ""


class FluxCDDeployer(K8sSubDeployer):
    """Flux CD: patch HelmRelease/Kustomization + trigger reconcile + verify pods"""

    def cd_type(self) -> str:
        return "fluxcd"

    def stop(self, req, project: str, host: str, port: int = 22,
             user: str = "root", pwd: str = "", ssh_key: str = "") -> dict:
        """停止：flux suspend <resource>"""
        target = DeployTarget(host=host, port=port, user=user, password=pwd, ssh_key=ssh_key)
        try:
            ssh = ssh_connect(target, settings.ssh_timeout)
            flux_name, flux_kind = _discover_flux_resource(ssh, project.split("/")[-1], "")
            if not flux_kind:
                ssh.close()
                return {"success": False, "output": f"Flux resource not found: {project}"}
            cmd = f"flux suspend {flux_kind} {flux_name} -n {settings.flux_namespace}"
            out, err, ec = _exec_exit(ssh, cmd, timeout=settings.ssh_timeout)
            ssh.close()
            return {"success": ec == 0, "output": (err or out)[:settings.log_truncate_chars]}
        except Exception as ex:
            return {"success": False, "output": str(ex)}

    def deploy(self, req, image, project, host, port=22, user="root", pwd="", ssh_key="", callback=None):
        import time

        target = DeployTarget(host=host, port=port, user=user, password=pwd, ssh_key=ssh_key)
        tag = req.tag
        img_name = image.split(":")[0]

        def _check_flux_error(ssh, resource_name, resource_kind):
            """检查 Flux 资源 (HelmRelease/Kustomization) 是否报错。返回错误描述或 None"""
            if resource_kind not in ("helmrelease", "kustomization"):
                return None
            raw = _ssh_cmd(
                ssh,
                f"kubectl get {resource_kind} {resource_name} -n {settings.flux_namespace} "
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

        ssh = None
        try:
            _log(callback, S("deploy_log.flux_connecting"))
            ssh = ssh_connect(target, settings.ssh_timeout)

            # ── 发现 Flux 资源名，不盲猜等于项目名 ──
            flux_name, flux_kind = _discover_flux_resource(ssh, project.split("/")[-1], img_name)
            if not flux_kind:
                _log(callback, S("deploy_log.flux_resource_not_found", image=img_name))
                ssh.close()
                return {
                    "success": False,
                    "output": f"No Flux resource (HelmRelease/Kustomization) referencing image [{img_name}] found in flux-system namespace.",
                }
            if flux_name != project.split("/")[-1]:
                _log(callback, S("deploy_log.flux_name_diff", name=flux_name, project=project.split('/')[-1]))
            _log(callback, S("deploy_log.flux_detected", kind=flux_kind, name=flux_name))

            # 1. 获取部署前状态
            _log(callback, S("deploy_log.helm_getting_current"))
            before = _kubectl_pods(ssh, flux_name)
            before_text = f"当前运行版本:\n{before or '(无)'}" if before.strip() else "当前运行版本: (无)"
            before_pod_names = set(b.split()[0] for b in before.split("\n") if b.strip()) if before else set()
            if before.strip():
                _log(callback, S("deploy_log.current_version"))
                _log(callback, before)
            else:
                _log(callback, S("deploy_log.current_version_none"))

            # 2. Patch flux 资源
            _log(callback, S("deploy_log.flux_start"))
            # 如果指定了 path 且资源是 Kustomization，先更新 spec.path
            if req.path and flux_kind == "kustomization":
                _log(callback, S("deploy_log.flux_path_update", path=req.path))
                path_patch_data = json.dumps({"spec": {"path": req.path}})
                path_cmd = (
                    f"kubectl patch {shlex.quote(flux_kind)} {shlex.quote(flux_name)} "
                    f"-n {shlex.quote(settings.flux_namespace)} --type=merge "
                    f"-p {shlex.quote(path_patch_data)}"
                )
                _exec_exit(ssh, path_cmd)

            # 安全构造 patch JSON，防止 tag 注入
            _log(callback, S("deploy_log.flux_update"))
            if flux_kind == "helmrelease":
                patch_data = json.dumps({"spec": {"values": {"image": {"tag": tag}}}})
            else:
                patch_data = json.dumps({"spec": {"images": [{"name": img_name, "newTag": tag}]}})
            patch_cmd = (
                f"kubectl patch {shlex.quote(flux_kind)} {shlex.quote(flux_name)} "
                f"-n {shlex.quote(settings.flux_namespace)} --type=merge "
                f"-p {shlex.quote(patch_data)}"
            )
            patch_out, patch_err, patch_ec = _exec_exit(ssh, patch_cmd)
            if patch_ec != 0:
                _log(callback, S("deploy_log.flux_fail_error", error=patch_err or "patch command failed"))
                ssh.close()
                return {"success": False, "output": f"Flux patch failed:\n{patch_err or patch_out}"}
            _log(callback, S("deploy_log.flux_update_ok"))

            # 3. 触发 Flux 立即协调
            _log(callback, S("deploy_log.flux_reconcile"))
            annotate_cmd = (
                f"kubectl annotate {shlex.quote(flux_kind)} {shlex.quote(flux_name)} "
                f"-n {shlex.quote(settings.flux_namespace)} "
                f"reconcile.fluxcd.io/requestedAt=\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\" --overwrite"
            )
            anno_out, anno_err, anno_ec = _exec_exit(ssh, annotate_cmd)
            if anno_ec != 0:
                _log(callback, f"⚠ Reconcile trigger failed (non-fatal): {anno_err or anno_out}")
            else:
                _log(callback, S("deploy_log.flux_reconcile_ok"))

            # 4. 等待 Flux 开始滚动更新（轮询检测新 Pod + 检查 Flux 资源报错，最长 90s）
            _log(callback, S("deploy_log.flux_wait"))
            flux_reacted = False
            for i in range(9):  # 9 × 10s = 90s
                time.sleep(10)

                # 从第 4 轮开始检查 Flux 资源是否报错
                if i >= 3:
                    flux_err = _check_flux_error(ssh, flux_name, flux_kind)
                    if flux_err:
                        _log(callback, S("deploy_log.flux_resource_error", error=flux_err))
                        ssh.close()
                        return {
                            "success": False,
                            "output": f"{before_text}\n\nDeploy started:\nImage updated, Flux reconcile triggered\n\nFlux deploy failed: {flux_err}",
                        }

                after = _kubectl_pods(ssh, flux_name)
                current_pod_names = set(l.split()[0] for l in after.split("\n") if l.strip()) if after else set()
                new_names = current_pod_names - before_pod_names
                terminating = any("Terminating" in l for l in after.split("\n")) if after else False

                if new_names or terminating:
                    flux_reacted = True
                    status = f"新 Pod: {new_names}" if new_names else "旧 Pod 正在终止"
                    _log(callback, S("deploy_log.flux_reacted", n=i+1, status=status))
                    break
                _log(callback, S("deploy_log.flux_polling", n=i+1, total=9))

            if not flux_reacted:
                flux_err = _check_flux_error(ssh, flux_name, flux_kind)
                if flux_err:
                    _log(callback, S("deploy_log.flux_resource_error", error=flux_err))
                    ssh.close()
                    return {
                        "success": False,
                        "output": f"{before_text}\n\nDeploy started:\nImage updated, Flux reconcile triggered\n\nFlux deploy failed: {flux_err}",
                    }
                _log(callback, S("deploy_log.flux_no_reaction"))

            # 5. 用 deployment 名进行 rollout status（从集群提取，不盲猜）
            rollout_result = ""
            deploy_name = _ssh_cmd(
                ssh,
                f"kubectl get deploy -o name 2>/dev/null | grep -E '^{flux_name}-' | head -1 | cut -d'/' -f2 || "
                f"kubectl get deploy -o name 2>/dev/null | grep '{flux_name}' | head -1 | cut -d'/' -f2",
            )
            if deploy_name:
                _log(callback, S("deploy_log.flux_rollout", deploy=deploy_name))
                rollout_out, rollout_err, rollout_ec = _exec_exit(
                    ssh,
                    f"kubectl rollout status deployment/{shlex.quote(deploy_name)} --timeout={settings.k8s_rollout_timeout}s",
                    timeout=settings.k8s_rollout_timeout + 30,
                )
                rollout_result = (rollout_out or rollout_err or "").strip()
                if rollout_out:
                    _log(callback, rollout_out)
                if rollout_err:
                    _log(callback, rollout_err)
            else:
                _log(callback, S("deploy_log.deploy_no_deploy"))

            # 6. 部署后状态（rollout status 已做主裁判，这里仅展示 Pod 列表）
            _log(callback, S("deploy_log.flux_verify"))
            after = _kubectl_pods(ssh, flux_name)
            ssh.close()

            if after.strip():
                _log(callback, S("deploy_log.after_version"))
                _log(callback, after)
            else:
                _log(callback, after or "(无)")

            # 7. 构建结果
            running_count = sum(1 for l in after.split("\n") if "Running" in l)
            rollout_ok = deploy_name and rollout_ec == 0

            if deploy_name and rollout_ok:
                status_text = f"已部署: {running_count} 个 Running Pod"
                _log(callback, S("deploy_log.flux_success", status=status_text))
                result = (
                    f"{before_text}\n\n开始部署:\n镜像已更新，Flux 协调已触发"
                    + f"\n\n{rollout_result}\n\n部署后运行版本:\n{after}"
                    + f"\n\n{status_text}\n\n验证部署: ✅ 部署成功！"
                )
                return {"success": True, "output": result[:settings.log_truncate_chars]}
            elif deploy_name:
                _log(callback, S("deploy_log.flux_fail_error"))
                result = (
                    f"{before_text}\n\n开始部署:\n镜像已更新，Flux 协调已触发"
                    + f"\n\n{rollout_result}\n\n部署后运行版本:\n{after}"
                    + f"\n\n验证部署: ❌ 部署失败！"
                )
                return {"success": False, "output": result[:settings.log_truncate_chars]}
            else:
                # 没找到 deployment，但 Flux 已 patch 触发协调，由 Flux 自己完成
                status_text = f"当前 Pod: {running_count} 个 Running"
                _log(callback, S("deploy_log.flux_success", status=status_text))
                result = (
                    f"{before_text}\n\n开始部署:\n镜像已更新，Flux 协调已触发"
                    + f"\n\n部署后运行版本:\n{after or '(无)'}"
                    + f"\n\n{status_text}\n\n验证部署: ⚠️ 已触发 Flux 协调，未找到对应 Deployment"
                )
                return {"success": True, "output": result[:settings.log_truncate_chars]}
        except Exception as e:
            _log(callback, S("deploy_log.flux_fail_error", error=str(e)))
            return {"success": False, "output": str(e)}
        finally:
            if ssh:
                ssh.close()
