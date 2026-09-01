"""Web Shell + SCP 文件上传"""

import asyncio
import base64
import json
import os
import posixpath
import re
import shlex

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect

from backend.auth import (
    _check_disabled,
    _query_permissions,
    _query_user_with_systems,
    _timing_safe_compare,
    get_db,
    require_perm,
)
from backend.config import settings
from backend.crypto import decrypt
from backend.database import Database
from backend.deployers.base import DeployTarget, ssh_connect
from backend.exceptions import NotFoundError, ValidationError
from backend.responses import ok

router = APIRouter()

# ── 危险命令黑名单（正则匹配，检测完整命令行）──
_DANGEROUS_PATTERNS = [
    r"\brm\s+.*-rf\s+/",  # rm -rf /
    r"\brm\s+.*-rf\s+~",  # rm -rf ~
    r"\bdd\s+if=",  # dd if=/dev/zero of=/dev/sda
    r"\bmkfs\.",  # mkfs.ext4 /dev/sda
    r":\(\)\s*\{.*:\|:&\s*\};:",  # fork bomb
    r">\s*/dev/sd",  # redirect to disk device
    r"\bchmod\s+.*777\s+/",  # chmod 777 /
    r">\s*/etc/passwd",  # overwrite system files
    r"\brm\s+.*--no-preserve-root\s+/",  # rm --no-preserve-root /
    r"\bchown\s+.*-R\s+\w+\s+/",  # chown -R on /
    r"\bchmod\s+.*-R\s+\d+\s+/etc",  # chmod on /etc
]

_DANGEROUS_WARNING = "\r\n⚠️  危险命令已被拦截，未发送到服务器\r\n"


def _check_dangerous(cmd: str) -> bool:
    """检测命令行是否匹配危险模式"""
    stripped = cmd.strip()
    if not stripped:
        return False
    return any(re.search(pattern, stripped) for pattern in _DANGEROUS_PATTERNS)


async def _ws_verify(token: str | None = None) -> str:
    """WebSocket 鉴权：通过 query param token 校验（O(1) 查询）"""
    if not token:
        raise HTTPException(401, "请登录")
    try:
        decoded = base64.b64decode(token).decode()
        username, _, _hash = decoded.partition(":")
    except Exception as e:
        raise HTTPException(401, "token 无效") from e

    db = get_db()
    with db.conn() as conn:
        row = _query_user_with_systems(conn, username, "username, password_hash, status")
        if row is None:
            raise HTTPException(401, "token 无效")
        # 停用账号即时踢下线：WebSocket（WebShell）是独立鉴权路径，必须与 REST 一致校验 status
        _check_disabled(row)
        expected = base64.b64encode(f"{row['username']}:{row['password_hash']}".encode()).decode()
        if not _timing_safe_compare(token, expected):
            raise HTTPException(401, "token 无效")
        return row["username"]


@router.websocket("/ws/terminal/{server_id}")
async def terminal(websocket: WebSocket, server_id: int):
    # 从 query string 获取 token 并校验
    token = websocket.query_params.get("token")
    try:
        username = await _ws_verify(token)
    except HTTPException:
        await websocket.close(code=4001, reason="鉴权失败")
        return

    # 权限检查：需要 cd.webshell
    db = get_db()
    with db.conn() as conn:
        row = conn.execute("SELECT role FROM admin_users WHERE username=?", (username,)).fetchone()
        if row:
            perms = _query_permissions(db, row["role"])
            if row["role"] != settings.super_admin_role and "cd.webshell" not in perms:
                await websocket.close(code=4003, reason="权限不足")
                return

    await websocket.accept()

    # 查服务器
    db = get_db()
    with db.conn() as conn:
        srv = conn.execute("SELECT * FROM cd_servers WHERE id=?", (server_id,)).fetchone()
    if not srv:
        await websocket.send_text("\r\n❌ 服务器不存在\r\n")
        await websocket.close()
        return

    target = DeployTarget(
        host=srv["host"],
        port=srv["port"],
        user=srv["user"],
        password=decrypt(srv["password"] or ""),
        ssh_key=decrypt(srv["ssh_key"] or ""),
    )

    # SSH 连接（在线程池中执行，避免阻塞事件循环）
    try:
        ssh = await asyncio.to_thread(ssh_connect, target, settings.ssh_timeout)
    except Exception as e:
        await websocket.send_text(f"\r\n❌ SSH 连接失败: {e}\r\n")
        await websocket.close()
        return

    # SSH 已连上，主动通知前端（onopen 只是 WebSocket 握手成功，不代表 SSH 通）
    chan = await asyncio.to_thread(ssh.invoke_shell, term="xterm-256color", width=100, height=28)
    assert chan is not None, "SSH shell channel 创建失败"
    chan.settimeout(0.0)

    async def ssh_to_ws():
        """SSH 输出 → WebSocket"""
        while not chan.closed:
            try:
                if chan.recv_ready():
                    data = chan.recv(4096)
                    if data:
                        await websocket.send_bytes(data)
                await asyncio.sleep(0.02)
            except Exception:
                break

    async def ws_to_ssh():
        """WebSocket 输入 → SSH，支持终端尺寸自适应 + 危险命令拦截"""
        buf = ""  # 行缓冲
        while not chan.closed:
            try:
                data = await asyncio.wait_for(websocket.receive(), timeout=0.05)
                if data["type"] == "websocket.receive":
                    if "text" in data:
                        text = data["text"]
                        # 终端尺寸自适应（前端 xterm.js 发送 JSON resize 事件）
                        if text.startswith("{"):
                            try:
                                msg = json.loads(text)
                                if msg.get("type") == "resize":
                                    chan.resize_pty(width=msg.get("cols", 100), height=msg.get("rows", 28))
                                    continue
                            except Exception:
                                pass

                        # ── 危险命令检测 ──
                        if "\r" in text:
                            line = buf + text
                            if _check_dangerous(line):
                                buf = ""
                                await websocket.send_text(_DANGEROUS_WARNING)
                                continue
                            buf = ""
                        elif "\x03" in text or "\x04" in text:
                            # Ctrl+C / Ctrl+D，清空缓冲区
                            buf = ""
                        elif text.startswith("\x1b"):
                            # ANSI 转义序列（方向键等），保持缓冲区
                            pass
                        elif text == "\x7f" or text == "\x08":
                            # 退格
                            if buf:
                                buf = buf[:-1]
                        elif len(text) == 1 and ord(text) >= 32:
                            buf += text
                        else:
                            buf = ""

                        chan.send(text.encode())  # type: ignore[arg-type]
                    elif "bytes" in data:
                        chan.send(data["bytes"])
                elif data["type"] == "websocket.disconnect":
                    break
            # 注意：必须用 asyncio.TimeoutError 而非内置 TimeoutError。
            # Python 3.11+ 两者等价，但 Python 3.10 中 asyncio.wait_for 抛的是
            # asyncio.exceptions.TimeoutError（独立类，非 builtins.TimeoutError 子类），
            # 用内置 TimeoutError 会捕获不到导致 WebSocket 连接中断。
            except asyncio.TimeoutError:
                continue
            except WebSocketDisconnect:
                break

    try:
        await asyncio.gather(ssh_to_ws(), ws_to_ssh())
    finally:
        chan.close()
        ssh.close()


# ── SCP 文件上传 ──


@router.post("/api/upload/{server_id}")
async def upload_file(
    server_id: int,
    file: UploadFile = File(...),
    path: str = Form("/tmp/"),
    _user: dict = Depends(require_perm("cd.webshell")),
):
    """上传文件到目标服务器"""
    db = Database()
    with db.conn() as conn:
        srv = conn.execute("SELECT * FROM cd_servers WHERE id=?", (server_id,)).fetchone()
    if not srv:
        raise NotFoundError("服务器不存在", error_key="errors.server_not_found")

    # 安全校验：跨平台文件名防路径穿越
    # 浏览器可能传入 Windows 完整路径 (D:\tmp\2012.txt)，\ 在 Linux 上不被识别为分隔符
    # 统一将 \ 转为 /，再取 basename，确保 Windows/Linux 后端行为一致
    _fn = (file.filename or "").replace("\\", "/")
    _fn = re.sub(r"^[a-zA-Z]:", "", _fn)  # 去除 Windows 盘符
    safe_filename = _fn.rsplit("/", 1)[-1]
    if not safe_filename:
        raise ValidationError("无效文件名", error_key="errors.invalid_filename")

    # 安全校验：路径必须为绝对路径
    if not path.startswith("/"):
        raise ValidationError("路径必须为绝对路径，如 /tmp", error_key="errors.path_absolute")

    # 安全校验：路径遍历防护，解析 ../ 等符号
    # 用 posixpath 强制按 POSIX 规则归一化，无论 Python 跑在 Windows 还是 Linux 都一致
    # （os.path.realpath 在 Windows 上会自动加盘符 D:\，导致 SFTP 把目标写到错误位置）
    combined = (path.rstrip("/") + "/" + safe_filename).replace("\\", "/")
    combined = re.sub(r"^[a-zA-Z]:", "", combined)  # 去 Windows 盘符
    target = posixpath.normpath(combined)
    # 拦截写入系统敏感目录
    blocked_prefixes = ["/etc", "/boot", "/sys", "/proc", "/dev"]
    for prefix in blocked_prefixes:
        if target == prefix or target.startswith(prefix + "/"):
            raise ValidationError(
                f"不允许上传到系统目录: {prefix}",
                error_key="errors.upload_blocked_path",
                error_params={"path": prefix},
            )

    dt = DeployTarget(
        host=srv["host"],
        port=srv["port"],
        user=srv["user"],
        password=decrypt(srv["password"] or ""),
        ssh_key=decrypt(srv["ssh_key"] or ""),
    )
    try:
        ssh = await asyncio.to_thread(ssh_connect, dt, settings.ssh_timeout)
    except Exception as e:
        raise ValidationError(f"SSH 连接失败: {e}", error_key="errors.ssh_connect_failed") from e

    try:
        sftp = ssh.open_sftp()
        ssh.exec_command(f"mkdir -p {shlex.quote(os.path.dirname(target))}")
        with sftp.file(target, "w") as f:
            while True:
                chunk = await file.read(65536)
                if not chunk:
                    break
                f.write(chunk)
        sftp.close()
        ssh.close()
        return ok(data={"path": target}, message="文件上传成功")
    except Exception as e:
        ssh.close()
        raise ValidationError(f"上传失败: {e}", error_key="errors.upload_failed") from e
