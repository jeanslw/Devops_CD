"""部署器抽象基类"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


def ssh_connect(target: "DeployTarget", timeout: int):
    """统一的 SSH 连接，有密码用密码，没有则用默认 key"""
    import paramiko
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs = dict(
        hostname=target.host, port=target.port, username=target.user,
        timeout=timeout,
    )
    if target.password:
        kwargs["password"] = target.password
    ssh.connect(**kwargs)
    return ssh


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
    path: str = ""                  # compose路径 / K8s YAML路径 / Ansible playbook路径
    mode: str = ""                  # docker | commands | ansible (SSH) / remote | commands (Compose)
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
        self, target: DeployTarget, image: str, project: str, tag: str
    ) -> DeployResult:
        """执行部署，同步方法（由调用方负责线程池包装）"""
        ...

    def validate(self, target: DeployTarget) -> Optional[str]:
        """校验目标参数是否有效，返回 None 通过，否则返回错误信息"""
        return None

    def supports(self, deploy_type: str) -> bool:
        """是否匹配部署类型"""
        return deploy_type == self.name()
