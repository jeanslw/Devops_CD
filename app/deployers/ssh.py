"""SSH 单机部署器 — 自定义命令 / Ansible Playbook"""

from .base import Deployer, DeployTarget, DeployResult
from app.config import settings


class SSHDeployer(Deployer):
    """SSH 部署，支持 commands / ansible 两种模式"""

    def name(self) -> str:
        return "ssh"

    def deploy(
        self, target: DeployTarget, image: str, project: str, tag: str
    ) -> DeployResult:
        if not target.host:
            return DeployResult(image=image, status="failed", output="缺少目标主机")

        if target.mode == "ansible":
            cmd = self._build_ansible(target, image, project, tag)
        else:
            cmd = self._build_commands(target, image, project, tag)

        return self._ssh_exec(target, cmd, image)

    def _build_commands(self, target: DeployTarget, image: str, project: str, tag: str) -> str:
        template = target.options.get("commands", "")
        if not template:
            return "echo 'ERROR: 未配置自定义命令' && exit 1"
        return template.replace("{image}", image).replace("{tag}", tag).replace("{project}", project)

    def _build_ansible(self, target: DeployTarget, image: str, project: str, tag: str) -> str:
        if not target.path:
            return "echo 'ERROR: 缺少 Ansible playbook 路径' && exit 1"
        return (
            f"ansible-playbook {target.path}"
            f" -e image={image} -e tag={tag} -e project={project}"
        )

    def _ssh_exec(self, target: DeployTarget, cmd: str, image: str) -> DeployResult:
        try:
            from .base import ssh_connect
            ssh = ssh_connect(target, settings.ssh_timeout)
            _, stdout, stderr = ssh.exec_command(cmd)
            out = stdout.read().decode().strip()
            err = stderr.read().decode().strip()
            ssh.close()

            output = (err or out)[:settings.log_truncate_chars]
            is_ok = not err or "error" not in err.lower()

            return DeployResult(
                image=image,
                status="ok" if is_ok else "failed",
                output=output,
            )
        except Exception as e:
            return DeployResult(image=image, status="failed", output=str(e))

    def validate(self, target: DeployTarget) -> str | None:
        if not target.host:
            return "目标主机不能为空"
        if target.mode == "ansible" and not target.path:
            return "Ansible 模式需要 playbook 路径"
        if not target.options.get("commands"):
            return "需要填写部署命令"
        return None
