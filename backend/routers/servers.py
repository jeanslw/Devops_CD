"""服务器管理路由"""

import concurrent.futures
import logging
import socket

import paramiko
import pymysql
from fastapi import APIRouter, Depends

from backend.auth import get_db, require_perm, verify_token
from backend.crypto import decrypt, encrypt
from backend.database import Database
from backend.deployers.base import trust_ssh_host
from backend.exceptions import ConflictError, NotFoundError
from backend.models import ServerRequest
from backend.responses import ok
from backend.services.monitor_utils import _cache_get, _cache_set, clear_server_cache

router = APIRouter(prefix="/api/servers", tags=["servers"])
logger = logging.getLogger(__name__)


MASK = "***"


@router.get("")
def list_servers(
    db: Database = Depends(get_db),
    username: str = Depends(verify_token),
):
    with db.conn() as conn:
        rows = conn.execute("SELECT * FROM cd_servers ORDER BY name").fetchall()
        result = []
        for r in rows:
            row = dict(r)
            # 不暴露明文 key/password，只标记是否存在
            row["has_password"] = bool(row.get("password"))
            row["has_ssh_key"] = bool(row.get("ssh_key"))
            row["password"] = MASK if row.get("password") else ""
            row["ssh_key"] = MASK if row.get("ssh_key") else ""
            result.append(row)
        return result


@router.post("")
def add_server(
    req: ServerRequest,
    db: Database = Depends(get_db),
    _user: dict = Depends(require_perm("cd.server-manage")),
):
    with db.conn() as conn:
        try:
            conn.execute(
                "INSERT INTO cd_servers (name,host,port,user,auth_type,password,ssh_key,type,tags) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    req.name,
                    req.host,
                    req.port,
                    req.user,
                    req.auth_type,
                    encrypt(req.password),
                    encrypt(req.ssh_key),
                    req.type,
                    req.tags,
                ),
            )
            clear_server_cache()
            return ok(message=f"服务器 '{req.name}' 已添加")
        except pymysql.err.IntegrityError as e:
            raise ConflictError(
                f"服务器 '{req.name}' 已存在", error_key="errors.server_exists", error_params={"name": req.name}
            ) from e


@router.put("/{sid}")
def update_server(
    sid: int,
    req: ServerRequest,
    db: Database = Depends(get_db),
    _user: dict = Depends(require_perm("cd.server-manage")),
):
    with db.conn() as conn:
        existing = conn.execute("SELECT * FROM cd_servers WHERE id=?", (sid,)).fetchone()
        if not existing:
            raise NotFoundError("服务器不存在", error_key="errors.server_not_found")

        # 前端发来 MASK 表示未修改，保留数据库现有值
        password = existing["password"]
        if req.password and req.password != MASK:
            password = encrypt(req.password)

        ssh_key = existing["ssh_key"]
        if req.ssh_key and req.ssh_key != MASK:
            ssh_key = encrypt(req.ssh_key)
        elif req.ssh_key == "":
            ssh_key = ""  # 显式清空

        # 切换认证方式时清除旧凭据
        if req.auth_type == "password":
            ssh_key = ""
        elif req.auth_type == "key":
            password = ""

        try:
            conn.execute(
                "UPDATE cd_servers SET name=?, host=?, port=?, user=?, auth_type=?, password=?, ssh_key=?, type=?, tags=? WHERE id=?",
                (req.name, req.host, req.port, req.user, req.auth_type, password, ssh_key, req.type, req.tags, sid),
            )
            clear_server_cache()
            return ok(message=f"服务器 '{req.name}' 已更新")
        except pymysql.err.IntegrityError as e:
            raise ConflictError(
                f"服务器 '{req.name}' 已存在", error_key="errors.server_exists", error_params={"name": req.name}
            ) from e


@router.delete("/{sid}")
def delete_server(
    sid: int,
    db: Database = Depends(get_db),
    _user: dict = Depends(require_perm("cd.server-manage")),
):
    with db.conn() as conn:
        conn.execute("DELETE FROM cd_servers WHERE id=?", (sid,))
        clear_server_cache()
        return ok(message="服务器已删除")


STATUS_CACHE_KEY = "servers:status"


def _check_single_server(server):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        result = s.connect_ex((server["host"], server["port"]))
        s.close()
        return server["id"], result == 0
    except Exception:
        return server["id"], False


@router.get("/status")
def check_servers_status(
    db: Database = Depends(get_db),
    username: str = Depends(verify_token),
):
    cached = _cache_get(STATUS_CACHE_KEY)
    if cached is not None:
        return cached

    with db.conn() as conn:
        rows = conn.execute("SELECT id, host, port FROM cd_servers").fetchall()
    servers_list = [{"id": r["id"], "host": r["host"], "port": r["port"]} for r in rows]

    status_map = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(_check_single_server, s): s for s in servers_list}
        for future in concurrent.futures.as_completed(futures):
            sid, online = future.result()
            status_map[sid] = online

    result = {}
    for s in servers_list:
        result[str(s["id"])] = status_map.get(s["id"], False)

    _cache_set(STATUS_CACHE_KEY, result)
    return result


@router.post("/test-connection")
def test_server_connection(
    req: ServerRequest,
    trust: bool = False,
    sid: int = 0,
    db: Database = Depends(get_db),
    _user: dict = Depends(require_perm("cd.server-manage")),
):
    """测试服务器 SSH 连接。
    trust=True 时会自动信任主机密钥并保存到 known_hosts。
    sid>0 时为编辑已有服务器，凭据字段为 MASK/空 视为未修改，从数据库读取真实值。
    """
    password = req.password
    ssh_key = req.ssh_key

    # 编辑模式：与 PUT 更新接口保持一致的凭据处理逻辑
    if sid:
        with db.conn() as conn:
            row = conn.execute("SELECT * FROM cd_servers WHERE id=?", (sid,)).fetchone()
            if row:
                # password：有值且非MASK才用前端新值，否则从DB取解密后原值
                if req.password and req.password != MASK:
                    password = req.password
                else:
                    password = decrypt(row["password"]) if row.get("password") else ""
                # ssh_key：有值且非MASK才用前端新值，否则从DB取解密后原值
                if req.ssh_key and req.ssh_key != MASK:
                    ssh_key = req.ssh_key
                else:
                    ssh_key = decrypt(row["ssh_key"]) if row.get("ssh_key") else ""

    # 切换认证方式时清除旧凭据（与 PUT 保持一致）
    if req.auth_type == "password":
        ssh_key = ""
    elif req.auth_type == "key":
        password = ""

    if trust:
        result = trust_ssh_host(
            host=req.host,
            port=req.port,
            username=req.user,
            password=password,
            ssh_key=ssh_key,
        )
        if result["success"]:
            return ok(message=result["message"], data={"key_fingerprint": result["key_fingerprint"]})
        else:
            return {"success": False, "message": result["message"]}
    else:
        # 普通连接测试（使用 RejectPolicy）
        from backend.deployers.base import DeployTarget, ssh_connect

        try:
            target = DeployTarget(
                host=req.host,
                port=req.port,
                user=req.user,
                password=password,
                ssh_key=ssh_key,
            )
            ssh = ssh_connect(target, timeout=10)
            ssh.close()
            return ok(message="连接成功")
        except (TimeoutError, ConnectionRefusedError, OSError) as e:
            logger.warning("SSH test unreachable: %s:%s — %s", req.host, req.port, e)
            return {"success": False, "message": "服务器不可达，请检查地址/端口/网络"}
        except paramiko.AuthenticationException:
            logger.warning("SSH test auth failed: %s@%s:%s", req.user, req.host, req.port)
            return {"success": False, "message": "认证失败，请检查用户名/密码/密钥"}
        except paramiko.SSHException as e:
            logger.warning("SSH test protocol error: %s:%s — %s", req.host, req.port, e)
            return {"success": False, "message": "SSH 协议错误，请检查服务端配置"}
        except Exception as e:
            logger.error("SSH connection test failed", exc_info=e)
            return {"success": False, "message": "连接失败，请检查主机配置"}


@router.post("/{sid}/trust")
def trust_existing_server(
    sid: int,
    db: Database = Depends(get_db),
    _user: dict = Depends(require_perm("cd.server-manage")),
):
    """信任已存在的服务器（获取并保存主机密钥）"""
    with db.conn() as conn:
        row = conn.execute("SELECT * FROM cd_servers WHERE id=?", (sid,)).fetchone()
        if not row:
            raise NotFoundError("服务器不存在")

        password = decrypt(row["password"]) if row.get("password") else ""
        ssh_key = decrypt(row["ssh_key"]) if row.get("ssh_key") else ""

        # 与 PUT 更新接口保持一致：按 auth_type 过滤旧凭据
        auth_type = (row.get("auth_type") or "password").lower()
        if auth_type == "password":
            ssh_key = ""
        elif auth_type == "key":
            password = ""

        logger.info(
            f"trust_existing_server: sid={sid}, auth_type={auth_type}, "
            f"password_len={len(password or '')}, ssh_key_len={len(ssh_key or '')}"
        )

        result = trust_ssh_host(
            host=row["host"],
            port=row["port"],
            username=row["user"],
            password=password,
            ssh_key=ssh_key,
        )
        if result["success"]:
            return ok(message=result["message"], data={"key_fingerprint": result["key_fingerprint"]})
        else:
            return {"success": False, "message": result["message"]}
