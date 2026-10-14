"""资源监控路由 - 统一 K8S / Docker / SSH 服务器资源查看

服务器列表分两步:GET /servers 只读库(不做 SSH,页面立即渲染),
GET /servers/{id}/probe 单台探测(前端并发调用,逐行回填状态).
"""

import logging
import shlex

import paramiko
from fastapi import APIRouter, Depends

from backend.auth import get_db, require_perm
from backend.config import settings
from backend.database import Database
from backend.deployers.base import _ssh_cmd, ssh_connect
from backend.exceptions import NotFoundError, ServiceUnavailableError, ValidationError
from backend.services.monitor_utils import (
    _cache_get,
    _cache_set,
    _make_target,
    _parse_kubectl_top,
    _ssh_test,
)

router = APIRouter(prefix="/api/monitor", tags=["monitor"])
logger = logging.getLogger(__name__)

# ── API ──


@router.get("/status")
def monitor_status():
    """监控功能是否启用"""
    return {"enabled": settings.monitoring_enabled}


def _guess_monitor_type(srv) -> str:
    """按 type 优先、tags 兜底的顺序推断监控类型(不连服务器)"""
    srv_type = (srv.get("type") or "").lower()
    if srv_type in ("k8s", "argocd", "fluxcd"):
        return "k8s"
    if srv_type == "docker":
        return "docker"
    if srv_type == "ssh":
        return "ssh"
    tags = [t.strip().lower() for t in (srv.get("tags") or "").split(",") if t.strip()]
    if any(t in ("k8s", "kubernetes") for t in tags):
        return "k8s"
    return "docker" if "docker" in tags else "ssh"


@router.get("/servers")
def list_monitor_servers(
    db: Database = Depends(get_db),
    _user: dict = Depends(require_perm("cd.resource-monitor")),
):
    """返回所有服务器的监控状态(按类型区分).

    只读库 + 合并已缓存的单台探测结果,不做 SSH 探测:探测过的服务器直接带状态,
    其余为 "unknown",由前端并行调 /servers/{id}/probe 逐台回填(页面不再等所有主机).
    """
    if not settings.monitoring_enabled:
        return {"enabled": False, "servers": []}

    with db.conn() as conn:
        servers = conn.execute("SELECT * FROM cd_servers ORDER BY type, name").fetchall()

    result = []
    for srv in servers:
        entry = dict(srv)
        entry["status"] = "unknown"
        entry["monitor_type"] = _guess_monitor_type(srv)
        entry["has_prometheus"] = False
        entry["has_metrics_server"] = False
        cached = _cache_get(f"servers:{srv['id']}")
        if isinstance(cached, dict):
            entry.update(cached)
        result.append(entry)

    return {"enabled": True, "servers": result}


@router.get("/servers/{server_id}/probe")
def probe_monitor_server(
    server_id: int,
    db: Database = Depends(get_db),
    _user: dict = Depends(require_perm("cd.resource-monitor")),
):
    """探测单台服务器的监控能力(SSH),结果缓存 monitor_cache_servers 秒.

    失败也返回 200 + status=error:单台不可达不该让整页报错,前端按行显示.
    """
    if not settings.monitoring_enabled:
        raise ValidationError("监控功能未启用", error_key="errors.monitoring_disabled")

    cache_key = f"servers:{server_id}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return {"success": True, "cached": True, "server": cached}

    with db.conn() as conn:
        srv = conn.execute("SELECT * FROM cd_servers WHERE id=?", (server_id,)).fetchone()
    if not srv:
        raise NotFoundError("服务器不存在", error_key="errors.server_not_found")

    entry = _probe_server(srv)
    _cache_set(cache_key, entry)
    return {"success": True, "cached": False, "server": entry}


def _probe_server(srv) -> dict:
    """SSH 探测单台服务器的监控能力,返回可合并进列表项的字段
    (status / monitor_type / has_prometheus / has_metrics_server / hint / error)"""
    entry = {
        "status": "error",
        "monitor_type": _guess_monitor_type(srv),
        "has_prometheus": False,
        "has_metrics_server": False,
    }
    ssh = None
    try:
        target = _make_target(srv)
        ssh = ssh_connect(target, settings.ssh_timeout)
        monitor_type = entry["monitor_type"]

        if monitor_type == "k8s":
            # 检测 Prometheus
            prom_out = _ssh_cmd(
                ssh,
                "kubectl get pods -A --no-headers 2>/dev/null | grep -i prometheus | grep -i running | head -1",
            )
            entry["has_prometheus"] = bool(prom_out)

            # 检测 metrics-server
            ms_out = _ssh_cmd(
                ssh,
                "kubectl top nodes --no-headers 2>/dev/null | head -1",
            )
            entry["has_metrics_server"] = bool(ms_out.strip())
            if not entry["has_metrics_server"] and not entry["has_prometheus"]:
                entry["status"] = "unavailable"
                entry["hint"] = "未安装 metrics-server 或 Prometheus"
            else:
                entry["status"] = "available"

        elif monitor_type == "docker":
            entry["status"] = "available"
            # 快速检测 docker 是否可用
            if not _ssh_cmd(ssh, "docker ps --format 'ok' 2>/dev/null | head -1"):
                entry["status"] = "unavailable"
                entry["hint"] = "Docker 不可用或权限不足"

        else:  # ssh / 其他
            entry["status"] = "available"
            # 检测是否有支持的监控命令
            if not _ssh_cmd(ssh, "which top 2>/dev/null"):
                entry["status"] = "unavailable"
                entry["hint"] = "SSH 连接正常但缺少基础命令"

    except (TimeoutError, ConnectionRefusedError, OSError) as e:
        logger.warning("Server %s:%s unreachable: %s", srv.get("host"), srv.get("port"), e)
        entry["status"] = "error"
        entry["error"] = "服务器不可达，请检查地址/端口/网络"
    except paramiko.AuthenticationException:
        logger.warning("Server %s auth failed", srv.get("host"))
        entry["status"] = "error"
        entry["error"] = "认证失败，请检查用户名/密码/密钥"
    except paramiko.SSHException as e:
        logger.warning("Server %s SSH error: %s", srv.get("host"), e)
        entry["status"] = "error"
        entry["error"] = "SSH 协议错误，请检查服务端配置"
    except Exception as e:
        logger.error("Server monitor failed", exc_info=e)
        entry["status"] = "error"
        entry["error"] = "服务器连接失败，请联系管理员"
    finally:
        if ssh:
            ssh.close()

    return entry


# ── K8S ──


@router.get("/nodes/{server_id}")
def get_nodes(
    server_id: int,
    db: Database = Depends(get_db),
    _user: dict = Depends(require_perm("cd.monitor.system")),
):
    """获取 K8S 集群 Node 资源占用"""
    if not settings.monitoring_enabled:
        raise ValidationError("监控功能未启用", error_key="errors.monitoring_disabled")

    cache_key = f"nodes:{server_id}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    with db.conn() as conn:
        srv = conn.execute("SELECT * FROM cd_servers WHERE id=?", (server_id,)).fetchone()
    if not srv:
        raise NotFoundError("服务器不存在", error_key="errors.server_not_found")

    ssh = None
    try:
        target = _make_target(srv)
        ssh = ssh_connect(target, settings.ssh_timeout)

        top_out = _ssh_cmd(ssh, "kubectl top nodes --no-headers 2>/dev/null")
        nodes = _parse_kubectl_top(top_out) if top_out else []

        capacity_out = _ssh_cmd(
            ssh,
            "kubectl get nodes -o custom-columns=NAME:.metadata.name,"
            "CPU:.status.capacity.cpu,MEMORY:.status.capacity.memory,"
            "PODS:.status.capacity.pods --no-headers 2>/dev/null",
        )
        capacities = {}
        if capacity_out:
            for line in capacity_out.strip().split("\n"):
                parts = line.split()
                if len(parts) >= 3:
                    capacities[parts[0]] = {
                        "cpu": parts[1],
                        "memory": parts[2],
                        "max_pods": parts[3] if len(parts) > 3 else "?",
                    }

        for node in nodes:
            cap = capacities.get(node["name"], {})
            node["capacity_cpu"] = cap.get("cpu", "?")
            node["capacity_memory"] = cap.get("memory", "?")
            node["max_pods"] = cap.get("max_pods", "?")

        resp = {
            "success": True,
            "monitor_type": "k8s",
            "has_metrics": bool(nodes),
            "nodes": nodes,
            "hint": ""
            if nodes
            else "集群未安装 metrics-server，请运行: kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml",
        }
        _cache_set(cache_key, resp)
        return resp
    except Exception as e:
        raise ServiceUnavailableError(f"SSH 连接失败: {e}", error_key="errors.ssh_connect_failed") from e
    finally:
        if ssh:
            ssh.close()


@router.get("/pods/{server_id}")
def get_pods(
    server_id: int,
    namespace: str = "",
    db: Database = Depends(get_db),
    _user: dict = Depends(require_perm("cd.monitor.app")),
):
    """获取 K8S 集群 Pod 资源占用"""
    if not settings.monitoring_enabled:
        raise ValidationError("监控功能未启用", error_key="errors.monitoring_disabled")

    cache_key = f"pods:{server_id}:{namespace}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    with db.conn() as conn:
        srv = conn.execute("SELECT * FROM cd_servers WHERE id=?", (server_id,)).fetchone()
    if not srv:
        raise NotFoundError("服务器不存在", error_key="errors.server_not_found")

    ssh = None
    try:
        target = _make_target(srv)
        ssh = ssh_connect(target, settings.ssh_timeout)

        ns_list = _ssh_cmd(
            ssh,
            "kubectl get ns -o custom-columns=NAME:.metadata.name --no-headers 2>/dev/null",
        )
        namespaces = [ns.strip() for ns in ns_list.split("\n") if ns.strip()] if ns_list else []

        ns_filter = f"-n {shlex.quote(namespace)}" if namespace else "--all-namespaces"
        top_cmd = f"kubectl top pods {ns_filter} --no-headers 2>/dev/null"
        top_out = _ssh_cmd(ssh, top_cmd)
        pods = []
        if top_out:
            for line in top_out.strip().split("\n"):
                parts = line.split()
                if namespace and len(parts) >= 3:
                    pods.append({"namespace": namespace, "name": parts[0], "cpu": parts[1], "memory": parts[2]})
                elif not namespace and len(parts) >= 4:
                    pods.append({"namespace": parts[0], "name": parts[1], "cpu": parts[2], "memory": parts[3]})

        status_cmd = f"kubectl get pods {ns_filter} -o custom-columns=NAMESPACE:.metadata.namespace,NAME:.metadata.name,STATUS:.status.phase,RESTARTS:.status.containerStatuses[*].restartCount,NODE:.spec.nodeName --no-headers 2>/dev/null"
        status_out = _ssh_cmd(ssh, status_cmd)
        status_map = {}
        if status_out:
            for line in status_out.strip().split("\n"):
                parts = line.split()
                if len(parts) >= 4:
                    if namespace:
                        key = f"{namespace}/{parts[0]}"
                        status_map[key] = {
                            "status": parts[1],
                            "restarts": parts[2].replace("[", "").replace("]", ""),
                            "node": parts[3],
                        }
                    else:
                        key = f"{parts[0]}/{parts[1]}"
                        status_map[key] = {
                            "status": parts[2],
                            "restarts": parts[3].replace("[", "").replace("]", ""),
                            "node": parts[4],
                        }

        for pod in pods:
            key = f"{pod['namespace']}/{pod['name']}"
            info = status_map.get(key, {})
            pod["status"] = info.get("status", "?")
            pod["restarts"] = info.get("restarts", "0")
            pod["node"] = info.get("node", "?")

        resp = {
            "success": True,
            "monitor_type": "k8s",
            "has_metrics": bool(pods),
            "namespaces": namespaces,
            "pods": pods,
            "hint": "" if pods else "集群未安装 metrics-server",
        }
        _cache_set(cache_key, resp)
        return resp
    except Exception as e:
        raise ServiceUnavailableError(f"SSH 连接失败: {e}", error_key="errors.ssh_connect_failed") from e
    finally:
        if ssh:
            ssh.close()


@router.get("/pod-detail/{server_id}")
def get_pod_detail(
    server_id: int,
    namespace: str,
    name: str,
    db: Database = Depends(get_db),
    _user: dict = Depends(require_perm("cd.monitor.app")),
):
    """获取单个 K8S Pod 详情"""
    if not settings.monitoring_enabled:
        raise ValidationError("监控功能未启用", error_key="errors.monitoring_disabled")

    cache_key = f"pod_detail:{server_id}:{namespace}:{name}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    with db.conn() as conn:
        srv = conn.execute("SELECT * FROM cd_servers WHERE id=?", (server_id,)).fetchone()
    if not srv:
        raise NotFoundError("服务器不存在", error_key="errors.server_not_found")

    ssh = None
    try:
        target = _make_target(srv)
        ssh = ssh_connect(target, settings.ssh_timeout)
        describe = _ssh_cmd(
            ssh, f"kubectl describe pod {shlex.quote(name)} -n {shlex.quote(namespace)} 2>/dev/null | tail -30"
        )
        logs = _ssh_cmd(ssh, f"kubectl logs {shlex.quote(name)} -n {shlex.quote(namespace)} --tail=20 2>/dev/null")
        top = _ssh_cmd(ssh, f"kubectl top pod {shlex.quote(name)} -n {shlex.quote(namespace)} --no-headers 2>/dev/null")
        resp = {"success": True, "name": name, "namespace": namespace, "top": top, "describe": describe, "logs": logs}
        _cache_set(cache_key, resp)
        return resp
    except Exception as e:
        raise ServiceUnavailableError(f"SSH 连接失败: {e}", error_key="errors.ssh_connect_failed") from e
    finally:
        if ssh:
            ssh.close()


# ── Docker ──


@router.get("/docker/{server_id}")
def get_docker_containers(
    server_id: int,
    db: Database = Depends(get_db),
    _user: dict = Depends(require_perm("cd.monitor.app")),
):
    """获取 Docker 服务器容器资源占用(仅容器,系统资源走 /system)"""
    if not settings.monitoring_enabled:
        raise ValidationError("监控功能未启用", error_key="errors.monitoring_disabled")

    cache_key = f"docker:{server_id}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    with db.conn() as conn:
        srv = conn.execute("SELECT * FROM cd_servers WHERE id=?", (server_id,)).fetchone()
    if not srv:
        raise NotFoundError("服务器不存在", error_key="errors.server_not_found")

    ssh = None
    try:
        target = _make_target(srv)
        ssh = ssh_connect(target, settings.ssh_timeout)

        stats_out = _ssh_cmd(
            ssh,
            "docker stats --no-stream --format '{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}|{{.NetIO}}|{{.BlockIO}}' 2>/dev/null",
        )
        containers = []
        if stats_out:
            for line in stats_out.strip().split("\n"):
                parts = line.split("|")
                if len(parts) >= 4:
                    containers.append(
                        {
                            "name": parts[0],
                            "cpu": parts[1],
                            "memory": parts[2],
                            "memory_percent": parts[3],
                            "net_io": parts[4] if len(parts) > 4 else "?",
                            "block_io": parts[5] if len(parts) > 5 else "?",
                        }
                    )

        resp = {
            "success": True,
            "monitor_type": "docker",
            "containers": containers,
        }
        _cache_set(cache_key, resp)
        return resp
    except Exception as e:
        raise ServiceUnavailableError(f"SSH 连接失败: {e}", error_key="errors.ssh_connect_failed") from e
    finally:
        if ssh:
            ssh.close()


# ── 系统资源(通用:K8S / Docker / SSH 都可用)──


@router.get("/system/{server_id}")
def get_system_info(
    server_id: int,
    db: Database = Depends(get_db),
    _user: dict = Depends(require_perm("cd.monitor.system")),
):
    """获取服务器系统资源:CPU,内存,磁盘,负载,进程 Top5(所有类型通用)"""
    if not settings.monitoring_enabled:
        raise ValidationError("监控功能未启用", error_key="errors.monitoring_disabled")

    cache_key = f"system:{server_id}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    with db.conn() as conn:
        srv = conn.execute("SELECT * FROM cd_servers WHERE id=?", (server_id,)).fetchone()
    if not srv:
        raise NotFoundError("服务器不存在", error_key="errors.server_not_found")

    ssh = None
    try:
        target = _make_target(srv)
        ssh = ssh_connect(target, settings.ssh_timeout)
        info = _ssh_test(ssh)

        top_out = _ssh_cmd(
            ssh,
            'ps aux --sort=-%cpu --no-headers 2>/dev/null | head -5 | awk \'{print $2"|"$3"|"$4"|"$11}\'',
        )
        processes = []
        if top_out:
            for line in top_out.strip().split("\n"):
                parts = line.split("|")
                if len(parts) >= 4:
                    processes.append(
                        {
                            "pid": parts[0],
                            "cpu": parts[1],
                            "mem": parts[2],
                            "cmd": parts[3],
                        }
                    )

        resp = {
            "success": True,
            "system": info,
            "top_processes": processes,
        }
        _cache_set(cache_key, resp)
        return resp
    except Exception as e:
        raise ServiceUnavailableError(f"SSH 连接失败: {e}", error_key="errors.ssh_connect_failed") from e
    finally:
        if ssh:
            ssh.close()
