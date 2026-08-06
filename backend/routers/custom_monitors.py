"""自定义监控项管理路由 — 支持多指标"""

import csv
import io
import json as _json
import logging
import re

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.database import Database
from backend.auth import get_db, verify_token, require_perm
from backend.services.monitor_utils import _make_target
from backend.deployers.base import ssh_connect, _ssh_cmd
from backend.config import settings
from backend.exceptions import NotFoundError, ValidationError
from backend.responses import ok

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/custom-monitors", tags=["custom-monitors"])


# ── 请求模型 ──
class MetricDef(BaseModel):
    """单个指标定义"""
    id: int | None = None        # 编辑时传，新建时 null
    name: str = ""
    field_key: str = ""
    unit: str = ""
    sort_order: int = 0


class CustomMonitorRequest(BaseModel):
    name: str
    command: str
    output_format: str = "auto"   # auto / csv / kv / json
    description: str = ""
    server_ids: str = ""
    enabled: bool = True
    metrics: list[MetricDef] = []  # 指标列表


# ── 工具：多格式解析 ──
def parse_output(raw: str, output_format: str, metrics: list[dict]) -> list[dict]:
    """
    按指定格式解析命令输出，返回结构化结果。
    每条结果: { metric_name, field_key, value: float|None, unit, entity: dict }
    entity 包含行级标签（如 CSV 第一列作为实体标识）。
    """
    if not raw or not raw.strip():
        return []

    fmt = (output_format or "auto").strip().lower()

    if fmt == "csv":
        return _parse_csv(raw, metrics)
    elif fmt == "kv":
        return _parse_kv(raw, metrics)
    elif fmt == "json":
        return _parse_json(raw, metrics)
    else:
        # auto / 兼容旧数据：回退到单值提取
        return _parse_auto(raw, metrics)


def _parse_csv(raw: str, metrics: list[dict]) -> list[dict]:
    """CSV/TSV/空白分隔 解析：第一行表头，后续每行是一条实体。
    自动识别逗号/制表符/分号/竖线分隔（csv.Sniffer），不识别时回退到空白拆分（处理 free -m 等变长空白输出）。
    """
    results = []
    text = raw.strip()
    if not text:
        return results

    # 尝试用 Sniffer 探测分隔符
    delim = None
    try:
        delim = csv.Sniffer().sniff(text[:4096], delimiters=",\t;|").delimiter
    except Exception:
        pass

    lines = text.split("\n")
    if delim:
        reader = csv.DictReader(io.StringIO(text), delimiter=delim)
        headers = [h.strip() for h in (reader.fieldnames or [])]
        rows = list(reader)
    else:
        # 变长空白（如 free -m）：按空白拆分
        split_lines = [re.split(r"\s+", l.strip()) for l in lines if l.strip()]
        if len(split_lines) < 2:
            return results
        headers = split_lines[0]
        rows = []
        for parts in split_lines[1:]:
            # 仿 DictReader：第一列可能是行标签
            if len(parts) == len(headers) + 1:
                row = {"": parts[0]}
                for h, v in zip(headers, parts[1:]):
                    row[h] = v
            else:
                row = dict(zip(headers, parts))
            rows.append(row)

    for row in rows:
        entity = {k: (v.strip() if isinstance(v, str) else v) for k, v in row.items()}
        # 行标签列（如果有，空字符串 key ""）作为 entity_label，否则用第一列值
        entity_label = entity.pop("", "") or (next(iter(entity.values())) if entity else "")
        for m in metrics:
            fk = m.get("field_key", "").strip()
            if not fk:
                continue
            val_str = str(entity.get(fk, "") or "")
            val = _try_float(val_str)
            results.append({
                "metric_name": m.get("name", fk),
                "field_key": fk,
                "value": val,
                "raw_val": val_str,
                "unit": m.get("unit", ""),
                "entity": entity,
                "entity_label": entity_label,
            })
    return results


def _parse_kv(raw: str, metrics: list[dict]) -> list[dict]:
    """key=value 或 key: value 解析"""
    results = []
    kv_map = {}
    for line in raw.strip().split("\n"):
        line = line.strip()
        # 支持 key=value 和 key: value
        m = re.match(r"^([^=:]+)[=:]\s*(.*)", line)
        if m:
            kv_map[m.group(1).strip()] = m.group(2).strip()

    for m in metrics:
        fk = m.get("field_key", "").strip()
        if not fk or fk not in kv_map:
            continue
        val_str = kv_map[fk]
        val = _try_float(val_str)
        results.append({
            "metric_name": m.get("name", fk),
            "field_key": fk,
            "value": val,
            "raw_val": val_str,
            "unit": m.get("unit", ""),
            "entity": kv_map,
            "entity_label": "",
        })
    return results


def _parse_json(raw: str, metrics: list[dict]) -> list[dict]:
    """JSON 解析：支持对象或数组"""
    results = []
    try:
        data = _json.loads(raw)
    except Exception:
        return results

    items = data if isinstance(data, list) else [data]
    for obj in items:
        if not isinstance(obj, dict):
            continue
        entity_label = str(list(obj.values())[0]) if obj else ""
        for m in metrics:
            fk = m.get("field_key", "").strip()
            if not fk:
                continue
            # 支持嵌套 key 如 "gpu.utilization"
            val = _json_get(obj, fk)
            if val is None:
                continue
            val_str = str(val)
            num = _try_float(val_str)
            results.append({
                "metric_name": m.get("name", fk),
                "field_key": fk,
                "value": num,
                "raw_val": val_str,
                "unit": m.get("unit", ""),
                "entity": obj,
                "entity_label": entity_label,
            })
    return results


def _parse_auto(raw: str, metrics: list[dict]) -> list[dict]:
    """auto 模式：优先 CSV，失败回退单值"""
    if metrics and metrics[0].get("field_key", "").strip():
        # 有指标定义 → 尝试 CSV
        r = _parse_csv(raw, metrics)
        if r:
            return r
    # 回退：单值提取（兼容旧数据）
    val = _parse_number(raw)
    if val is None:
        return []
    label = (metrics[0].get("name") if metrics else "") or "value"
    return [{
        "metric_name": label,
        "field_key": "",
        "value": val,
        "raw_val": str(val),
        "unit": metrics[0].get("unit", "") if metrics else "",
        "entity": {},
        "entity_label": "",
    }]


def _parse_number(raw: str) -> float | None:
    """从命令输出中提取数值（兼容旧逻辑）"""
    if not raw:
        return None
    text = raw.strip()
    try:
        return float(text)
    except ValueError:
        pass
    for line in reversed(text.split("\n")):
        m = re.search(r"[-+]?\d+\.?\d*", line.strip())
        if m:
            try:
                return float(m.group())
            except ValueError:
                continue
    return None


def _try_float(s: str) -> float | None:
    """尝试转 float，去百分号/逗号/容量单位后缀(G/M/K/T/B)"""
    s = (s or "").strip()
    if not s:
        return None
    s = s.rstrip("%").replace(",", "")
    # 剥离容量单位后缀（不换算，只提取数字）：20G → 20, 1.5GiB → 1.5, 300M → 300
    s = re.sub(r"(?i)([kmgtp]i?b?|b)$", r"", s)
    try:
        return float(s)
    except ValueError:
        return None


def _json_get(obj: dict, path: str):
    """简易 JSON path 取值，支持 "a.b.c" """
    cur = obj
    for seg in path.split("."):
        if isinstance(cur, dict) and seg in cur:
            cur = cur[seg]
        else:
            return None
    return cur


def _diagnose_parse(raw: str, metrics: list[dict]) -> dict | None:
    """当解析结果为空时，提取原始输出中的可用字段，帮助用户排查配置问题"""
    if not metrics or not raw.strip():
        return None

    text = raw.strip()
    lines = text.split("\n")
    if len(lines) < 2:
        return None

    # 尝试从第一行提取表头
    # 先尝试 Sniffer 探测分隔符
    delim = None
    try:
        delim = csv.Sniffer().sniff(text[:4096], delimiters=",\t;|").delimiter
    except Exception:
        pass

    if delim:
        headers = [h.strip() for h in lines[0].split(delim)]
    else:
        headers = [h.strip() for h in re.split(r"\s+", lines[0].strip()) if h]

    configured_keys = [m.get("field_key", "").strip() for m in metrics if m.get("field_key", "").strip()]

    # 逐列精确匹配检测
    matched_headers = [h for h in headers if h in configured_keys]
    unmatched_keys = [k for k in configured_keys if k not in headers]

    return {
        "available_headers": headers,
        "configured_keys": configured_keys,
        "matched_headers": matched_headers,
        "unmatched_keys": unmatched_keys,
        "hint": (
            "field_key 需与输出表头精确匹配（区分大小写）。"
            "如果表头是中文（如'已用%'），请将 field_key 改为对应中文，"
            "或在命令前加 LANG=C 强制英文输出（如 LANG=C df -h）。"
            if unmatched_keys and len(unmatched_keys) == len(configured_keys) else ""
        ),
    }


# ── 指标 DB 操作 ──
def _save_metrics(conn, monitor_id: int, metrics: list[MetricDef]):
    """全量替换监控项的指标定义"""
    conn.execute("DELETE FROM cd_custom_monitor_metrics WHERE monitor_id=?", (monitor_id,))
    for m in metrics:
        if not m.name.strip() or not m.field_key.strip():
            continue
        conn.execute(
            "INSERT INTO cd_custom_monitor_metrics (monitor_id, name, field_key, unit, sort_order) "
            "VALUES (?,?,?,?,?)",
            (monitor_id, m.name.strip(), m.field_key.strip(), m.unit.strip(), m.sort_order),
        )


def _load_metrics(conn, monitor_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM cd_custom_monitor_metrics WHERE monitor_id=? ORDER BY sort_order, id",
        (monitor_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ── CRUD ──
@router.get("")
def list_monitors(
    db: Database = Depends(get_db),
    username: str = Depends(verify_token),
):
    """获取所有自定义监控项（含指标）"""
    with db.conn() as conn:
        rows = conn.execute("SELECT * FROM cd_custom_monitors ORDER BY created_at DESC").fetchall()
        result = []
        for r in rows:
            item = dict(r)
            item["metrics"] = _load_metrics(conn, item["id"])
            result.append(item)
    return result


@router.post("")
def create_monitor(
    req: CustomMonitorRequest,
    db: Database = Depends(get_db),
    _user: dict = Depends(require_perm("cd.monitor.custom")),
):
    """创建自定义监控项"""
    with db.conn() as conn:
        cur = conn.execute(
            "INSERT INTO cd_custom_monitors (name, command, output_format, description, server_ids, enabled) "
            "VALUES (?,?,?,?,?,?)",
            (req.name, req.command, req.output_format, req.description, req.server_ids, 1 if req.enabled else 0),
        )
        new_id = cur.lastrowid  # sqlite3 / pymysql 都支持
        if req.metrics:
            _save_metrics(conn, new_id, req.metrics)
    return ok(message=f"监控项 '{req.name}' 已创建")


@router.put("/{monitor_id}")
def update_monitor(
    monitor_id: int,
    req: CustomMonitorRequest,
    db: Database = Depends(get_db),
    _user: dict = Depends(require_perm("cd.monitor.custom")),
):
    """更新自定义监控项"""
    with db.conn() as conn:
        existing = conn.execute("SELECT id FROM cd_custom_monitors WHERE id=?", (monitor_id,)).fetchone()
        if not existing:
            raise NotFoundError("监控项不存在", error_key="errors.monitor_not_found")

        conn.execute(
            "UPDATE cd_custom_monitors SET name=?, command=?, output_format=?, description=?, "
            "server_ids=?, enabled=? WHERE id=?",
            (req.name, req.command, req.output_format, req.description,
             req.server_ids, 1 if req.enabled else 0, monitor_id),
        )
        _save_metrics(conn, monitor_id, req.metrics)
    return ok(message=f"监控项 '{req.name}' 已更新")


@router.delete("/{monitor_id}")
def delete_monitor(
    monitor_id: int,
    db: Database = Depends(get_db),
    _user: dict = Depends(require_perm("cd.monitor.custom")),
):
    """删除自定义监控项（级联删除指标）"""
    with db.conn() as conn:
        conn.execute("DELETE FROM cd_custom_monitor_metrics WHERE monitor_id=?", (monitor_id,))
        conn.execute("DELETE FROM cd_custom_monitors WHERE id=?", (monitor_id,))
    return ok(message="监控项已删除")


@router.post("/{monitor_id}/test")
def test_monitor(
    monitor_id: int,
    db: Database = Depends(get_db),
    username: str = Depends(verify_token),
):
    """测试运行自定义监控命令，返回每台服务器的解析后结构化结果"""
    with db.conn() as conn:
        row = conn.execute("SELECT * FROM cd_custom_monitors WHERE id=?", (monitor_id,)).fetchone()
        if not row:
            return {"success": False, "detail": "not found"}
        monitor = dict(row)
        metrics = _load_metrics(conn, monitor_id)
        server_ids_str = (monitor.get("server_ids") or "").strip()
        if server_ids_str:
            ids = [int(x) for x in server_ids_str.split(",") if x.strip().isdigit()]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                servers = conn.execute(
                    f"SELECT * FROM cd_servers WHERE id IN ({placeholders})", ids
                ).fetchall()
            else:
                servers = []
        else:
            servers = conn.execute(
                "SELECT * FROM cd_servers WHERE type IN ('ssh','docker')"
            ).fetchall()

    results = []
    for server in servers:
        server = dict(server)
        try:
            target = _make_target(server)
            ssh = ssh_connect(target, settings.ssh_timeout)
            try:
                out = (_ssh_cmd(ssh, monitor["command"]) or "").strip()
            finally:
                try:
                    ssh.close()
                except Exception:
                    pass

            parsed = parse_output(out, monitor.get("output_format", "auto"), metrics)
            result_item = {
                "server_id": server["id"],
                "server_name": server["name"],
                "host": server["host"],
                "output": out,
                "parsed": parsed,
                "error": "",
            }
            # 解析为空但输出非空 → 附加诊断信息
            if (not parsed) and out.strip() and metrics:
                diag = _diagnose_parse(out, metrics)
                if diag:
                    result_item["diagnostic"] = diag
            results.append(result_item)
        except Exception as e:
            logger.error(f"Custom monitor test failed for server {server.get('id')}", exc_info=e)
            results.append({
                "server_id": server["id"],
                "server_name": server.get("name", "?"),
                "host": server.get("host", "?"),
                "output": "",
                "parsed": [],
                "error": "命令执行失败，请联系管理员",
            })

    return {"success": True, "results": results}
