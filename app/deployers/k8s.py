"""Kubernetes 部署器

优先级：
1. kubectl apply -f {path}  — 远程 YAML 文件（生产推荐）
2. kubectl set image       — 直接改镜像版本（快速迭代）
"""

from .base import Deployer, DeployTarget, DeployResult
from app.config import settings


class K8sDeployer(Deployer):
    """SSH 到 K8s 节点，优先 kubectl apply，兜底 kubectl set image"""

    def name(self) -> str:
        return "k8s"

    def deploy(
        self, target: DeployTarget, image: str, project: str, tag: str
    ) -> DeployResult:
        if not target.host:
            return DeployResult(image=image, status="failed", output="缺少 K8s 节点主机")

        namespace = target.options.get("namespace", "default")
        deployment_name = target.options.get("deployment", project)
        container_name = target.options.get("container", project)

        if target.path:
            # YAML apply 模式：apply + 通用 pod 查看
            cmds = [
                f"kubectl apply -f {target.path}",
                "sleep 2",
                f"kubectl get pods -n {namespace} -o wide | grep {project}",
            ]
        else:
            # set image 模式
            cmds = [
                f"kubectl set image deployment/{deployment_name} {container_name}={image} -n {namespace}",
                f"kubectl rollout status deployment/{deployment_name} -n {namespace} --timeout=120s",
                f"kubectl get pods -n {namespace} -o wide | grep {deployment_name}",
            ]

        try:
            from .base import ssh_connect
            ssh = ssh_connect(target, settings.ssh_timeout)
            output_lines = []
            for c in cmds:
                _, stdout, stderr = ssh.exec_command(c)
                o = stdout.read().decode().strip()
                e = stderr.read().decode().strip()
                if o: output_lines.append(o)
                elif e: output_lines.append(e)
            ssh.close()

            output = "\n".join(output_lines)
            is_ok = "successfully rolled out" in output or "Running" in output or "created" in output

            return DeployResult(
                image=image,
                status="ok" if is_ok else "failed",
                output=output[:settings.log_truncate_chars],
            )
        except Exception as e:
            return DeployResult(image=image, status="failed", output=str(e))

    def validate(self, target: DeployTarget) -> str | None:
        if not target.host:
            return "K8s 节点主机不能为空"
        return None

    def supports(self, deploy_type: str) -> bool:
        return deploy_type in ("k8s", "kubernetes")
