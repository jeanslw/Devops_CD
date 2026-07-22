"""Kubernetes 部署器

优先级：
1. kubectl apply -f {path}  — 远程 YAML 文件（生产推荐）
2. kubectl set image       — 直接改镜像版本（快速迭代）
"""

from .base import Deployer, DeployTarget, DeployResult, ssh_session, _exec_on
from app.config import settings


def _get_deployment_name_from_yaml(ssh, yaml_path):
    """从 YAML 文件中提取第一个 Deployment 名称"""
    _, stdout, _ = ssh.exec_command(
        f"kubectl get -f {yaml_path} -o jsonpath='{{.items[?(@.kind==\"Deployment\")].metadata.name}}' 2>/dev/null"
    )
    raw = stdout.read().decode().strip()
    names = [n for n in raw.split() if n]
    return names[0] if names else ""


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
                f"kubectl get pods -n {namespace} --no-headers 2>/dev/null | grep -E '^{project}-[a-f0-9]'",
            ]
        else:
            cmds = [
                f"kubectl set image deployment/{deployment_name} {container_name}={image} -n {namespace}",
                f"kubectl rollout status deployment/{deployment_name} -n {namespace} --timeout=120s",
                f"kubectl get pods -n {namespace} --no-headers 2>/dev/null | grep -E '^{deployment_name}-[a-f0-9]'",
            ]

        try:
            with ssh_session(target, settings.ssh_timeout) as ssh:
                # ── 从 YAML 提取实际 Deployment 名称 ──
                if target.path:
                    actual_deploy = _get_deployment_name_from_yaml(ssh, target.path)
                    # 使用实际部署名
                    deploy_name = actual_deploy or project
                    # 校验项目名与 YAML 部署名严格相等
                    if project != deploy_name:
                        return DeployResult(
                            image=image, status="failed",
                            output=f"项目 [{project}] 与 YAML 部署名 [{deploy_name}] 不匹配，请检查 YAML 路径。",
                        )
                    cmds = [
                        f"kubectl apply -f {target.path}",
                        "sleep 2",
                        f"kubectl get pods -n {namespace} --no-headers 2>/dev/null | grep -E '^{deploy_name}-[a-f0-9]'",
                    ]

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
