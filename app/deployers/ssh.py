"""SSH 单机部署器 — 自定义命令 / Ansible Playbook"""

from .base import Deployer, DeployTarget, DeployResult, ssh_session, _exec_on
from app.config import settings


class SSHDeployer(Deployer):
    """SSH 部署，支持 commands / ansible 两种模式"""

    def name(self) -> str:
        return "ssh"

    def deploy(
        self, target: DeployTarget, image: str, project: str, tag: str,
        callback=None,
    ) -> DeployResult:
        if not target.host:
            return DeployResult(image=image, status="failed", output="缺少目标主机")

        if target.mode == "ansible":
            cmd = self._build_ansible(target, image, project, tag)
        else:
            cmd = self._build_commands(target, image, project, tag)

        project_short = project.split("/")[-1]
        try:
            with ssh_session(target, settings.ssh_timeout) as ssh:
                if target.path:
                    self._log(callback, "正在验证路径...")
                    path_check = self._ssh_exec(ssh, f"test -d {target.path} && echo 'OK' || echo 'NOT_FOUND'", image)
                    if path_check.output.strip() != "OK":
                        self._log(callback, f"❌ 部署失败：路径不存在或路径错误 - {target.path}")
                        return DeployResult(image=image, status="failed", output=f"部署失败：路径不存在或路径错误 - {target.path}")
                    self._log(callback, f"✅ 路径验证通过: {target.path}")

                self._log(callback, "正在查看当前运行版本...")
                before = self._ssh_exec(ssh,
                    f"docker ps --format '{{{{.Names}}}} {{{{.Image}}}}' 2>/dev/null | grep -F '{project_short}'", image)
                self._log(callback, f"当前运行版本:\n{before.output or '(无)'}")

                self._log(callback, "\n正在验证应用一致性...")
                if not before.output.strip():
                    all_containers = self._ssh_exec(ssh, "docker ps --format '{{{{.Names}}}}' 2>/dev/null", image)
                    running_names = all_containers.output.strip()
                    if running_names:
                        self._log(callback, f"❌ 部署失败：未找到应用 [{project_short}]，当前运行的容器：\n{running_names}")
                        return DeployResult(image=image, status="failed", output=f"部署失败：未找到应用 [{project_short}]，当前运行的容器：\n{running_names}")
                    else:
                        self._log(callback, f"⚠️ 未检测到运行中的容器，将首次部署 [{project_short}]")
                else:
                    self._log(callback, f"✅ 应用 [{project_short}] 验证通过")

                self._log(callback, "\n开始部署...")
                deploy_text = self._ssh_exec_stream(ssh, cmd, callback)
                if "error" in deploy_text.lower() or not deploy_text.strip():
                    result = DeployResult(image=image, status="failed",
                        output=f"当前运行版本:\n{before.output or '(无)'}\n\n开始部署:\n{deploy_text}\n\n验证部署: ❌ 部署失败！")
                    self._log(callback, "\n验证部署: ❌ 部署失败！")
                else:
                    result = DeployResult(image=image, status="ok",
                        output=f"当前运行版本:\n{before.output or '(无)'}\n\n开始部署:\n{deploy_text}")
                    self._log(callback, "\n✅ 命令执行完成！")
        except Exception as e:
            self._log(callback, f"\n❌ 部署失败: {e}")
            return DeployResult(image=image, status="failed", output=str(e))
        return result

    def _build_commands(self, target: DeployTarget, image: str, project: str, tag: str) -> str:
        template = target.options.get("commands", "")
        if not template:
            return "echo 'ERROR: 未配置自定义命令' && exit 1"
        image_name = image.split(":")[0]
        return template.replace("{image}", image).replace("{image_name}", image_name).replace("{tag}", tag).replace("{project}", project)

    def _build_ansible(self, target: DeployTarget, image: str, project: str, tag: str) -> str:
        if not target.path:
            return "echo 'ERROR: 缺少 Ansible playbook 路径' && exit 1"
        inv = target.options.get("inventory", "")
        inv_flag = f"-i {inv}" if inv else ""
        return (
            f"ansible-playbook {inv_flag} {target.path}"
            f" -e image={image} -e tag={tag} -e project={project}"
        ).strip()

    def _ssh_exec(self, ssh, cmd: str, image: str) -> DeployResult:
        try:
            out, err = _exec_on(ssh, cmd)
            output = (err or out)[:settings.log_truncate_chars]
            is_ok = not err or "error" not in err.lower()

            return DeployResult(
                image=image,
                status="ok" if is_ok else "failed",
                output=output,
            )
        except Exception as e:
            return DeployResult(image=image, status="failed", output=str(e))

    def _ssh_exec_stream(self, ssh, cmd: str, callback) -> str:
        """实时流式执行命令，边执行边通过 callback 推送输出"""
        channel = ssh.get_transport().open_session()
        try:
            channel.exec_command(cmd)

            all_output = []
            while not channel.exit_status_ready():
                if channel.recv_ready():
                    data = channel.recv(4096).decode("utf-8", errors="replace")
                    for line in data.split("\n"):
                        line = line.strip()
                        if line:
                            self._log(callback, line)
                            all_output.append(line)
                if channel.recv_stderr_ready():
                    err_data = channel.recv_stderr(4096).decode("utf-8", errors="replace")
                    for line in err_data.split("\n"):
                        line = line.strip()
                        if line:
                            self._log(callback, line)
                            all_output.append(line)

            # 收尾残余数据
            while channel.recv_ready():
                data = channel.recv(4096).decode("utf-8", errors="replace")
                for line in data.split("\n"):
                    line = line.strip()
                    if line:
                        self._log(callback, line)
                        all_output.append(line)

            return "\n".join(all_output)
        finally:
            channel.close()

    def validate(self, target: DeployTarget) -> str | None:
        if not target.host:
            return "目标主机不能为空"
        if target.mode == "ansible":
            if not target.path:
                return "Ansible 模式需要 playbook 路径"
        elif not target.options.get("commands"):
            return "需要填写部署命令"
        return None
