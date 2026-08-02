"""SSH 单机部署器 — 纯透传，不硬编码任何工具命令"""

import shlex
from .base import Deployer, DeployTarget, DeployResult, ssh_session, _exec_on, ssh_exec_stream
from backend.config import settings
from backend.deploy_log import S


class SSHDeployer(Deployer):
    """SSH 部署：用户输入什么命令就原样执行，不做任何前置检查或工具假设"""

    def name(self) -> str:
        return "ssh"

    def deploy(
        self, target: DeployTarget, image: str, project: str, tag: str,
        callback=None,
    ) -> DeployResult:
        if not target.host:
            return DeployResult(image=image, status="failed", output="Missing target host")

        if target.mode == "ansible":
            cmd = self._build_ansible(target, image, project, tag)
        else:
            cmd = self._build_commands(target, image, project, tag)

        try:
            with ssh_session(target, settings.ssh_timeout) as ssh:
                self._log(callback, S("deploy_log.ssh_exec_start"))
                output = self._ssh_exec_stream(ssh, cmd, callback)
                self._log(callback, S("deploy_log.ssh_exec_done"))

            return DeployResult(image=image, status="ok", output=output)
        except Exception as e:
            self._log(callback, S("deploy_log.deploy_error", error=str(e)))
            return DeployResult(image=image, status="failed", output=str(e))

    def _build_commands(self, target: DeployTarget, image: str, project: str, tag: str) -> str:
        template = target.options.get("commands", "")
        if not template:
            return "echo 'ERROR: Custom commands not configured' && exit 1"
        image_name = image.split(":")[0]
        return template.replace("{image}", image).replace("{image_name}", image_name).replace("{tag}", tag).replace("{project}", project)

    def _build_ansible(self, target: DeployTarget, image: str, project: str, tag: str) -> str:
        if not target.path:
            return "echo 'ERROR: Missing Ansible playbook path' && exit 1"
        inv = target.options.get("inventory", "")
        inv_flag = f"-i {inv}" if inv else ""
        return (
            f"ansible-playbook {inv_flag} {shlex.quote(target.path)}"
            f" -e image={shlex.quote(image)} -e tag={shlex.quote(tag)} -e project={shlex.quote(project)}"
        ).strip()

    def _ssh_exec_stream(self, ssh, cmd: str, callback) -> str:
        """实时流式执行命令（委托给共享实现）"""
        return ssh_exec_stream(ssh, cmd, lambda msg: self._log(callback, msg))

    def stop(self, target: DeployTarget, project: str, **kwargs) -> dict:
        """停止服务：执行用户自定义的停止命令（替换模板变量后执行）"""
        commands = kwargs.get("commands", "")
        tag = kwargs.get("tag", "")
        if not commands:
            return {"success": False, "output": "SSH stop: no stop commands configured"}
        image = f"{project}:{tag}"
        image_name = project.split("/")[-1] if "/" in project else project
        cmd = commands.replace("{image}", image).replace("{image_name}", image_name)\
                     .replace("{tag}", tag).replace("{project}", project)
        try:
            with ssh_session(target, settings.ssh_timeout) as ssh:
                _, stdout, stderr = ssh.exec_command(cmd, timeout=settings.ssh_timeout)
                out = stdout.read().decode(errors="replace").strip()
                err = stderr.read().decode(errors="replace").strip()
                return {"success": True, "output": (err or out)[:settings.log_truncate_chars]}
        except Exception as ex:
            return {"success": False, "output": str(ex)}

    def validate(self, target: DeployTarget) -> str | None:
        if not target.host:
            return "SSH target host is required"
        if target.mode == "ansible":
            if not target.path:
                return "Ansible playbook path is required"
        elif not target.options.get("commands"):
            return "Deploy commands are required"
        return None
