"""Web Shell + SCP 文件上传"""

import asyncio
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, UploadFile, File, Form, HTTPException, Depends
from app.database import Database
from app.auth import verify_token
from app.config import settings

router = APIRouter()


@router.websocket("/ws/terminal/{server_id}")
async def terminal(websocket: WebSocket, server_id: int):
    await websocket.accept()

    # 查服务器
    db = Database()
    conn = db.conn()
    srv = conn.execute("SELECT * FROM cd_servers WHERE id=?", (server_id,)).fetchone()
    if not srv:
        await websocket.send_text("\r\n❌ 服务器不存在\r\n")
        await websocket.close()
        return

    host, port = srv["host"], srv["port"]
    user, password = srv["user"], srv["password"] or ""

    # SSH 连接
    import paramiko
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        kwargs = dict(hostname=host, port=port, username=user, timeout=settings.ssh_timeout)
        if password:
            kwargs["password"] = password
        ssh.connect(**kwargs)
    except Exception as e:
        await websocket.send_text(f"\r\n❌ SSH 连接失败: {e}\r\n")
        await websocket.close()
        return

    # 开交互 shell
    chan = ssh.invoke_shell(term="xterm-256color", width=100, height=28)
    chan.settimeout(0.0)

    async def ssh_to_ws():
        """SSH 输出 → WebSocket"""
        buf = b""
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
        """WebSocket 输入 → SSH"""
        while not chan.closed:
            try:
                data = await asyncio.wait_for(websocket.receive(), timeout=0.05)
                if data["type"] == "websocket.receive":
                    if "text" in data:
                        chan.send(data["text"])
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

    import paramiko
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs = dict(hostname=srv["host"], port=srv["port"], username=srv["user"], timeout=settings.ssh_timeout)
    if srv["password"]:
        kwargs["password"] = srv["password"]
    try:
        ssh.connect(**kwargs)
    except Exception as e:
        raise HTTPException(400, f"SSH 连接失败: {e}")

    target = path.rstrip("/") + "/" + file.filename
    try:
        sftp = ssh.open_sftp()
        ssh.exec_command(f"mkdir -p {path}")
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
