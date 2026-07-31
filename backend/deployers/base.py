"""部署器抽象基类"""

import os
import re
import time
import tempfile
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Optional, Tuple


def ssh_connect(target: "DeployTarget", timeout: int):
    """统一的 SSH 连接。
    优先级: ssh_key > password > 系统默认 key
    """
    from backend.config import settings
    import paramiko
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs = dict(hostname=target.host, port=target.port, username=target.user, timeout=timeout)
    tmp_file = None

    if target.ssh_key:
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False)
        tmp.write(target.ssh_key)
        tmp.close()
        tmp_file = tmp.name
        os.chmod(tmp_file, 0o600)  # paramiko 要求私钥文件权限严格
        kwargs["key_filename"] = tmp_file
    elif target.password:
        kwargs["password"] = target.password

    try:
        ssh.connect(**kwargs)
        if settings.ssh_keepalive > 0:
            transport = ssh.get_transport()
            if transport:
                transport.set_keepalive(settings.ssh_keepalive)
    finally:
        if tmp_file:
            try:
                os.unlink(tmp_file)
            except FileNotFoundError:
                pass
    return ssh


@contextmanager
def ssh_session(target: "DeployTarget", timeout: int):
    """SSH 会话上下文管理器，复用同一连接执行多条命令"""
    ssh = ssh_connect(target, timeout)
    try:
        yield ssh
    finally:
        ssh.close()


def _exec_on(ssh, cmd: str) -> Tuple[str, str, int]:
    """在已建立的 SSH 连接上执行单条命令，返回 (stdout, stderr, exit_code)"""
    _, stdout, stderr = ssh.exec_command(cmd)
    o = stdout.read().decode(errors="replace").strip()
    e = stderr.read().decode(errors="replace").strip()
    exit_code = stdout.channel.recv_exit_status()
    return o, e, exit_code


def _ssh_cmd(ssh, cmd: str) -> str:
    """执行 SSH 命令，返回 stdout+stderr 合并字符串（去重 fallback）"""
    o, e, _ = _exec_on(ssh, cmd)
    return o or e


# 进度条特征：终端用 \r 刷新，形如 "[==>                ]" 或 "[=====>     ]"
_PROGRESS_BAR = re.compile(r'\[[=> ]+\]')


def ssh_exec_stream(ssh, cmd: str, log_fn) -> str:
    """实时流式执行命令，批量推送输出。

    有数据时全速读，没数据时才 sleep，保证高吞吐。
    log_fn 签名: log_fn(message: str)
    """
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
            # 快速路径：不含 \r 的普通行原样返回，避免误进分段逻辑
            if '\r' not in line:
                return line
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
                log_fn("\n".join(buffer))
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

        # 阻塞等待退出状态码接收完毕（确保远端已刷新所有 stdout/stderr）
        try:
            channel.recv_exit_status()
        except Exception:
            pass

        # 补读残留数据，最多重试 5 次（含短暂延时，应对网络延迟导致的数据滞后到达）
        for _ in range(5):
            had = False
            while channel.recv_ready():
                data = channel.recv(buf_size).decode("utf-8", errors="replace")
                for line in data.split("\n"):
                    line = _clean(line)
                    if line:
                        all_output.append(line)
                        buffer.append(line)
                had = True
            while channel.recv_stderr_ready():
                err_data = channel.recv_stderr(buf_size).decode("utf-8", errors="replace")
                for line in err_data.split("\n"):
                    line = _clean(line)
                    if line:
                        all_output.append(line)
                        buffer.append(line)
                had = True
            if not had:
                break
            time.sleep(0.01)
        _flush()
        return "\n".join(all_output)
    finally:
        channel.close()


@dataclass
class DeployTarget:
    """部署目标配置

    SSH 模式 (mode):
      - docker:   docker pull/stop/rm/run (默认)
      - commands: options["commands"] 自定义脚本，{image} {tag} {project} 会被替换
      - ansible:  ansible-playbook {path} -e image={image} tag={tag} project={project}

    Compose 模式:
      - remote: cd {path} && IMAGE_TAG={tag} docker-compose up -d (默认)
      - commands: options["commands"]

    K8s 模式:
      - apply: kubectl apply -f {path} (默认，有 path 时)
      - setimage: kubectl set image (无 path 时兜底)
    """
    host: str = ""
    port: int = 22
    user: str = "root"
    password: str = ""
    ssh_key: str = ""              # SSH 私钥内容（PEM 格式）
    path: str = ""                 # compose路径 / K8s YAML路径 / Ansible playbook路径
    mode: str = ""                 # docker | commands | ansible (SSH) / remote | commands (Compose)
    options: dict = field(default_factory=dict)  # commands / namespace / deployment / container


@dataclass
class DeployResult:
    image: str
    status: str                     # ok | failed | skipped
    output: str = ""


class Deployer(ABC):
    """部署器抽象基类 —— 所有部署策略实现此接口
    对齐 PHP 项目的 BuildProviderInterface 设计模式
    """

    @abstractmethod
    def name(self) -> str:
        """唯一标识符：ssh | compose | k8s"""
        ...

    @abstractmethod
    def deploy(
        self, target: DeployTarget, image: str, project: str, tag: str,
        callback=None,
    ) -> DeployResult:
        """执行部署，同步方法（由调用方负责线程池包装）
        callback: 可选回调函数，用于实时推送部署进度，签名: callback(message)
        """
        ...

    def validate(self, _target: DeployTarget) -> Optional[str]:
        """校验目标参数是否有效，返回 None 通过，否则返回错误信息"""
        return None

    def supports(self, deploy_type: str) -> bool:
        """是否匹配部署类型"""
        return deploy_type == self.name()

    def stop(self, target: DeployTarget, project: str, **kwargs) -> dict:
        """停止服务。返回 {"success": bool, "output": str}。
        默认抛出 NotImplementedError，子类按需覆盖。
        """
        raise NotImplementedError(f"{self.name()} deployer does not support stop")

    def _log(self, callback, message):
        """辅助方法：调用回调输出日志，如果 callback 为 None 则忽略"""
        if callable(callback):
            callback(message)
