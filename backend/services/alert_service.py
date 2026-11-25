"""告警检测服务 - 后台线程定时检测阈值 + 推送 Bot 通知"""

import logging
import threading
import time
from datetime import datetime, timezone

from backend.config import settings
from backend.database import Database
from backend.services.alert_collector import collect_alert_metrics
from backend.services.notification import render_template, send_webhook

logger = logging.getLogger("cd.alert")

_STATE_TTL_SECONDS = 86400

_cooldowns: dict[str, float] = {}

_durations: dict[str, float] = {}

_pending_langs: dict[str, set[str]] = {}

_retry_not_before: dict[str, float] = {}

_runner: threading.Thread | None = None
_stop = threading.Event()

_DEFAULT_TEMPLATES = {
    "system": {
        "zh": ("⚠️ [资源告警] {time}\n服务器：{server}({host})\n资源：{resource_name} ({value}%)\n阈值：{threshold}%"),
        "en": (
            "⚠️ [Resource Alert] {time}\n"
            "Server: {server}({host})\n"
            "Resource: {resource_name} ({value}%)\n"
            "Threshold: {threshold}%"
        ),
    },
    "app": {
        "zh": (
            "⚠️ [应用告警] {time}\n"
            "服务器：{server}({host})\n"
            "类型：{type_name}\n"
            "名称：{name}\n"
            "资源：{resource_name} ({value}%)\n"
            "阈值：{threshold}%"
        ),
        "en": (
            "⚠️ [App Alert] {time}\n"
            "Server: {server}({host})\n"
            "Type: {type_name}\n"
            "Name: {name}\n"
            "Resource: {resource_name} ({value}%)\n"
            "Threshold: {threshold}%"
        ),
    },
}

_RESOURCE_NAMES = {
    "zh": {
        "cpu": "CPU",
        "memory": "内存",
        "disk": "硬盘",
        "pod_cpu": "Pod CPU",
        "pod_memory": "Pod 内存",
        "docker_cpu": "容器 CPU",
        "docker_memory": "容器 内存",
        "process_cpu": "进程 CPU",
        "process_memory": "进程 内存",
    },
    "en": {
        "cpu": "CPU",
        "memory": "Memory",
        "disk": "Disk",
        "pod_cpu": "Pod CPU",
        "pod_memory": "Pod Memory",
        "docker_cpu": "Container CPU",
        "docker_memory": "Container Memory",
        "process_cpu": "Process CPU",
        "process_memory": "Process Memory",
    },
}

_TYPE_NAMES = {
    "zh": {"pod": "Pod", "docker": "容器", "process": "进程"},
    "en": {"pod": "Pod", "docker": "Container", "process": "Process"},
}


def _track_key(rule_id: int, server_id: int, entity: str = "") -> str:
    """统一追踪键:规则 + 服务器 + 实体标签"""
    return f"{rule_id}_{server_id}_{entity}"


def _check_cooldown(rule_id: int, server_id: int, cooldown_minutes: int, entity: str = "") -> bool:
    """检查是否在冷却期,返回 True = 跳过(纯查询,不修改状态)"""
    key = _track_key(rule_id, server_id, entity)
    last = _cooldowns.get(key, 0)
    return (time.time() - last) < (cooldown_minutes or 0) * 60


def _set_cooldown(rule_id: int, server_id: int, entity: str = ""):
    """记录告警发送时间"""
    key = _track_key(rule_id, server_id, entity)
    _cooldowns[key] = time.time()
    for k in list(_cooldowns):
        if time.time() - _cooldowns[k] > _STATE_TTL_SECONDS:
            del _cooldowns[k]


# ── 持续时间追踪 ──


def _mark_exceeded(rule_id: int, server_id: int, entity: str = ""):
    """记录首次超标时间"""
    key = _track_key(rule_id, server_id, entity)
    if key not in _durations:
        _durations[key] = time.time()
    for k in list(_durations):
        if time.time() - _durations[k] > _STATE_TTL_SECONDS:
            del _durations[k]


def _get_exceeded_seconds(rule_id: int, server_id: int, entity: str = "") -> float:
    """返回已持续超标的秒数"""
    first = _durations.get(_track_key(rule_id, server_id, entity), 0)
    return time.time() - first if first else 0


def _clear_duration(rule_id: int, server_id: int, entity: str = ""):
    """重置持续时间追踪"""
    _durations.pop(_track_key(rule_id, server_id, entity), None)


def _check_retry_backoff(key: str) -> bool:
    return time.time() < _retry_not_before.get(key, 0)


def _set_retry_backoff(key: str, seconds: float):
    _retry_not_before[key] = time.time() + seconds
    for k in list(_retry_not_before):
        if time.time() - _retry_not_before[k] > _STATE_TTL_SECONDS:
            del _retry_not_before[k]


def _clear_send_state(key: str):
    _pending_langs.pop(key, None)
    _retry_not_before.pop(key, None)


def check_all_rules():
    """检查所有启用的告警规则"""
    db = Database()
    with db.conn() as conn:
        rules = conn.execute("SELECT * FROM cd_alert_rules WHERE enabled=1").fetchall()

    for rule in rules:
        try:
            _check_one(db, dict(rule))
        except Exception as e:
            logger.warning("Alert: rule %s check failed: %s", rule["id"], e)


def _check_one(db, rule: dict):
    """检查单条规则"""
    rule_id = rule["id"]
    resource_type = (rule.get("resource_type") or "").lower()
    target_type = rule.get("target_type", "system")
    bot_id = rule.get("bot_id", 0)
    cooldown = rule.get("cooldown_minutes", 10)
    duration_minutes = rule.get("duration_minutes", 0)
    template = (rule.get("template") or "").strip()
    server_ids_str = (rule.get("server_ids") or "").strip()

    if not resource_type or not bot_id:
        return

    with db.conn() as conn:
        if server_ids_str:
            ids = [int(x) for x in server_ids_str.split(",") if x.strip().isdigit()]
            if not ids:
                return
            placeholders = ",".join("?" for _ in ids)
            servers = conn.execute(f"SELECT * FROM cd_servers WHERE id IN ({placeholders})", ids).fetchall()  # nosec
        else:
            servers = conn.execute("SELECT * FROM cd_servers WHERE type IN ('ssh','docker')").fetchall()

    bot = _get_bot(db, bot_id)
    if not bot:
        return

    for server in servers:
        server = dict(server)
        sid = server["id"]

        try:
            results = collect_alert_metrics(db, rule, server)
        except Exception as e:
            logger.warning("Alert: skip server %s (rule %s), collection failed: %s", sid, rule_id, e)
            continue

        if results:
            for r in results:
                entity = r.get("entity_label", "")

                _mark_exceeded(rule_id, sid, entity)

                elapsed = _get_exceeded_seconds(rule_id, sid, entity)
                required = duration_minutes * 60

                if required > 0 and elapsed < required:
                    continue

                if _check_cooldown(rule_id, sid, cooldown, entity):
                    continue

                key = _track_key(rule_id, sid, entity)
                if _check_retry_backoff(key):
                    continue

                pending = _pending_langs.setdefault(key, {"zh", "en"})
                for lang in ("zh", "en"):
                    if lang in pending and _send_alert(rule, server, r, bot, template, lang, target_type):
                        pending.discard(lang)

                if pending:
                    _set_retry_backoff(key, max((cooldown or 0) * 60, settings.alert_check_interval))
                else:
                    _set_cooldown(rule_id, sid, entity)
                    _clear_duration(rule_id, sid, entity)
                    _clear_send_state(key)
        else:
            _clear_duration(rule_id, sid)
            prefix = f"{rule_id}_{sid}_"
            for k in list(_durations):
                if k.startswith(prefix):
                    _durations.pop(k, None)
            for k in list(_pending_langs):
                if k.startswith(prefix):
                    _pending_langs.pop(k, None)
            for k in list(_retry_not_before):
                if k.startswith(prefix):
                    _retry_not_before.pop(k, None)


def _get_bot(db, bot_id: int):
    with db.conn() as conn:
        return conn.execute("SELECT * FROM cd_bots WHERE id=?", (bot_id,)).fetchone()


def _send_alert(rule: dict, server: dict, result: dict, bot, template: str, lang: str, target_type: str) -> bool:
    """构造并发送告警通知,返回发送结果"""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    resource_type = result["resource"]

    if resource_type.startswith("custom_"):
        resource_name = result.get("name", resource_type)
    else:
        resource_name = _RESOURCE_NAMES.get(lang, _RESOURCE_NAMES["en"]).get(resource_type, resource_type)

    values = {
        "time": now,
        "server": server.get("name", "?"),
        "host": server.get("host", "?"),
        "resource_name": resource_name,
        "value": result["value"],
        "threshold": rule.get("threshold", 80),
        "name": result.get("name", ""),
        "type_name": _TYPE_NAMES.get(lang, _TYPE_NAMES["en"]).get(
            result.get("sub_type", ""), result.get("sub_type", "")
        ),
        "resource": resource_name,
        "unit": result.get("unit", ""),
        "raw": result.get("raw", ""),
        "entity": result.get("entity_label", ""),
        "metric": result.get("metric_name", ""),
    }
    default_tpl = _DEFAULT_TEMPLATES.get(target_type, _DEFAULT_TEMPLATES["system"]).get(lang, "")
    msg = render_template(template, values, default_tpl.format(**values))
    logger.info(f"Alert: {server['name']} {resource_type} {result['value']}% (threshold={rule['threshold']}%)")
    return send_webhook(bot["webhook_url"], msg, bot.get("type", ""))


def _run_loop():
    """后台线程主循环"""
    interval = getattr(settings, "alert_check_interval", 60)
    logger.info(f"Alert checker started, interval={interval}s")
    try:
        while not _stop.wait(interval):
            try:
                from backend.database import Database
                from backend.dlock import acquire

                if not acquire(Database(), "alert_check", ttl_seconds=max(interval * 3, 90)):
                    continue
                check_all_rules()
            except Exception as e:
                logger.error(f"Alert check error: {e}")
    finally:
        try:
            from backend.database import Database
            from backend.dlock import release

            release(Database(), "alert_check")
        except Exception:
            pass


def start_alert_checker():
    """启动告警检测后台线程"""
    global _runner
    if not settings.monitoring_enabled:
        logger.info("Alert checker disabled (monitoring disabled)")
        return
    if _runner and _runner.is_alive():
        return
    _stop.clear()
    _runner = threading.Thread(target=_run_loop, daemon=True)
    _runner.start()


def stop_alert_checker():
    """停止告警检测后台线程"""
    _stop.set()
    if _runner:
        _runner.join(timeout=5)
