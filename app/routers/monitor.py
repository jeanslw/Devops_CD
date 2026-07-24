"""资源监控路由 — 统一 K8S / Docker / SSH 服务器资源查看"""

import time
from fastapi import APIRouter, HTTPException, Depends
from app.database import Database
from app.auth import get_db, verify_token
from app.deployers.base import ssh_connect, DeployTarget
from app.crypto import decrypt
from app.config import settings

router = APIRouter(prefix="/api/monitor", tags=["monitor"])

# ── 简易内存缓存 ──
_cache: dict[str, tuple[float, object]] = {}

# TTL 从配置读取，可按类型在 .env 中覆盖
_CACHE_TTL: dict[str, int] = {}


def _get_cache_ttl() -> dict[str, int]:
    """懒加载缓存 TTL，确保 settings 已初始化"""
    if not _CACHE_TTL:
        _CACHE_TTL.update({
            "servers": settings.monitor_cache_servers,
            "system": settings.monitor_cache_system,
            "nodes": settings.monitor_cache_nodes,
            "pods": settings.monitor_cache_pods,
            "docker": settings.monitor_cache_docker,
            "pod_detail": settings.monitor_cache_pod_detail,
        })
    return _CACHE_TTL


def _cache_get(key: str) -> object | None:
    entry = _cache.get(key)
    if entry is None:
        return None
    ts, data = entry
    ttl = _get_cache_ttl().get(key.split(":")[0], 30)
    if time.time() - ts > ttl:
        del _cache[key]
        return None
    return data


def _cache_set(key: str, data: object):
    _cache[key] = (time.time(), data)


def clear_server_cache():
    """服务器变更时清除相关监控缓存"""
    for key in list(_cache.keys()):
        if key.startswith("servers:") or key.startswith("system:") or key.startswith("docker:") or key.startswith("nodes:") or key.startswith("pods:"):
            del _cache[key]


# ── 工具函数 ──

def _ssh_cmd(ssh, cmd):
    _, stdout, stderr = ssh.exec_command(cmd)
    o = stdout.read().decode(errors="replace").strip()
    e = stderr.read().decode(errors="replace").strip()
    return o or e


def _parse_kubectl_top(text: str, has_header: bool = False) -> list[dict]:
    """解析 kubectl top nodes/pods 输出"""
    if not text:
        return []
    lines = text.strip().split("\n")
    if has_header:
        lines = lines[1:]
    items = []
    for line in lines:
        parts = line.split()
        if len(parts) >= 5:  # nodes: NAME CPU CORES CPU% MEMORY MEMORY%
            items.append({
                "name": parts[0],
                "cpu": parts[1],
                "cpu_percent": parts[2],
                "memory": parts[3],
                "memory_percent": parts[4],
            })
        elif len(parts) >= 3:
            items.append({
                "name": parts[0],
                "cpu": parts[1],
                "memory": parts[2],
            })
    return items


def _ssh_test(ssh) -> dict:
    """SSH 连接后执行一段复合脚本，获取系统资源摘要"""
    script = r"""
echo "---CPU_CORES---"
nproc
echo "---UPTIME---"
uptime -s 2>/dev/null && echo "" && cat /proc/uptime 2>/dev/null | awk '{print int($1)}'
echo "---LOAD---"
cat /proc/loadavg 2>/dev/null
echo "---MEM---"
free -m 2>/dev/null | tail -2 | head -1 | awk '{print $3"/"$2" "$3*100/$2}'
echo "---DISK---"
df -h / 2>/dev/null | tail -1 | awk '{print $5" "$3"/"$2}'
echo "---OS---"
cat /etc/os-release 2>/dev/null | grep PRETTY_NAME | cut -d= -f2 | tr -d '"'
echo "---DOCKER---"
docker info --format '{{.ContainersRunning}}/{{.Containers}}' 2>/dev/null || echo "N/A"
"""
    out = _ssh_cmd(ssh, script)
    result = {
        "cpu_cores": "?",
        "uptime_seconds": 0,
        "uptime_since": "",
        "load": "",
        "memory_used": "",
        "memory_total": "",
        "memory_percent": "",
        "disk_used": "",
        "disk_total": "",
        "disk_percent": "",
        "os": "",
        "docker_containers": "N/A",
    }
    if not out:
        return result

    current = None
    for line in out.split("\n"):
        line = line.strip()
        if line == "---CPU_CORES---":
            current = "cpu_cores"
            continue
        elif line == "---UPTIME---":
            current = "uptime"
            continue
        elif line == "---LOAD---":
            current = "load"
            continue
        elif line == "---MEM---":
            current = "mem"
            continue
        elif line == "---DISK---":
            current = "disk"
            continue
        elif line == "---OS---":
            current = "os"
            continue
        elif line == "---DOCKER---":
            current = "docker"
            continue

        if not current or not line:
            continue

        if current == "cpu_cores":
            result["cpu_cores"] = line
            current = None
        elif current == "uptime":
            if ":" in line and "-" not in line:  # uptime -s output: YYYY-MM-DD HH:MM:SS
                result["uptime_since"] = line
            elif line.isdigit():
                result["uptime_seconds"] = int(line)
            current = None
        elif current == "load":
            result["load"] = line
            current = None
        elif current == "mem":
            parts = line.split()
            if len(parts) >= 2:
                result["memory_used"] = parts[0].split("/")[0] if "/" in parts[0] else parts[0]
                result["memory_total"] = parts[0].split("/")[1] if "/" in parts[0] else "?"
                result["memory_percent"] = parts[1] if len(parts) > 1 else "?"
            current = None
        elif current == "disk":
            parts = line.split()
            if len(parts) >= 2:
                result["disk_percent"] = parts[0]
                used_total = parts[1] if len(parts) > 1 else "?/?"
                result["disk_used"] = used_total.split("/")[0] if "/" in used_total else used_total
                result["disk_total"] = used_total.split("/")[1] if "/" in used_total else "?"
            current = None
        elif current == "os":
            result["os"] = line
            current = None
        elif current == "docker":
            result["docker_containers"] = line
            current = None

    return result


def _make_target(srv) -> DeployTarget:
    """从数据库行构造 DeployTarget，自动解密 password / ssh_key"""
    return DeployTarget(
        host=srv["host"], port=srv["port"],
        user=srv["user"],
        password=decrypt(srv["password"] or ""),
        ssh_key=decrypt(srv["ssh_key"] or ""),
    )


# ── API ──

@router.get("/status")
def monitor_status():
    """监控功能是否启用"""
    return {"enabled": settings.monitoring_enabled}


@router.get("/servers")
def list_monitor_servers(
    db: Database = Depends(get_db),
    username: str = Depends(verify_token),
):
    """返回所有服务器的监控状态（按类型区分）"""
    if not settings.monitoring_enabled:
        return {"enabled": False, "servers": []}

    cached = _cache_get("servers:all")
    if cached is not None:
        return cached

    conn = db.conn()
    try:
        servers = conn.execute(
            "SELECT * FROM cd_servers ORDER BY type, name"
        ).fetchall()
    finally:
        conn.close()

    result = []
    for srv in servers:
        entry = dict(srv)
        entry["status"] = "unchecked"
        entry["monitor_type"] = "unknown"
        # K8S 专用
        entry["has_prometheus"] = False
        entry["has_metrics_server"] = False

        try:
            target = _make_target(srv)
            ssh = ssh_connect(target, settings.ssh_timeout)

            srv_type = (srv.get("type") or "").lower()
            tag_list = [t.strip().lower() for t in (srv.get("tags") or "").split(",") if t.strip()]

            # type 优先；type 不明确时才用 tags 推断
            if srv_type in ("k8s", "argocd", "fluxcd"):
                is_k8s, is_docker, is_ssh = True, False, False
            elif srv_type == "docker":
                is_k8s, is_docker, is_ssh = False, True, False
            elif srv_type == "ssh":
                is_k8s, is_docker, is_ssh = False, False, True
            else:
                is_k8s = any(t in ("k8s", "kubernetes") for t in tag_list)
                is_docker = "docker" in tag_list
                is_ssh = "ssh" in tag_list or (not is_k8s and not is_docker)

            if is_k8s:
                entry["monitor_type"] = "k8s"

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

            elif is_docker:
                entry["monitor_type"] = "docker"
                entry["status"] = "available"
                # 快速检测 docker 是否可用
                docker_test = _ssh_cmd(ssh, "docker ps --format 'ok' 2>/dev/null | head -1")
                if not docker_test:
                    entry["status"] = "unavailable"
                    entry["hint"] = "Docker 不可用或权限不足"

            else:  # ssh / 其他
                entry["monitor_type"] = "ssh"
                entry["status"] = "available"
                # 检测是否有支持的监控命令
                has_top = _ssh_cmd(ssh, "which top 2>/dev/null")
                if not has_top:
                    entry["status"] = "unavailable"
                    entry["hint"] = "SSH 连接正常但缺少基础命令"

            ssh.close()

        except Exception as e:
            entry["status"] = "error"
            entry["error"] = str(e)

        result.append(entry)

    resp = {"enabled": True, "servers": result}
    _cache_set("servers:all", resp)
    return resp


# ── K8S ──

@router.get("/nodes/{server_id}")
def get_nodes(
    server_id: int,
    db: Database = Depends(get_db),
    username: str = Depends(verify_token),
):
    """获取 K8S 集群 Node 资源占用"""
    if not settings.monitoring_enabled:
        raise HTTPException(400, "监控功能未启用")

    cache_key = f"nodes:{server_id}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    conn = db.conn()
    srv = conn.execute("SELECT * FROM cd_servers WHERE id=?", (server_id,)).fetchone()
    conn.close()
    if not srv:
        raise HTTPException(404, "服务器不存在")

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
                        "cpu": parts[1], "memory": parts[2],
                        "max_pods": parts[3] if len(parts) > 3 else "?",
                    }

        for node in nodes:
            cap = capacities.get(node["name"], {})
            node["capacity_cpu"] = cap.get("cpu", "?")
            node["capacity_memory"] = cap.get("memory", "?")
            node["max_pods"] = cap.get("max_pods", "?")

        ssh.close()
        resp = {
            "success": True,
            "monitor_type": "k8s",
            "has_metrics": bool(nodes),
            "nodes": nodes,
            "hint": "" if nodes else "集群未安装 metrics-server，请运行: kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml",
        }
        _cache_set(cache_key, resp)
        return resp
    except Exception as e:
        raise HTTPException(500, f"SSH 连接失败: {e}")


@router.get("/pods/{server_id}")
def get_pods(
    server_id: int,
    namespace: str = "",
    db: Database = Depends(get_db),
    username: str = Depends(verify_token),
):
    """获取 K8S 集群 Pod 资源占用"""
    if not settings.monitoring_enabled:
        raise HTTPException(400, "监控功能未启用")

    cache_key = f"pods:{server_id}:{namespace}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    conn = db.conn()
    srv = conn.execute("SELECT * FROM cd_servers WHERE id=?", (server_id,)).fetchone()
    conn.close()
    if not srv:
        raise HTTPException(404, "服务器不存在")

    try:
        target = _make_target(srv)
        ssh = ssh_connect(target, settings.ssh_timeout)

        ns_list = _ssh_cmd(
            ssh,
            "kubectl get ns -o custom-columns=NAME:.metadata.name --no-headers 2>/dev/null",
        )
        namespaces = [ns.strip() for ns in ns_list.split("\n") if ns.strip()] if ns_list else []

        ns_filter = f"-n {namespace}" if namespace else "--all-namespaces"
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
                        status_map[key] = {"status": parts[1], "restarts": parts[2].replace("[","").replace("]",""), "node": parts[3]}
                    else:
                        key = f"{parts[0]}/{parts[1]}"
                        status_map[key] = {"status": parts[2], "restarts": parts[3].replace("[","").replace("]",""), "node": parts[4]}

        for pod in pods:
            key = f"{pod['namespace']}/{pod['name']}"
            info = status_map.get(key, {})
            pod["status"] = info.get("status", "?")
            pod["restarts"] = info.get("restarts", "0")
            pod["node"] = info.get("node", "?")

        ssh.close()
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
        raise HTTPException(500, f"SSH 连接失败: {e}")


@router.get("/pod-detail/{server_id}")
def get_pod_detail(
    server_id: int,
    namespace: str,
    name: str,
    db: Database = Depends(get_db),
    username: str = Depends(verify_token),
):
    """获取单个 K8S Pod 详情"""
    if not settings.monitoring_enabled:
        raise HTTPException(400, "监控功能未启用")

    cache_key = f"pod_detail:{server_id}:{namespace}:{name}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    conn = db.conn()
    srv = conn.execute("SELECT * FROM cd_servers WHERE id=?", (server_id,)).fetchone()
    conn.close()
    if not srv:
        raise HTTPException(404, "服务器不存在")

    try:
        target = _make_target(srv)
        ssh = ssh_connect(target, settings.ssh_timeout)
        describe = _ssh_cmd(ssh, f"kubectl describe pod {name} -n {namespace} 2>/dev/null | tail -30")
        logs = _ssh_cmd(ssh, f"kubectl logs {name} -n {namespace} --tail=20 2>/dev/null")
        top = _ssh_cmd(ssh, f"kubectl top pod {name} -n {namespace} --no-headers 2>/dev/null")
        ssh.close()
        resp = {"success": True, "name": name, "namespace": namespace, "top": top, "describe": describe, "logs": logs}
        _cache_set(cache_key, resp)
        return resp
    except Exception as e:
        raise HTTPException(500, f"SSH 连接失败: {e}")


# ── Docker ──

@router.get("/docker/{server_id}")
def get_docker_containers(
    server_id: int,
    db: Database = Depends(get_db),
    username: str = Depends(verify_token),
):
    """获取 Docker 服务器容器资源占用（仅容器，系统资源走 /system）"""
    if not settings.monitoring_enabled:
        raise HTTPException(400, "监控功能未启用")

    cache_key = f"docker:{server_id}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    conn = db.conn()
    srv = conn.execute("SELECT * FROM cd_servers WHERE id=?", (server_id,)).fetchone()
    conn.close()
    if not srv:
        raise HTTPException(404, "服务器不存在")

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
                    containers.append({
                        "name": parts[0],
                        "cpu": parts[1],
                        "memory": parts[2],
                        "memory_percent": parts[3],
                        "net_io": parts[4] if len(parts) > 4 else "?",
                        "block_io": parts[5] if len(parts) > 5 else "?",
                    })

        ssh.close()
        resp = {
            "success": True,
            "monitor_type": "docker",
            "containers": containers,
        }
        _cache_set(cache_key, resp)
        return resp
    except Exception as e:
        raise HTTPException(500, f"SSH 连接失败: {e}")


# ── 系统资源（通用：K8S / Docker / SSH 都可用）──

@router.get("/system/{server_id}")
def get_system_info(
    server_id: int,
    db: Database = Depends(get_db),
    username: str = Depends(verify_token),
):
    """获取服务器系统资源：CPU、内存、磁盘、负载、进程 Top5（所有类型通用）"""
    if not settings.monitoring_enabled:
        raise HTTPException(400, "监控功能未启用")

    cache_key = f"system:{server_id}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    conn = db.conn()
    srv = conn.execute("SELECT * FROM cd_servers WHERE id=?", (server_id,)).fetchone()
    conn.close()
    if not srv:
        raise HTTPException(404, "服务器不存在")

    try:
        target = _make_target(srv)
        ssh = ssh_connect(target, settings.ssh_timeout)
        info = _ssh_test(ssh)

        top_out = _ssh_cmd(
            ssh,
            "ps aux --sort=-%cpu --no-headers 2>/dev/null | head -5 | awk '{print $2\"|\"$3\"|\"$4\"|\"$11}'",
        )
        processes = []
        if top_out:
            for line in top_out.strip().split("\n"):
                parts = line.split("|")
                if len(parts) >= 4:
                    processes.append({
                        "pid": parts[0], "cpu": parts[1],
                        "mem": parts[2], "cmd": parts[3],
                    })

        ssh.close()
        resp = {
            "success": True,
            "system": info,
            "top_processes": processes,
        }
        _cache_set(cache_key, resp)
        return resp
    except Exception as e:
        raise HTTPException(500, f"SSH 连接失败: {e}")
