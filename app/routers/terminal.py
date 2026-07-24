"""Web Shell + SCP 文件上传"""

import asyncio
import base64
import json
import os
import shlex
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, UploadFile, File, Form, HTTPException, Depends, Query
from app.database import Database
from app.auth import verify_token, get_db
from app.config import settings
from app.deployers.base import ssh_connect, DeployTarget
from app.crypto import decrypt

router = APIRouter()


async def _ws_verify(token: str | None = None) -> str:
    """WebSocket 鉴权：通过 query param token 校验"""
    if not token:
        raise HTTPException(401, "请登录")
    db = get_db()
    conn = None
    try:
        conn = db.conn()
        for r in conn.execute("SELECT username, password_hash FROM admin_users").fetchall():
            expected = base64.b64encode(
                f"{r['username']}:{r['password_hash']}".encode()
            ).decode()
            if token == expected:
                return r["username"]
    finally:
        if conn:
            conn.close()
    raise HTTPException(401, "token 无效")


@router.websocket("/ws/terminal/{server_id}")
async def terminal(websocket: WebSocket, server_id: int):
    # 从 query string 获取 token 并校验
    token = websocket.query_params.get("token")
    try:
        await _ws_verify(token)
    except HTTPException:
        await websocket.close(code=4001, reason="鉴权失败")
        return

    await websocket.accept()

    # 查服务器
    db = Database()
    conn = db.conn()
    srv = conn.execute("SELECT * FROM cd_servers WHERE id=?", (server_id,)).fetchone()
    if not srv:
        await websocket.send_text("\r\n❌ 服务器不存在\r\n")
        await websocket.close()
        return

    target = DeployTarget(
        host=srv["host"], port=srv["port"], user=srv["user"],
        password=decrypt(srv["password"] or ""), ssh_key=decrypt(srv["ssh_key"] or ""),
    )

    # SSH 连接
    try:
        ssh = ssh_connect(target, settings.ssh_timeout)
    except Exception as e:
        await websocket.send_text(f"\r\n❌ SSH 连接失败: {e}\r\n")
        await websocket.close()
        return

    # 开交互 shell
    chan = ssh.invoke_shell(term="xterm-256color", width=100, height=28)
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
        """WebSocket 输入 → SSH，支持终端尺寸自适应"""
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
                        chan.send(text)
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
    username: str = Depends(verify_token),
):
    """上传文件到目标服务器"""
    db = Database()
    conn = db.conn()
    srv = conn.execute("SELECT * FROM cd_servers WHERE id=?", (server_id,)).fetchone()
    if not srv:
        raise HTTPException(400, "服务器不存在")

    # 安全校验：文件名防路径穿越
    safe_filename = os.path.basename(file.filename)
    if not safe_filename:
        raise HTTPException(400, "无效文件名")

    # 安全校验：路径必须为绝对路径
    if not path.startswith("/"):
        raise HTTPException(400, "路径必须为绝对路径，如 /tmp")

    target = path.rstrip("/") + "/" + safe_filename

    dt = DeployTarget(
        host=srv["host"], port=srv["port"], user=srv["user"],
        password=decrypt(srv["password"] or ""), ssh_key=decrypt(srv["ssh_key"] or ""),
    )
    try:
        ssh = ssh_connect(dt, settings.ssh_timeout)
    except Exception as e:
        raise HTTPException(400, f"SSH 连接失败: {e}")

    try:
        sftp = ssh.open_sftp()
        ssh.exec_command(f"mkdir -p {shlex.quote(path)}")
        with sftp.file(target, "w") as f:
            while True:
                chunk = await file.read(65536)
                if not chunk:
                    break
                f.write(chunk)
        sftp.close()
        ssh.close()
        return {"success": True, "path": target}
    except Exception as e:
        ssh.close()
        raise HTTPException(500, f"上传失败: {e}")
