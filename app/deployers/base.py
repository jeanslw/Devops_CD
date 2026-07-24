"""部署器抽象基类"""

import os
import tempfile
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Optional, Tuple


def ssh_connect(target: "DeployTarget", timeout: int):
    """统一的 SSH 连接。
    优先级: ssh_key > password > 系统默认 key
    """
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
        kwargs["key_filename"] = tmp_file
    elif target.password:
        kwargs["password"] = target.password

    try:
        ssh.connect(**kwargs)
    finally:
        if tmp_file:
            os.unlink(tmp_file)
    return ssh


@contextmanager
def ssh_session(target: "DeployTarget", timeout: int):
    """SSH 会话上下文管理器，复用同一连接执行多条命令"""
    ssh = ssh_connect(target, timeout)
    try:
        yield ssh
    finally:
        ssh.close()


def _exec_on(ssh, cmd: str) -> Tuple[str, str]:
    """在已建立的 SSH 连接上执行单条命令，返回 (stdout, stderr)"""
    _, stdout, stderr = ssh.exec_command(cmd)
    return stdout.read().decode().strip(), stderr.read().decode().strip()


@dataclass
class DeployTarget:
    """部署目标配置

    SSH 模式 (mode):
      - docker:   docker pull/stop/rm/run (默认)
      - commands: options["commands"] 自定义脚本，{image} {tag} {project} 会被替换
      - ansible:  ansible-playbook {path} -e image={image} tag={tag} project={project}

    Compose 模式:
      - remote: cd {path} && IMAGE_TAG={tag} docker compose up -d (默认)
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

    def _log(self, callback, message):
        """辅助方法：调用回调输出日志，如果 callback 为 None 则忽略"""
        if callable(callback):
            callback(message)
