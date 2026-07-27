"""K8S Helm 部署模式 — SSH helm upgrade --install + 镜像版本校验"""

from backend.deployers.base import ssh_connect, DeployTarget
from backend.deployers.k8s_utils import _ssh_cmd, _kubectl_pods
from backend.config import settings
from backend.deploy_log import S


def deploy_helm(req, image, project, host, port, user, pwd, ssh_key="", callback=None):
    """Helm: helm upgrade --install"""
    target = DeployTarget(host=host, port=port, user=user, password=pwd, ssh_key=ssh_key)
    tag = req.tag
    chart = req.path or f"/opt/helm/{project}"
    ns = req.k8s_ns
    ns_flag = f" -n {ns}" if ns else ""

    def log(msg):
        if callable(callback):
            callback(msg)

    try:
        log(S("deploy_log.flux_connecting"))
        ssh = ssh_connect(target, settings.ssh_timeout)

        # ── 从 Helm 获取实际 release 名，不盲猜等于项目名 ──
        helm_release = project.split("/")[-1]
        existing_releases = _ssh_cmd(ssh, f"helm list -q{ns_flag} 2>/dev/null")
        if helm_release not in (existing_releases or "").split("\n"):
            # release 不存在，尝试按 chart 名匹配
            for rel in (existing_releases or "").split("\n"):
                rel = rel.strip()
                if rel:
                    detail = _ssh_cmd(ssh, f"helm get values {rel}{ns_flag} -o json 2>/dev/null")
                    if project in detail or image.split(":")[0] in detail:
                        helm_release = rel
                        log(S("deploy_log.helm_release_found", name=rel))
                        break

        log(S("deploy_log.helm_getting_current"))
        before = _kubectl_pods(ssh, helm_release)
        before_text = f"当前运行版本:\n{before or '(无)'}" if before.strip() else "当前运行版本: (无)"
        log(before_text)

        log(S("deploy_log.helm_start"))
        # 去掉 --recreate-pods，使用默认 RollingUpdate 策略，避免单副本服务中断
        helm_cmd = (
            f"helm upgrade --install {helm_release} {chart} "
            f"--set image.tag={tag} --set image.repository={image.split(':')[0]}"
            f"{ns_flag} --wait --timeout 120s"
        )
        deploy_log = []
        log(S("deploy_log.helm_cmd", cmd=helm_cmd))
        helm_out = _ssh_cmd(ssh, helm_cmd)
        if helm_out:
            deploy_log.append(helm_out)
            log(helm_out)

        # 精确校验：检查 Deployment 是否 Available + 镜像版本是否匹配
        availability = _ssh_cmd(
            ssh,
            f"kubectl get deployment/{helm_release}{ns_flag} "
            f"-o jsonpath='{{.status.conditions[?(@.type==\"Available\")].status}}' 2>/dev/null"
        ).strip()
        deployed_image = _ssh_cmd(
            ssh,
            f"kubectl get deployment/{helm_release}{ns_flag} "
            f"-o jsonpath='{{.spec.template.spec.containers[0].image}}' 2>/dev/null"
        ).strip()

        log(S("deploy_log.helm_getting_after"))
        after = _kubectl_pods(ssh, helm_release)
        ssh.close()

        is_available = (availability == "True")
        tag_matched = (tag in (deployed_image or "")) if deployed_image else (tag in (after or ""))
        matched = is_available and tag_matched

        result = (
            f"{before_text}\n\n开始部署:\n{helm_out}"
            f"\n\n部署完成！\n\nDeployment Available: {'是' if is_available else '否'}"
            f"\n运行镜像: {deployed_image or '未知'}"
            f"\n\n当前 Pod 状态:\n{after or '(无)'}"
        )

        if matched:
            result += f"\n\n验证部署: ✅ 部署成功！"
        elif not is_available:
            result += f"\n\n验证部署: ❌ 部署失败！(Deployment 未就绪)"
        else:
            result += f"\n\n验证部署: ❌ 部署失败！(镜像版本不匹配，期望 tag={tag})"

        log(S("deploy_log.verify_ok") if matched else S("deploy_log.verify_fail"))
        return {"success": matched, "output": result[:settings.log_truncate_chars]}
    except Exception as e:
        log(S("deploy_log.helm_fail", error=str(e)))
        return {"success": False, "output": str(e)}
