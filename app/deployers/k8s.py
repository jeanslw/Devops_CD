"""Kubernetes 部署器

优先级：
1. kubectl apply -f {path}  — 远程 YAML 文件（生产推荐）
2. kubectl set image       — 直接改镜像版本（快速迭代）
"""

from .base import Deployer, DeployTarget, DeployResult, ssh_session, _exec_on
from app.config import settings


class K8sDeployer(Deployer):
    """SSH 到 K8s 节点，优先 kubectl apply，兜底 kubectl set image"""

    def name(self) -> str:
        return "k8s"

    def deploy(
        self, target: DeployTarget, image: str, project: str, _tag: str,
        callback=None,
    ) -> DeployResult:
        if not target.host:
            return DeployResult(image=image, status="failed", output="缺少 K8s 节点主机")

        namespace = target.options.get("namespace", "default")
        deployment_name = target.options.get("deployment", project)
        container_name = target.options.get("container", project)

        if target.path:
            cmds = [
                f"kubectl apply -f {target.path}",
                "sleep 2",
                f"kubectl get pods -n {namespace} -o wide | grep {project}",
            ]
        else:
            cmds = [
                f"kubectl set image deployment/{deployment_name} {container_name}={image} -n {namespace}",
                f"kubectl rollout status deployment/{deployment_name} -n {namespace} --timeout=120s",
                f"kubectl get pods -n {namespace} -o wide | grep {deployment_name}",
            ]

        try:
            with ssh_session(target, settings.ssh_timeout) as ssh:
                self._log(callback, "开始部署 K8s...")
                output_lines = []
                for i, c in enumerate(cmds):
                    self._log(callback, f"\n执行命令 {i+1}: {c}")
                    o, e = _exec_on(ssh, c)
                    if o:
                        output_lines.append(o)
                        self._log(callback, o)
                    elif e:
                        output_lines.append(e)
                        self._log(callback, e)

            output = "\n".join(output_lines)
            is_ok = "successfully rolled out" in output or "Running" in output or "created" in output

            if is_ok:
                self._log(callback, "\n✅ K8s 部署成功！")
            else:
                self._log(callback, "\n❌ K8s 部署失败！")

            return DeployResult(
                image=image,
                status="ok" if is_ok else "failed",
                output=output[:settings.log_truncate_chars],
            )
        except Exception as e:
            self._log(callback, f"\n❌ K8s 部署失败: {e}")
            return DeployResult(image=image, status="failed", output=str(e))

    def validate(self, target: DeployTarget) -> str | None:
        if not target.host:
            return "K8s 节点主机不能为空"
        return None

    def supports(self, deploy_type: str) -> bool:
        return deploy_type in ("k8s", "kubernetes")
