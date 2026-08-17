"""部署器抽象基类"""

import logging
import os
import re
import tempfile
import time
from abc import ABC, abstractmethod
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field

from backend.deploy_run import DeployCancelled, get_cancel_checker

logger = logging.getLogger(__name__)


def _known_hosts_file() -> str:
    """返回 known_hosts 文件路径（固定在 ~/.cd_service/known_hosts）。"""
    cd_dir = os.path.join(os.path.expanduser("~"), ".cd_service")
    os.makedirs(cd_dir, exist_ok=True)
    return os.path.join(cd_dir, "known_hosts")


def _save_first_host_key(host: str, port: int, timeout: int) -> None:
    """通过 Transport 层取远端 host key 并存入 known_hosts（无 AutoAddPolicy）。

    仅用于 trust=True 场景（用户主动点"测试连接/信任"）。不验证凭据，
    只取 host key。调用方需要在保存后重新发起 SSH 连接。
    """
    import socket

    import paramiko

    sock: socket.socket | None = None
    transport: paramiko.Transport | None = None
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        transport = paramiko.Transport(sock)
        transport.start_client(timeout=timeout)
        key = transport.get_remote_server_key()
        if not key:
            raise RuntimeError("无法获取远端 SSH 主机密钥")

        kh_file = _known_hosts_file()
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        if os.path.exists(kh_file):
            client.load_host_keys(kh_file)
        host_keys = client.get_host_keys()
        host_keys.add(host, key.get_name(), key)
        if f"[{host}]:{port}" != host:
            host_keys.add(f"[{host}]:{port}", key.get_name(), key)
        client.save_host_keys(kh_file)
        client.close()
    finally:
        if transport is not None:
            with suppress(Exception):
                transport.close()
        if sock is not None:
            with suppress(Exception):
                sock.close()


def trust_ssh_host(host: str, port: int, username: str = "", password: str = "", ssh_key: str = "", timeout: int = 30) -> dict:
    """信任 SSH 主机，获取并保存主机密钥。返回 {success, key_fingerprint, message}

    使用 Transport 层直接握手取 host key，避免 AutoAddPolicy（消除中间人攻击告警）。
    流程：
      1. 匿名 Transport 层握手 → 取 host key（无需凭据）
      2. 保存 host key 到 known_hosts（信任主机的核心目的，到此已完成）
      3. 有凭据则顺便验证可登录；无凭据或凭据失败仅附加 warning（不影响信任成功）
    """
    import base64
    import paramiko
    import socket
    from hashlib import sha256

    key = None
    sock = None
    transport = None
    tmp_file = None
    ssh = None
    try:
        # 步骤 1：通过 Transport 层直接握手获取远端 host key（无需凭据，无 AutoAddPolicy）
        sock = socket.create_connection((host, port), timeout=timeout)
        transport = paramiko.Transport(sock)
        transport.start_client(timeout=timeout)
        key = transport.get_remote_server_key()
        transport.close()
        transport = None
        sock = None

        if not key:
            raise RuntimeError("无法获取远端 SSH 主机密钥")

        # 计算指纹
        fp = sha256(key.asbytes()).digest()
        fingerprint = base64.b64encode(fp).decode().rstrip("=")

        # 步骤 2：写入 known_hosts（使用 RejectPolicy + 手动 load_host_keys，不使用 AutoAddPolicy）
        kh_file = _known_hosts_file()
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        if os.path.exists(kh_file):
            client.load_host_keys(kh_file)
        host_key_entry = client.get_host_keys()
        host_key_entry.add(host, key.get_name(), key)
        if f"[{host}]:{port}" != host:
            host_key_entry.add(f"[{host}]:{port}", key.get_name(), key)
        client.save_host_keys(kh_file)
        client.close()

        # 步骤 3：有凭据则顺便验证可连通；失败只记警告（信任本身已成功）
        warning = ""
        if ssh_key or password:
            try:
                connect_kwargs = {"hostname": host, "port": port, "timeout": timeout, "allow_agent": False, "look_for_keys": False}
                if ssh_key:
                    with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as tmp:
                        tmp.write(ssh_key)
                        tmp_file = tmp.name
                    os.chmod(tmp_file, 0o600)
                    connect_kwargs["key_filename"] = tmp_file
                    connect_kwargs["username"] = username or "root"
                else:  # password
                    connect_kwargs["username"] = username or "root"
                    connect_kwargs["password"] = password

                ssh = paramiko.SSHClient()
                ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
                ssh.load_host_keys(kh_file)
                ssh.connect(**connect_kwargs)  # type: ignore[arg-type]
            except Exception as e:
                logger.warning("trust_ssh_host: host key saved but credential check failed", exc_info=e)
                if isinstance(e, paramiko.AuthenticationException):
                    warning = "（主机密钥已保存，但凭据验证失败，请检查用户名/密码/密钥）"
                else:
                    warning = f"（主机密钥已保存，但连接验证失败：{e}）"

        return {
            "success": True,
            "key_fingerprint": f"SHA256:{fingerprint}",
            "message": f"已信任主机 {host}:{port}，指纹: SHA256:{fingerprint}{warning}"
        }
    except Exception as e:
        logger.error("SSH host trust failed", exc_info=e)
        msg = str(e) if isinstance(e, (paramiko.AuthenticationException, paramiko.SSHException, OSError, RuntimeError)) else "连接失败，请检查主机是否可达"
        return {"success": False, "message": msg}
    finally:
        if tmp_file:
            with suppress(FileNotFoundError):
                os.unlink(tmp_file)
        if transport is not None:
            with suppress(Exception):
                transport.close()
        if sock is not None:
            with suppress(Exception):
                sock.close()
        if ssh is not None:
            with suppress(Exception):
                ssh.close()


def ssh_connect(target: "DeployTarget", timeout: int, trust: bool = False):
    """统一的 SSH 连接。
    优先级: ssh_key > password > system 默认 key
    全程使用 RejectPolicy + known_hosts，无 AutoAddPolicy（避免中间人攻击）。
    unknown host: 用户必须先调用 trust_ssh_host 把 host key 存入 ~/.cd_service/known_hosts。
    """
    import paramiko

    from backend.config import settings
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.RejectPolicy())

    # 加载已信任的主机密钥（trust=True 和 正常部署路径都用 known_hosts）
    kh_file = _known_hosts_file()
    if os.path.exists(kh_file):
        ssh.load_host_keys(kh_file)

    kwargs = {"hostname": target.host, "port": target.port, "username": target.user, "timeout": timeout}
    tmp_file = None

    if target.ssh_key:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as tmp:
            tmp.write(target.ssh_key)
            tmp_file = tmp.name
        os.chmod(tmp_file, 0o600)
        kwargs["key_filename"] = tmp_file
    elif target.password:
        kwargs["password"] = target.password

    try:
        try:
            ssh.connect(**kwargs)  # type: ignore[arg-type]
        except paramiko.ssh_exception.SSHException as e:
            # RejectPolicy 不认识 host 时会抛 SSHException("Server ... not found in known_hosts")
            # 仅在用户明确 trust=True（前端点了"测试连接/信任"按钮）时，先存 host key 再重试
            if trust and "not found in known_hosts" in str(e).lower():
                _save_first_host_key(target.host, target.port, timeout)
                ssh.close()
                ssh = paramiko.SSHClient()
                ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
                kh_file = _known_hosts_file()
                if os.path.exists(kh_file):
                    ssh.load_host_keys(kh_file)
                ssh.connect(**kwargs)  # type: ignore[arg-type]
            else:
                raise

        if settings.ssh_keepalive > 0:
            transport = ssh.get_transport()
            if transport:
                transport.set_keepalive(settings.ssh_keepalive)
    finally:
        if tmp_file:
            with suppress(FileNotFoundError):
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


def _exec_on(ssh, cmd: str) -> tuple[str, str, int]:
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

        check_cancel = get_cancel_checker()
        while not channel.exit_status_ready():
            # 部署取消检查：用户调用 cancel 接口后，尽快中断远端长命令
            if check_cancel is not None and check_cancel():
                log_fn("\n[deploy cancelled by user]")
                raise DeployCancelled()
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
        with suppress(Exception):
            channel.recv_exit_status()

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

    def validate(self, _target: DeployTarget) -> str | None:
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
