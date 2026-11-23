"""K8S Flux CD 部署模式 - patch HelmRelease/Kustomization + trigger reconcile + verify pods"""

import json
import logging
import re
import shlex

from backend.config import settings
from backend.deploy_log import S
from backend.deployers.base import DeployTarget, split_image_ref, ssh_connect
from backend.deployers.k8s_base import K8sSubDeployer
from backend.deployers.k8s_utils import _exec_exit, _kubectl_pods, _log, _ssh_cmd, check_cancelled

logger = logging.getLogger(__name__)


def _repo_basename(name: str) -> str:
    """取镜像仓库末段,用于短名与完整路径的匹配."""
    return (name or "").split("@", 1)[0].rsplit("/", 1)[-1]


def _build_flux_image_patch(image_name: str, tag: str, existing_images=None):
    """Merge patch for Flux's `spec.images`, preserving unrelated overrides.

    `kubectl patch --type=merge` replaces arrays wholesale; rebuild the array from
    the current entries and update/append the target image.
    """
    current = list(existing_images or [])
    patched = []
    updated = False
    target_base = _repo_basename(image_name)
    for item in current:
        if isinstance(item, str):
            # 兼容 "name=newTag" / "name" 形式的字符串条目,不再让 dict() 抛错中断部署
            name, sep, value = item.partition("=")
            entry = {"name": name.strip()}
            if sep and value.strip():
                entry["newTag"] = value.strip()
        elif isinstance(item, dict):
            entry = dict(item)
        else:
            continue
        entry_name = str(entry.get("name") or "")
        # 完整仓库路径与 Kustomization 内的短名不相等时,按末段匹配到已有条目,避免追加重复 override
        if not updated and (entry_name == image_name or _repo_basename(entry_name) == target_base):
            entry["newTag"] = tag
            updated = True
        patched.append(entry)
    if not updated:
        patched.append({"name": image_name, "newTag": tag})
    return {"spec": {"images": patched}}


def _discover_flux_resource(ssh, name, kind=""):
    """按显式资源名精确定位 Flux 资源,不再扫描 flux-system 猜目标.

    返回 (name, kind);未找到时 kind 为空.以找不到就失败代替「猜一个最像的」.
    """
    ns_q = shlex.quote(settings.flux_namespace)
    kinds = (kind,) if kind in ("helmrelease", "kustomization") else ("helmrelease", "kustomization")
    for item_kind in kinds:
        r = _ssh_cmd(
            ssh,
            f"kubectl get {shlex.quote(item_kind)} {shlex.quote(name)} -n {ns_q} -o name 2>/dev/null",
        )
        if r:
            return name, item_kind
    return name, ""


def _flux_pod_label(name: str) -> str:
    """Flux/Helm 生成的 Pod 通用实例标签,优先于按 release 名做前缀猜测."""
    return f"app.kubernetes.io/instance={name}"


def _deployment_images(ssh, deploy_name: str) -> list[str]:
    """读取 Deployment 当前容器镜像列表,用于部署后断言."""
    jsonpath = "{.spec.template.spec.containers[*].image}"
    raw = _ssh_cmd(
        ssh,
        f"kubectl get deploy {shlex.quote(deploy_name)} -o jsonpath={shlex.quote(jsonpath)} 2>/dev/null",
    )
    return [item.strip() for item in raw.split() if item.strip()]


def _deploy_image_matches(ref: str, repo: str, tag: str) -> bool:
    """判断实际镜像是否为目标仓库末段 + tag,允许 registry 前缀差异."""
    if "@" in ref:
        return False
    name, ref_tag = split_image_ref(ref)
    return name.rsplit("/", 1)[-1] == repo.rsplit("/", 1)[-1] and ref_tag == tag


class FluxCDDeployer(K8sSubDeployer):
    """Flux CD: patch HelmRelease/Kustomization + trigger reconcile + verify pods"""

    def cd_type(self) -> str:
        return "fluxcd"

    def stop(
        self, req, project: str, host: str, port: int = 22, user: str = "root", pwd: str = "", ssh_key: str = ""
    ) -> dict:
        """停止:flux suspend <resource>"""
        target = DeployTarget(host=host, port=port, user=user, password=pwd, ssh_key=ssh_key)
        try:
            ssh = ssh_connect(target, settings.ssh_timeout)
            stop_name = (
                (getattr(req, "target_app", "") or "").strip()
                or (getattr(req, "k8s_deploy", "") or "").strip()
                or project.split("/")[-1]
            )
            stop_kind = (getattr(req, "flux_kind", "") or "").strip().lower()
            flux_name, flux_kind = _discover_flux_resource(ssh, stop_name, stop_kind)
            if not flux_kind:
                ssh.close()
                return {"success": False, "output": f"Flux resource not found: {project}"}
            cmd = (
                f"flux suspend {shlex.quote(flux_kind)} {shlex.quote(flux_name)} "
                f"-n {shlex.quote(settings.flux_namespace)}"
            )
            out, err, ec = _exec_exit(ssh, cmd, timeout=settings.ssh_timeout)
            ssh.close()
            return {"success": ec == 0, "output": (err or out)[: settings.log_truncate_chars]}
        except Exception as ex:
            logger.error("FluxCD stop failed", exc_info=ex)
            return {"success": False, "output": "Stop service failed, please contact administrator"}

    def deploy(self, req, image, project, host, port=22, user="root", pwd="", ssh_key="", callback=None):
        import time

        target = DeployTarget(host=host, port=port, user=user, password=pwd, ssh_key=ssh_key)
        tag = req.tag
        img_name, _ = split_image_ref(image)

        def _check_flux_error(ssh, resource_name, resource_kind):
            """检查 Flux 资源 (HelmRelease/Kustomization) 是否报错.返回错误描述或 None"""
            if resource_kind not in ("helmrelease", "kustomization"):
                return None
            jsonpath = (
                '{.status.conditions[?(@.type=="Ready")].status}|'
                '{.status.conditions[?(@.type=="Ready")].reason}|'
                '{.status.conditions[?(@.type=="Ready")].message}'
            )
            raw = _ssh_cmd(
                ssh,
                f"kubectl get {shlex.quote(resource_kind)} {shlex.quote(resource_name)} "
                f"-n {shlex.quote(settings.flux_namespace)} -o jsonpath={shlex.quote(jsonpath)} 2>/dev/null",
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

            # ── 用显式资源名精确定位,不再扫描 flux-system 猜目标 ──
            target_name = (getattr(req, "target_app", "") or "").strip() or project.split("/")[-1]
            target_kind = (getattr(req, "flux_kind", "") or "").strip().lower()
            flux_name, flux_kind = _discover_flux_resource(ssh, target_name, target_kind)
            if not flux_kind:
                _log(callback, S("deploy_log.flux_resource_not_found", name=target_name))
                ssh.close()
                return {
                    "success": False,
                    "output": (
                        f"Flux resource [{target_name}] not found in namespace {settings.flux_namespace}. "
                        "Please specify the HelmRelease / Kustomization metadata.name and kind."
                    ),
                }
            _log(callback, S("deploy_log.flux_detected", kind=flux_kind, name=flux_name))

            def _flux_pods():
                """按实例标签取 Pod;标签为空才回退到名称前缀,避免把同名系其他工作负载算进来."""
                pods = _kubectl_pods(ssh, "", label=_flux_pod_label(flux_name))
                return pods if pods.strip() else _kubectl_pods(ssh, flux_name)

            # 1. 获取部署前状态
            _log(callback, S("deploy_log.helm_getting_current"))
            before = _flux_pods()
            before_text = f"当前运行版本:\n{before or '(无)'}" if before.strip() else "当前运行版本: (无)"
            before_pod_names = {b.split()[0] for b in before.split("\n") if b.strip()} if before else set()
            if before.strip():
                _log(callback, S("deploy_log.current_version"))
                _log(callback, before)
            else:
                _log(callback, S("deploy_log.current_version_none"))

            # 2. Patch flux 资源
            _log(callback, S("deploy_log.flux_start"))
            # 如果指定了 path 且资源是 Kustomization,先更新 spec.path
            if req.path and flux_kind == "kustomization":
                _log(callback, S("deploy_log.flux_path_update", path=req.path))
                path_patch_data = json.dumps({"spec": {"path": req.path}})
                path_cmd = (
                    f"kubectl patch {shlex.quote(flux_kind)} {shlex.quote(flux_name)} "
                    f"-n {shlex.quote(settings.flux_namespace)} --type=merge "
                    f"-p {shlex.quote(path_patch_data)}"
                )
                path_out, path_err, path_ec = _exec_exit(ssh, path_cmd)
                if path_ec != 0:
                    _log(
                        callback,
                        S("deploy_log.flux_fail_error", error=path_err or path_out or "path patch command failed"),
                    )
                    ssh.close()
                    return {
                        "success": False,
                        "output": f"Flux path patch failed:\n{path_err or path_out or f'exit code {path_ec}'}",
                    }

            # 构造 patch JSON;Kustomization 的数组必须保留已有 override
            _log(callback, S("deploy_log.flux_update"))
            if flux_kind == "helmrelease":
                patch_data = json.dumps({"spec": {"values": {"image": {"tag": tag}}}})
            else:
                current_json, current_err, current_ec = _exec_exit(
                    ssh,
                    f"kubectl get {shlex.quote(flux_kind)} {shlex.quote(flux_name)} -n {shlex.quote(settings.flux_namespace)} -o json 2>/dev/null",
                )
                if current_ec != 0 or not current_json.strip():
                    _log(
                        callback,
                        S(
                            "deploy_log.flux_fail_error",
                            error=current_err or current_json or "failed to read Flux resource",
                        ),
                    )
                    ssh.close()
                    return {
                        "success": False,
                        "output": f"Unable to read Flux resource before patch; deployment stopped to avoid overwriting spec.images.\n{current_err or current_json}",
                    }
                try:
                    current_spec = json.loads(current_json)
                    existing_images = current_spec.get("spec", {}).get("images", []) or []
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    _log(callback, S("deploy_log.flux_fail_error", error=f"invalid Flux resource JSON: {exc}"))
                    ssh.close()
                    return {
                        "success": False,
                        "output": f"Invalid Flux resource JSON; deployment stopped to avoid overwriting spec.images: {exc}",
                    }
                patch_data = json.dumps(_build_flux_image_patch(img_name, tag, existing_images))
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
                f'reconcile.fluxcd.io/requestedAt="$(date -u +%Y-%m-%dT%H:%M:%SZ)" --overwrite'
            )
            anno_out, anno_err, anno_ec = _exec_exit(ssh, annotate_cmd)
            if anno_ec != 0:
                _log(callback, f"⚠ Reconcile trigger failed (non-fatal): {anno_err or anno_out}")
            else:
                _log(callback, S("deploy_log.flux_reconcile_ok"))

            # 4. 等待 Flux 开始滚动更新(轮询检测新 Pod + 检查 Flux 资源报错,最长 90s)
            _log(callback, S("deploy_log.flux_wait"))
            flux_reacted = False
            for i in range(9):  # 9 × 10s = 90s
                check_cancelled()
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

                after = _flux_pods()
                current_pod_names = {line.split()[0] for line in after.split("\n") if line.strip()} if after else set()
                new_names = current_pod_names - before_pod_names
                terminating = any("Terminating" in line for line in after.split("\n")) if after else False

                if new_names or terminating:
                    flux_reacted = True
                    status = f"新 Pod: {new_names}" if new_names else "旧 Pod 正在终止"
                    _log(callback, S("deploy_log.flux_reacted", n=i + 1, status=status))
                    break
                _log(callback, S("deploy_log.flux_polling", n=i + 1, total=9))

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

            # 5. 用 deployment 名进行 rollout status(从集群提取,不盲猜)
            rollout_result = ""
            # 依次尝试:实例标签 → 同名 Deployment → 历史前缀兜底(最后手段)
            deploy_name = _ssh_cmd(
                ssh,
                f"kubectl get deploy -l {shlex.quote(_flux_pod_label(flux_name))} -o name 2>/dev/null "
                "| head -1 | cut -d'/' -f2",
            )
            if not deploy_name:
                deploy_name = _ssh_cmd(
                    ssh,
                    f"kubectl get deploy {shlex.quote(flux_name)} -o name 2>/dev/null | cut -d'/' -f2",
                )
            if not deploy_name:
                # flux_name 可能来自请求参数,正则元字符与 shell 元字符都要转义
                pat = shlex.quote(f"^{re.escape(flux_name)}-")
                deploy_name = _ssh_cmd(
                    ssh,
                    f"kubectl get deploy -o name 2>/dev/null | grep -E {pat} | head -1 | cut -d'/' -f2",
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

            # 5.1 镜像断言:Flux 被 suspend 或未协调时,rollout status 可能对旧 Pod 返回 0
            actual_images = []
            image_ok = False
            if deploy_name:
                actual_images = _deployment_images(ssh, deploy_name)
                image_ok = any(_deploy_image_matches(img, img_name, tag) for img in actual_images)
                if not image_ok:
                    _log(
                        callback,
                        S(
                            "deploy_log.flux_image_mismatch",
                            actual=", ".join(actual_images) or "(无)",
                            expected=f"{img_name}:{tag}",
                        ),
                    )

            # 6. 部署后状态(rollout status + 镜像断言已做主裁判,这里仅展示 Pod 列表)
            _log(callback, S("deploy_log.flux_verify"))
            after = _flux_pods()
            ssh.close()

            if after.strip():
                _log(callback, S("deploy_log.after_version"))
                _log(callback, after)
            else:
                _log(callback, after or "(无)")

            # 7. 构建结果
            running_count = sum(1 for line in after.split("\n") if "Running" in line)
            rollout_ok = deploy_name and rollout_ec == 0

            if deploy_name and rollout_ok and image_ok:
                status_text = f"已部署: {running_count} 个 Running Pod"
                _log(callback, S("deploy_log.flux_success", status=status_text))
                result = (
                    f"{before_text}\n\n开始部署:\n镜像已更新，Flux 协调已触发"
                    + f"\n\n{rollout_result}\n\n部署后运行版本:\n{after}"
                    + f"\n\n{status_text}\n\n验证部署: ✅ 部署成功！"
                )
                return {"success": True, "output": result[: settings.log_truncate_chars]}
            elif deploy_name:
                _log(callback, S("deploy_log.flux_fail_error"))
                assert_line = (
                    ""
                    if image_ok
                    else (
                        f"\n\n镜像断言: 实际 [{', '.join(actual_images) or '(无)'}]，"
                        f"期望 [{img_name}:{tag}]；Flux 可能未协调本次变更"
                    )
                )
                result = (
                    f"{before_text}\n\n开始部署:\n镜像已更新，Flux 协调已触发"
                    + f"\n\n{rollout_result}\n\n部署后运行版本:\n{after}"
                    + assert_line
                    + "\n\n验证部署: ❌ 部署失败！"
                )
                return {"success": False, "output": result[: settings.log_truncate_chars]}
            else:
                # 未找到 Deployment 时按失败处理
                status_text = f"当前 Pod: {running_count} 个 Running"
                _log(callback, S("deploy_log.flux_fail_error"))
                result = (
                    f"{before_text}\n\n开始部署:\n镜像已更新，Flux 协调已触发"
                    + f"\n\n部署后运行版本:\n{after or '(无)'}"
                    + "\n\n验证部署: ❌ 未找到对应 Deployment，无法确认 Flux 已完成滚动发布"
                )
                return {"success": False, "output": result[: settings.log_truncate_chars]}
        except Exception as e:
            logger.error("FluxCD deploy failed", exc_info=e)
            _log(callback, S("deploy_log.flux_fail_error", error=str(e)))
            return {"success": False, "output": str(e)}
        finally:
            if ssh:
                ssh.close()
