"""SSH 单机部署器 — 纯透传，不硬编码任何工具命令"""

import re

from .base import Deployer, DeployTarget, DeployResult, ssh_session, _exec_on
from backend.config import settings
from backend.deploy_log import S

# 进度条特征：终端用 \r 刷新，形如 "[==>                ]" 或 "[=====>     ]"
_PROGRESS_BAR = re.compile(r'\[[=> ]+\]')


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
            f"ansible-playbook {inv_flag} {target.path}"
            f" -e image={image} -e tag={tag} -e project={project}"
        ).strip()

    def _ssh_exec_stream(self, ssh, cmd: str, callback) -> str:
        """实时流式执行命令，批量推送输出。

        有数据时全速读，没数据时才 sleep，保证高吞吐。
        """
        import time, re
        ANSI_RE = re.compile(chr(27) + r'\[[0-9;?]*[A-Za-z]')
        BATCH = 50
        channel = ssh.get_transport().open_session()
        try:
            channel.exec_command(cmd)
            all_output = []
            buffer = []
            buf_size = 65536

            def _clean(line: str) -> str:
                # 去掉 ANSI 转义码，\r 分段智能去重：
                # - 连续的进度条段（如 "[==>   ]" → "[====> ]"）只保留最后一段
                # - 其余所有内容全部保留，不写死任何关键词
                line = ANSI_RE.sub('', line)
                segments = [p.strip() for p in line.split('\r') if p.strip()]
                if not segments:
                    return ""
                if len(segments) == 1:
                    return segments[0]
                kept = []
                for seg in segments:
                    if not kept:
                        kept.append(seg)
                        continue
                    prev = kept[-1]
                    # 相邻两段都是进度条 → 替换（去重）
                    if _PROGRESS_BAR.search(seg) and _PROGRESS_BAR.search(prev):
                        kept[-1] = seg
                    else:
                        kept.append(seg)
                return "\n".join(kept) if kept else ""

            def _flush():
                if buffer:
                    self._log(callback, "\n".join(buffer))
                    buffer.clear()

            while not channel.exit_status_ready():
                had_data = False
                if channel.recv_ready():
                    data = channel.recv(buf_size).decode("utf-8", errors="replace")
                    for line in data.split("\n"):
                        line = _clean(line)
                        if line:
                            all_output.append(line)
                            buffer.append(line)
                            if len(buffer) >= BATCH:
                                _flush()
                    had_data = True
                if channel.recv_stderr_ready():
                    err_data = channel.recv_stderr(buf_size).decode("utf-8", errors="replace")
                    for line in err_data.split("\n"):
                        line = _clean(line)
                        if line:
                            all_output.append(line)
                            buffer.append(line)
                            if len(buffer) >= BATCH:
                                _flush()
                    had_data = True
                if not had_data:
                    time.sleep(0.005)

            while channel.recv_ready():
                data = channel.recv(buf_size).decode("utf-8", errors="replace")
                for line in data.split("\n"):
                    line = _clean(line)
                    if line:
                        all_output.append(line)
                        buffer.append(line)
            _flush()
            return "\n".join(all_output)
        finally:
            channel.close()

    def validate(self, target: DeployTarget) -> str | None:
        if not target.host:
            return "SSH target host is required"
        if target.mode == "ansible":
            if not target.path:
                return "Ansible playbook path is required"
        elif not target.options.get("commands"):
            return "Deploy commands are required"
        return None
