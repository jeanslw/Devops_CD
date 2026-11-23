"""Web Shell + SCP 文件上传"""

import asyncio
import contextlib
import json
import logging
import os
import posixpath
import re
import shlex
import time

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect

from backend.auth import (
    WILDCARD_PERM,
    _delete_session,
    _lookup_session,
    _oidc_identity,
    cd_deploy_gate,
    get_db,
    require_perm,
)
from backend.config import settings
from backend.crypto import decrypt_strict, DecryptError
from backend.database import Database
from backend.deployers.base import DeployTarget, ssh_connect
from backend.exceptions import AppException, NotFoundError, ValidationError
from backend.responses import ok
from backend.services import glue_identity

router = APIRouter()

# ── 危险命令黑名单(正则匹配,检测完整命令行)──
_DANGEROUS_PATTERNS = [
    r"\brm\s+.*-rf\s+/",
    r"\brm\s+.*-rf\s+~",
    r"\bdd\s+if=",
    r"\bmkfs\.",
    r":\(\)\s*\{.*:\|:&\s*\};:",
    r">\s*/dev/sd",
    r"\bchmod\s+.*777\s+/",
    r">\s*/etc/passwd",
    r"\brm\s+.*--no-preserve-root\s+/",
    r"\bchown\s+.*-R\s+\w+\s+/",
    r"\bchmod\s+.*-R\s+\d+\s+/etc",
]

_DANGEROUS_WARNING = "\r\n⚠️  危险命令已被拦截，未发送到服务器\r\n"


def _check_dangerous(cmd: str) -> bool:
    """检测命令行是否匹配危险模式"""
    stripped = cmd.strip()
    if not stripped:
        return False
    return any(re.search(pattern, stripped) for pattern in _DANGEROUS_PATTERNS)


def _webshell_permitted(role: str, perms: list[str]) -> bool:
    """web 终端准入判据:super_admin 角色或 '*' 通配放行,其余只认 cd.webshell."""
    if role == settings.super_admin_role:
        return True
    perms = perms or []
    return WILDCARD_PERM in perms or "cd.webshell" in perms


async def _ws_verify(token: str | None = None) -> str:
    """WebSocket 鉴权:仅通过 HttpOnly cookie cd_token 查服务端会话(O(1) 哈希查找).

    不再接受 ?token= 查询串,避免令牌进入访问日志与反向代理日志.
    """
    if not token:
        raise HTTPException(401, "请登录")
    db = get_db()
    try:
        cd_deploy_gate(db)
    except AppException as exc:
        raise HTTPException(503, exc.message) from exc
    with db.conn() as conn:
        sess = _lookup_session(conn, token)
        if sess is None:
            raise HTTPException(401, "token 无效")
        if time.time() > sess["expires_at"]:
            _delete_session(conn, token)
            raise HTTPException(401, "token 已过期，请重新登录")
    try:
        ident = _oidc_identity(db, token, sess["username"])
    except AppException as exc:
        raise HTTPException(exc.status_code, exc.message) from exc
    return ident["username"]


@router.websocket("/ws/terminal/{server_id}")
async def terminal(websocket: WebSocket, server_id: int):
    token = websocket.cookies.get("cd_token")
    try:
        username = await _ws_verify(token)
    except HTTPException as e:
        await websocket.close(code=4001, reason="CD 未部署" if e.status_code == 503 else "鉴权失败")
        return

    db = get_db()
    try:
        ident = glue_identity.get_identity(username)
    except AppException:
        await websocket.close(code=4003, reason="权限校验不可用")
        return
    if not _webshell_permitted(ident.get("role") or "", ident.get("permissions") or []):
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

    try:
        password = decrypt_strict(srv["password"] or "")
        ssh_key = decrypt_strict(srv["ssh_key"] or "")
    except DecryptError as e:
        await websocket.send_text(f"\r\n❌ 服务器凭据解密失败：{e}\r\n")
        await websocket.close()
        return
    target = DeployTarget(
        host=srv["host"],
        port=srv["port"],
        user=srv["user"],
        password=password,
        ssh_key=ssh_key,
    )

    try:
        ssh = await asyncio.to_thread(ssh_connect, target, settings.ssh_timeout)
    except Exception as e:
        await websocket.send_text(f"\r\n❌ SSH 连接失败: {e}\r\n")
        await websocket.close()
        return

    try:
        chan = await asyncio.to_thread(ssh.invoke_shell, term="xterm-256color", width=100, height=28)
    except Exception as e:
        await websocket.send_text(f"\r\n❌ Shell 创建失败: {e}\r\n")
        ssh.close()
        await websocket.close()
        return
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
        """WebSocket 输入 → SSH,支持终端尺寸自适应 + 危险命令拦截"""
        buf = ""
        while not chan.closed:
            try:
                data = await asyncio.wait_for(websocket.receive(), timeout=0.05)
                if data["type"] == "websocket.receive":
                    if "text" in data:
                        text = data["text"]
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
                                logging.getLogger(__name__).warning(
                                    "WebShell 危险命令被拦截 server_id=%s user=%s cmd=%.200s",
                                    server_id,
                                    username,
                                    line,
                                )
                                await websocket.send_text(_DANGEROUS_WARNING)
                                continue
                            buf = ""
                        elif "\x03" in text or "\x04" in text:
                            buf = ""
                        elif text.startswith("\x1b"):
                            pass
                        elif text == "\x7f" or text == "\x08":
                            if buf:
                                buf = buf[:-1]
                        elif len(text) == 1 and ord(text) >= 32:
                            buf += text
                        else:
                            buf = ""

                        chan.send(text.encode())
                    elif "bytes" in data:
                        chan.send(data["bytes"])
                elif data["type"] == "websocket.disconnect":
                    break
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

    _fn = (file.filename or "").replace("\\", "/")
    _fn = re.sub(r"^[a-zA-Z]:", "", _fn)
    safe_filename = _fn.rsplit("/", 1)[-1]
    if not safe_filename:
        raise ValidationError("无效文件名", error_key="errors.invalid_filename")

    if not path.startswith("/"):
        raise ValidationError("路径必须为绝对路径，如 /tmp", error_key="errors.path_absolute")

    combined = (path.rstrip("/") + "/" + safe_filename).replace("\\", "/")
    combined = re.sub(r"^[a-zA-Z]:", "", combined)
    target = posixpath.normpath(combined)
    blocked_prefixes = ["/etc", "/boot", "/sys", "/proc", "/dev"]
    for prefix in blocked_prefixes:
        if target == prefix or target.startswith(prefix + "/"):
            raise ValidationError(
                f"不允许上传到系统目录: {prefix}",
                error_key="errors.upload_blocked_path",
                error_params={"path": prefix},
            )

    try:
        password = decrypt_strict(srv["password"] or "")
        ssh_key = decrypt_strict(srv["ssh_key"] or "")
    except DecryptError as e:
        raise AppException(
            f"服务器「{srv['name']}」凭据解密失败，请检查口令/私钥是否完好，及 SECRET_KEY 是否与加密时一致：{e}",
            status_code=500,
        ) from e
    dt = DeployTarget(
        host=srv["host"],
        port=srv["port"],
        user=srv["user"],
        password=password,
        ssh_key=ssh_key,
    )
    try:
        ssh = await asyncio.to_thread(ssh_connect, dt, settings.ssh_timeout)
    except Exception as e:
        raise ValidationError(f"SSH 连接失败: {e}", error_key="errors.ssh_connect_failed") from e

    sftp = None
    try:
        ssh.exec_command(f"mkdir -p {shlex.quote(os.path.dirname(target))}")
        sftp = ssh.open_sftp()
        with sftp.file(target, "w") as f:
            while True:
                chunk = await file.read(65536)
                if not chunk:
                    break
                f.write(chunk)
        return ok(data={"path": target}, message="文件上传成功")
    except Exception as e:
        raise ValidationError(f"上传失败: {e}", error_key="errors.upload_failed") from e
    finally:
        if sftp is not None:
            with contextlib.suppress(Exception):
                sftp.close()
        with contextlib.suppress(Exception):
            ssh.close()
