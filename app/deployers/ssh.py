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

        filter_str = target.options.get("filter", "")
        before = self._ssh_exec(target,
            f"docker ps --format '{{{{.Names}}}} {{{{.Image}}}}' 2>/dev/null", image)
        result = self._ssh_exec(target, cmd, image)
        deploy_text = result.output
        verify_cmd = target.options.get("verify", "")
        if verify_cmd:
            verify_cmd = verify_cmd.replace("{image}", image).replace("{tag}", tag).replace("{project}", project)
            verify = self._ssh_exec(target, verify_cmd, image)
            result.output = f"当前运行版本:\n{before.output or '(无)'}\n\n开始部署:\n{deploy_text}\n\n部署完成！\n\n查看应用新版本:\n{verify.output or '(无输出)'}"
            if verify.output and verify.output.strip():
                result.status = "ok"
                result.output += f"\n\n验证部署: ✅ 部署成功！"
            else:
                result.status = "failed"
                result.output += f"\n\n验证部署: ❌ 部署失败！"
        else:
            self._ssh_exec(target, "sleep 3", image)
            verify = self._ssh_exec(target,
                f"docker ps -a --format '{{{{.Names}}}} {{{{.Image}}}}' 2>/dev/null", image)
            result.output = f"当前运行版本:\n{before.output or '(无)'}\n\n开始部署:\n{deploy_text}\n\n部署完成！\n\n查看应用新版本:\n{verify.output or '(无运行中容器)'}"
            if verify.output and tag in verify.output:
                result.status = "ok"
                result.output += f"\n\n验证部署: ✅ 部署成功！"
            elif "error" in deploy_text.lower() or not deploy_text.strip():
                result.status = "failed"
                result.output += f"\n\n验证部署: ❌ 部署失败！"
            else:
                result.status = "failed"
                result.output += f"\n\n验证部署: ❌ 部署失败！(版本不匹配)"
        return result

    def _build_commands(self, target: DeployTarget, image: str, project: str, tag: str) -> str:
        template = target.options.get("commands", "")
        if not template:
            return "echo 'ERROR: 未配置自定义命令' && exit 1"
        return template.replace("{image}", image).replace("{tag}", tag).replace("{project}", project)

    def _build_ansible(self, target: DeployTarget, image: str, project: str, tag: str) -> str:
        if not target.path:
            return "echo 'ERROR: 缺少 Ansible playbook 路径' && exit 1"
        inv = target.options.get("inventory", "")
        inv_flag = f"-i {inv}" if inv else ""
        return (
            f"ansible-playbook {inv_flag} {target.path}"
            f" -e image={image} -e tag={tag} -e project={project}"
        ).strip()

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
        if target.mode == "ansible":
            if not target.path:
                return "Ansible 模式需要 playbook 路径"
        elif not target.options.get("commands"):
            return "需要填写部署命令"
        return None
