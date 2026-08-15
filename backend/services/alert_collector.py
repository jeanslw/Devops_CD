"""告警指标采集器 — 连接服务器采集 CPU/内存/磁盘/Docker/Pod/进程/自定义指标"""

import logging
from contextlib import suppress

from backend.config import settings
from backend.deployers.base import ssh_connect
from backend.routers.custom_monitors import parse_output
from backend.services.monitor_utils import _make_target, _ssh_cmd, _ssh_test

logger = logging.getLogger("cd.alert")


def collect_alert_metrics(db, rule: dict, server: dict) -> list[dict]:
    """连接服务器，根据 resource_type 采集指标，返回超标项列表"""
    results = []
    resource_type = (rule.get("resource_type") or "").lower()
    threshold = rule.get("threshold", 80)

    try:
        target = _make_target(server)
        ssh = ssh_connect(target, settings.ssh_timeout)
    except Exception as e:
        logger.warning(f"Alert: failed to connect {server['host']}: {e}")
        return results

    try:
        if resource_type in ("cpu", "memory", "disk"):
            info = _ssh_test(ssh)
            if resource_type == "cpu":
                # 从 /proc/loadavg 取 1min 负载，除以核数算百分比
                load_str = info.get("load", "")
                cores_str = info.get("cpu_cores", "1")
                try:
                    cores = int(cores_str)
                except ValueError:
                    cores = 1
                if load_str:
                    load_1m = float(load_str.split()[0])
                    cpu_pct = round(load_1m / cores * 100)
                    if cpu_pct >= threshold:
                        results.append({"name": "", "value": cpu_pct, "resource": "cpu"})
            elif resource_type == "memory":
                mem_pct_str = info.get("memory_percent", "0").rstrip("%")
                try:
                    mem_pct = float(mem_pct_str)
                except ValueError:
                    mem_pct = 0
                if mem_pct >= threshold:
                    results.append({"name": "", "value": int(mem_pct), "resource": "memory"})
            elif resource_type == "disk":
                disk_pct_str = info.get("disk_percent", "0%").rstrip("%")
                try:
                    disk_pct = int(disk_pct_str)
                except ValueError:
                    disk_pct = 0
                if disk_pct >= threshold:
                    results.append({"name": "", "value": disk_pct, "resource": "disk"})

        elif resource_type.startswith("docker_"):
            # 采集 docker stats
            stats_out = _ssh_cmd(
                ssh,
                "docker stats --no-stream --format '{{.Name}}|{{.CPUPerc}}|{{.MemPerc}}' 2>/dev/null",
            )
            if stats_out:
                for line in stats_out.strip().split("\n"):
                    parts = line.split("|")
                    if len(parts) < 3:
                        continue
                    name = parts[0]
                    cpu_str = parts[1].rstrip("%")
                    mem_str = parts[2].rstrip("%")
                    try:
                        cpu_val = float(cpu_str)
                        mem_val = float(mem_str)
                    except ValueError:
                        continue
                    if resource_type == "docker_cpu" and cpu_val >= threshold:
                        results.append({"name": name, "value": int(cpu_val), "resource": resource_type, "sub_type": "docker"})
                    elif resource_type == "docker_memory" and mem_val >= threshold:
                        results.append({"name": name, "value": int(mem_val), "resource": resource_type, "sub_type": "docker"})

        elif resource_type.startswith("pod_"):
            # 采集 k8s pods
            top_out = _ssh_cmd(
                ssh,
                "kubectl top pods --all-namespaces --no-headers 2>/dev/null",
            )
            if top_out:
                for line in top_out.strip().split("\n"):
                    parts = line.split()
                    if len(parts) < 4:
                        continue
                    ns, pod_name = parts[0], parts[1]
                    cpu_str = parts[2].rstrip("m")
                    mem_str = parts[3].rstrip("Mi")
                    try:
                        cpu_val = float(cpu_str)
                        mem_val = float(mem_str)  # in Mi
                    except ValueError:
                        continue
                    # k8s top 显示的是绝对值，需要和 request/limit 对比
                    # 简化：cpu 按 mCore 判断 > threshold*10（100m=10% 1core），内存按 node 32Gi 估算
                    if resource_type == "pod_cpu":
                        if cpu_val > threshold * 10:
                            results.append({"name": f"{ns}/{pod_name}", "value": int(cpu_val / 10), "resource": resource_type, "sub_type": "pod"})
                    elif resource_type == "pod_memory":
                        limit_mi = 8 * 1024 * threshold / 100
                        if mem_val > limit_mi:
                            results.append({"name": f"{ns}/{pod_name}", "value": int(mem_val / (8 * 1024 / 100)), "resource": resource_type, "sub_type": "pod"})

        elif resource_type.startswith("process_"):
            # 采集 top5 进程
            top_out = _ssh_cmd(
                ssh,
                "ps aux --sort=-%cpu --no-headers 2>/dev/null | head -10 | awk '{print $2\"|\"$3\"|\"$4\"|\"$11}'",
            )
            if top_out:
                for line in top_out.strip().split("\n"):
                    parts = line.split("|")
                    if len(parts) < 3:
                        continue
                    pid, cpu_str, mem_str = parts[0], parts[1], parts[2]
                    cmd = parts[3] if len(parts) > 3 else pid
                    try:
                        cpu_val = float(cpu_str)
                        mem_val = float(mem_str)
                    except ValueError:
                        continue
                    if resource_type == "process_cpu" and cpu_val >= threshold:
                        results.append({"name": f"{cmd}({pid})", "value": int(cpu_val), "resource": resource_type, "sub_type": "process"})
                    elif resource_type == "process_memory" and mem_val >= threshold:
                        results.append({"name": f"{cmd}({pid})", "value": int(mem_val), "resource": resource_type, "sub_type": "process"})

        elif resource_type.startswith("custom_"):
            # ── 自定义监控项 ──
            # 新格式: custom_<monitor_id>_<metric_id>  (指定指标)
            # 旧格式: custom_<monitor_id>              (兼容：取第一个指标/单值)
            parts = resource_type.split("_")
            try:
                monitor_id = int(parts[1])
            except (IndexError, ValueError):
                return results
            metric_id = int(parts[2]) if len(parts) > 2 else None

            with db.conn() as conn:
                row = conn.execute("SELECT * FROM cd_custom_monitors WHERE id=? AND enabled=1", (monitor_id,)).fetchone()
                if row:
                    metrics = conn.execute(
                        "SELECT * FROM cd_custom_monitor_metrics WHERE monitor_id=? ORDER BY sort_order, id",
                        (monitor_id,),
                    ).fetchall()
                else:
                    metrics = []

            if not row:
                return results

            custom = dict(row)
            raw = (_ssh_cmd(ssh, custom["command"]) or "").strip()
            metrics_list = [dict(m) for m in metrics]

            # 用统一解析器解析
            parsed = parse_output(raw, custom.get("output_format", "auto"), metrics_list)

            for item in parsed:
                val = item.get("value")
                if val is None:
                    continue

                # 如果指定了 metric_id，只取匹配的指标
                if metric_id is not None:
                    matched = [m for m in metrics_list if m["id"] == metric_id]
                    if not matched or item["field_key"] != matched[0].get("field_key", ""):
                        continue

                if val >= threshold:
                    entity_label = item.get("entity_label", "")
                    display_name = custom["name"]
                    if entity_label:
                        display_name = f"{custom['name']}[{entity_label}]"
                    if item.get("metric_name"):
                        display_name = f"{display_name} {item['metric_name']}"

                    results.append({
                        "name": display_name,
                        "value": int(val),
                        "resource": resource_type,
                        "sub_type": "custom",
                        "unit": item.get("unit", ""),
                        "raw": item.get("raw_val", raw),
                        "entity_label": entity_label,
                        "metric_name": item.get("metric_name", ""),
                    })

    except Exception as e:
        logger.warning(f"Alert: metric collection failed for {server['host']}: {e}")
    finally:
        with suppress(Exception):
            ssh.close()

    return results
