"""告警规则管理路由"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.database import Database
from backend.auth import get_db, verify_token, require_perm
from backend.exceptions import NotFoundError
from backend.responses import ok

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


class AlertRuleRequest(BaseModel):
    name: str
    target_type: str = "system"    # system / app
    resource_type: str             # cpu/memory/disk/pod_cpu/pod_memory/docker_cpu/docker_memory/process_cpu/process_memory
    server_ids: str = ""           # 逗号分隔
    threshold: int = 80
    bot_id: int = 0
    template: str = ""
    enabled: bool = True
    cooldown_minutes: int = 10
    duration_minutes: int = 0      # 持续超标 N 分钟后报警，0=立即


@router.get("")
def list_alerts(
    db: Database = Depends(get_db),
    username: str = Depends(verify_token),
):
    """获取所有告警规则"""
    with db.conn() as conn:
        rows = conn.execute("SELECT * FROM cd_alert_rules ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]


@router.post("")
def create_alert(
    req: AlertRuleRequest,
    db: Database = Depends(get_db),
    _user: dict = Depends(require_perm("cd.monitor.alert")),
):
    """创建告警规则"""
    with db.conn() as conn:
        conn.execute(
            "INSERT INTO cd_alert_rules (name, target_type, resource_type, server_ids, threshold, bot_id, template, enabled, cooldown_minutes, duration_minutes) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (req.name, req.target_type, req.resource_type, req.server_ids, req.threshold, req.bot_id, req.template, 1 if req.enabled else 0, req.cooldown_minutes, req.duration_minutes),
        )
        return ok(message="告警规则已创建")


@router.put("/{rule_id}")
def update_alert(
    rule_id: int,
    req: AlertRuleRequest,
    db: Database = Depends(get_db),
    _user: dict = Depends(require_perm("cd.monitor.alert")),
):
    """更新告警规则"""
    with db.conn() as conn:
        existing = conn.execute("SELECT id FROM cd_alert_rules WHERE id=?", (rule_id,)).fetchone()
        if not existing:
            raise NotFoundError("告警规则不存在")

        conn.execute(
            "UPDATE cd_alert_rules SET name=?, target_type=?, resource_type=?, server_ids=?, threshold=?, bot_id=?, template=?, enabled=?, cooldown_minutes=?, duration_minutes=? "
            "WHERE id=?",
            (req.name, req.target_type, req.resource_type, req.server_ids, req.threshold, req.bot_id, req.template, 1 if req.enabled else 0, req.cooldown_minutes, req.duration_minutes, rule_id),
        )
        return ok(message="告警规则已更新")


@router.delete("/{rule_id}")
def delete_alert(
    rule_id: int,
    db: Database = Depends(get_db),
    _user: dict = Depends(require_perm("cd.monitor.alert")),
):
    """删除告警规则"""
    with db.conn() as conn:
        conn.execute("DELETE FROM cd_alert_rules WHERE id=?", (rule_id,))
        return ok(message="告警规则已删除")


@router.get("/resource-types")
def list_resource_types(
    db: Database = Depends(get_db),
    username: str = Depends(verify_token),
):
    """返回可用的资源类型列表（给前端下拉用），自定义项按 采集器 → 指标 层级"""
    with db.conn() as conn:
        custom_rows = conn.execute(
            "SELECT id, name FROM cd_custom_monitors WHERE enabled=1 ORDER BY name"
        ).fetchall()
        custom_metrics = {}  # monitor_id → [{metric}]
        for r in custom_rows:
            mid = r["id"]
            rows = conn.execute(
                "SELECT * FROM cd_custom_monitor_metrics WHERE monitor_id=? ORDER BY sort_order, id",
                (mid,),
            ).fetchall()
            custom_metrics[mid] = [dict(m) for m in rows]

    custom = []
    for r in custom_rows:
        mid = r["id"]
        metrics = custom_metrics.get(mid, [])
        group = {
            "value": f"custom_{mid}",
            "label_en": r["name"],
            "label_zh": r["name"],
            "children": [
                {
                    "value": f"custom_{mid}_{m['id']}",
                    "label_en": m["name"],
                    "label_zh": m["name"],
                }
                for m in metrics
            ],
        }
        # 没有子指标时，value 自身就是可选资源（兼容旧逻辑）
        custom.append(group)

    return {
        "system": [
            {"value": "cpu", "label_en": "CPU", "label_zh": "CPU"},
            {"value": "memory", "label_en": "Memory", "label_zh": "内存"},
            {"value": "disk", "label_en": "Disk", "label_zh": "硬盘"},
        ],
        "app": [
            {"value": "pod_cpu", "label_en": "Pod CPU", "label_zh": "Pod CPU"},
            {"value": "pod_memory", "label_en": "Pod Memory", "label_zh": "Pod 内存"},
            {"value": "docker_cpu", "label_en": "Container CPU", "label_zh": "容器 CPU"},
            {"value": "docker_memory", "label_en": "Container Memory", "label_zh": "容器 内存"},
            {"value": "process_cpu", "label_en": "Process CPU", "label_zh": "进程 CPU"},
            {"value": "process_memory", "label_en": "Process Memory", "label_zh": "进程 内存"},
        ],
        "custom": custom,
    }


@router.post("/check")
def manual_check(
    _user: dict = Depends(require_perm("cd.monitor.alert")),
):
    """手动触发一次告警检测"""
    from backend.services.alert_service import check_all_rules
    check_all_rules()
    return ok(message="手动告警检测已触发")
