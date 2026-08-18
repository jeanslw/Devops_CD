"""监控工具函数 — SSH 测试 + 缓存 + 目标构造（被 monitor 路由和 alert_service 共用）"""

import time

from backend.config import settings
from backend.crypto import decrypt
from backend.deployers.base import DeployTarget, _ssh_cmd

# ── 简易内存缓存 ──
_cache: dict[str, tuple[float, object]] = {}

# TTL 从配置读取，可按类型在 .env 中覆盖
_CACHE_TTL: dict[str, int] = {}


def _get_cache_ttl() -> dict[str, int]:
    """懒加载缓存 TTL，确保 settings 已初始化"""
    if not _CACHE_TTL:
        _CACHE_TTL.update(
            {
                "servers": settings.monitor_cache_servers,
                "system": settings.monitor_cache_system,
                "nodes": settings.monitor_cache_nodes,
                "pods": settings.monitor_cache_pods,
                "docker": settings.monitor_cache_docker,
                "pod_detail": settings.monitor_cache_pod_detail,
            }
        )
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
        if (
            key.startswith("servers:")
            or key.startswith("system:")
            or key.startswith("docker:")
            or key.startswith("nodes:")
            or key.startswith("pods:")
        ):
            del _cache[key]


# ── 工具函数 ──


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
            items.append(
                {
                    "name": parts[0],
                    "cpu": parts[1],
                    "cpu_percent": parts[2],
                    "memory": parts[3],
                    "memory_percent": parts[4],
                }
            )
        elif len(parts) >= 3:
            items.append(
                {
                    "name": parts[0],
                    "cpu": parts[1],
                    "memory": parts[2],
                }
            )
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
        host=srv["host"],
        port=srv["port"],
        user=srv["user"],
        password=decrypt(srv["password"] or ""),
        ssh_key=decrypt(srv["ssh_key"] or ""),
    )
