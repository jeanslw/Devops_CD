"""审批服务 — 规则评估 + 审批单状态机 + 持久化队列（DB 队列 + 轮询器）。

v1.5.0 新增。设计：
1. 规则评估：cd_approval_rules 按项目（'*' 为全局默认）配置是否需要审批、
   哪些环境（require_envs，命中目标服务器 tags 才审批）、审批人
   （approvers 显式用户名优先，approver_role 兜底）。
2. 审批单状态机：
     pending → approved → deploying → deployed / failed
     pending → rejected / cancelled
   批准只把 pending 原子改成 approved（落库即不丢）；执行由后台线程完成。
3. 持久化队列：approved 是持久化真相源；轮询器幂等领取执行；进程重启后
   recover_on_startup 清僵尸 running 部署锁并重投 deploying 审批单。
"""

import json
import logging
import threading
import time
from datetime import datetime

from fastapi import HTTPException

from backend.auth import load_user_context
from backend.config import settings
from backend.database import Database
from backend.exceptions import NotFoundError
from backend.services.ci_service import CiService
from backend.services.deploy_executor import execute_from_params
from backend.services.notification import notify_approval

logger = logging.getLogger(__name__)

# 审批单状态
PENDING = "pending"
APPROVED = "approved"
DEPLOYING = "deploying"
DEPLOYED = "deployed"
FAILED = "failed"
REJECTED = "rejected"
CANCELLED = "cancelled"

_QUEUE_POLL_INTERVAL = 2  # 轮询器间隔（秒）
_APPROVE_PERM = "cd.deploy.approve"


def _parse_csv(s) -> list[str]:
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ── 规则评估 ──


def get_rule(db, project: str) -> dict | None:
    """查项目审批规则。规则 project 支持逗号分隔多项目（如 'php/devops-glue,static'）或 '*' 全局默认。

    匹配优先级：精确项目命中 > '*' 全局默认；同优先级多条命中时取 id 最小（最早创建）的规则。
    """
    with db.conn() as conn:
        rows = conn.execute("SELECT * FROM cd_approval_rules ORDER BY id").fetchall()
    exact = None
    fallback = None
    for row in rows:
        rule = dict(row)
        targets = _parse_csv(rule.get("project") or "")
        if not targets:
            continue
        if project in targets:
            if exact is None:
                exact = rule
        elif "*" in targets and fallback is None:
            fallback = rule
    return exact or fallback


def resolve_target_envs(db, server_ids: str = "") -> set[str]:
    """解析目标服务器的环境标签（cd_servers.tags，逗号分隔）。server_ids 空 → 全部服务器。"""
    with db.conn() as conn:
        if server_ids:
            ids = [int(s) for s in (server_ids or "").split(",") if s.strip().isdigit()]
            if not ids:
                return set()
            ph = ",".join("?" * len(ids))
            rows = conn.execute(f"SELECT tags FROM cd_servers WHERE id IN ({ph})", ids).fetchall()
        else:
            rows = conn.execute("SELECT tags FROM cd_servers").fetchall()
    envs: set[str] = set()
    for r in rows:
        envs.update(_parse_csv(r.get("tags") or ""))
    return envs


def approval_required(db, project: str, server_ids: str = "", for_rollback: bool = False) -> dict | None:
    """判断部署是否需要审批，需要则返回规则 dict，否则 None。

    require_envs 为空 → 所有部署都需审批；否则仅目标环境命中 require_envs 时需审批。
    """
    rule = get_rule(db, project)
    if not rule or not rule.get("enabled"):
        return None
    if for_rollback and not rule.get("require_rollback_approval"):
        return None
    require_envs = set(_parse_csv(rule.get("require_envs") or ""))
    if not require_envs:
        return rule
    envs = resolve_target_envs(db, server_ids)
    if require_envs & envs:
        return rule
    return None


# ── 审批单 CRUD / 状态机 ──


def create_approval(db, *, project, tag, image, deploy_type, envs, params_json, requester) -> int:
    """创建待审批单，返回审批单 id。"""
    with db.conn() as conn:
        cur = conn.execute(
            "INSERT INTO cd_approvals (project, tag, image, deploy_type, envs, params_json, status, requester) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (project, tag, image, deploy_type, ",".join(sorted(envs)), params_json, PENDING, requester),
        )
        return getattr(cur, "lastrowid", 0) or 0


def _transition(db, approval_id, from_statuses, to_status, **fields) -> bool:
    """原子状态迁移（WHERE status IN from_statuses 保证仅单次生效）。返回是否成功。"""
    sets = [f"{k}=?" for k in fields]
    sets.append("status=?")
    sets.append("updated_at=?")
    ph = ",".join("?" * len(from_statuses))
    sql = f"UPDATE cd_approvals SET {', '.join(sets)} WHERE id=? AND status IN ({ph})"
    params = [*fields.values(), to_status, _now(), approval_id, *from_statuses]
    with db.conn() as conn:
        cur = conn.execute(sql, params)
        return (getattr(cur, "rowcount", 0) or 0) > 0


def _get(db, approval_id) -> dict | None:
    with db.conn() as conn:
        row = conn.execute("SELECT * FROM cd_approvals WHERE id=?", (approval_id,)).fetchone()
    return dict(row) if row else None


def list_approvals(db, status: str = "", project: str = "", requester: str = "", page: int = 1, page_size: int = 20) -> dict:
    page = max(page, 1)
    page_size = max(min(page_size, 100), 1)
    offset = (page - 1) * page_size
    where = []
    args = []
    if status:
        where.append("status=?")
        args.append(status)
    if project:
        where.append("project=?")
        args.append(project)
    if requester:
        where.append("requester=?")
        args.append(requester)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    with db.conn() as conn:
        total = conn.execute(f"SELECT COUNT(*) AS cnt FROM cd_approvals{where_sql}", args).fetchone()["cnt"]
        rows = conn.execute(
            f"SELECT * FROM cd_approvals{where_sql} ORDER BY id DESC LIMIT ? OFFSET ?",
            [*args, page_size, offset],
        ).fetchall()
    return {
        "items": [dict(r) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max((total + page_size - 1) // page_size, 1),
    }


# ── 授权 ──


def can_approve(approval: dict, rule: dict, user: dict) -> bool:
    """审批权限：super_admin 或持 cd.deploy.approve 或 ∈ approvers 或 role == approver_role。"""
    role = user.get("role") or ""
    username = user.get("username") or ""
    if role == settings.super_admin_role:
        return True
    if _APPROVE_PERM in (user.get("permissions") or []):
        return True
    approvers = _parse_csv(rule.get("approvers") or "")
    if approvers and username in approvers:
        return True
    approver_role = (rule.get("approver_role") or "").strip()
    return bool(approver_role and role == approver_role)


def can_cancel(approval: dict, user: dict) -> bool:
    """撤销权限：super_admin 或申请人本人。"""
    role = user.get("role") or ""
    if role == settings.super_admin_role:
        return True
    return (user.get("username") or "") == (approval.get("requester") or "")


# ── 审批动作 ──


def approve(db, approval_id, user: dict) -> dict:
    approval = _get(db, approval_id)
    if not approval:
        raise NotFoundError("审批单不存在", error_key="errors.approval_not_found")
    rule = get_rule(db, approval["project"]) or {}
    if not can_approve(approval, rule, user):
        raise HTTPException(403, "无审批权限")
    ok = _transition(db, approval_id, [PENDING], APPROVED, approver=user.get("username", ""), approved_at=_now())
    if not ok:
        return {"success": False, "message": "审批单已处理"}
    _notify_result(db, rule, approval, "approved", approver=user.get("username", ""))
    # 立即领取执行（轮询器作为兜底，保证崩溃后仍会重投）
    _claim_and_execute(db, lambda: Database(), approval_id)
    return {"success": True, "message": "已批准，排队执行中"}


def reject(db, approval_id, user: dict, note: str = "") -> dict:
    approval = _get(db, approval_id)
    if not approval:
        raise NotFoundError("审批单不存在", error_key="errors.approval_not_found")
    rule = get_rule(db, approval["project"]) or {}
    if not can_approve(approval, rule, user):
        raise HTTPException(403, "无审批权限")
    ok = _transition(
        db, approval_id, [PENDING], REJECTED, approver=user.get("username", ""), approve_note=note, approved_at=_now()
    )
    if not ok:
        return {"success": False, "message": "审批单已处理"}
    _notify_result(db, rule, approval, "rejected", approver=user.get("username", ""), note=note)
    return {"success": True, "message": "已驳回"}


def cancel(db, approval_id, user: dict) -> dict:
    approval = _get(db, approval_id)
    if not approval:
        raise NotFoundError("审批单不存在", error_key="errors.approval_not_found")
    if not can_cancel(approval, user):
        raise HTTPException(403, "仅申请人可撤销")
    ok = _transition(db, approval_id, [PENDING], CANCELLED, approver=user.get("username", ""))
    if not ok:
        return {"success": False, "message": "仅待审批状态可撤销"}
    return {"success": True, "message": "已撤销"}


# ── 部署审批闸门（供 deploy / k8s_deploy 路由调用）──


def gate_deploy(db, *, project, tag, deploy_type, server_ids, params, requester, lang="en", for_rollback=False) -> dict | None:
    """部署审批闸门：需要审批则创建审批单并通知，返回 {"pending": True, "approval_id"}；否则 None。

    for_rollback=True 时按 require_rollback_approval 规则判断（回滚是否需要审批）。
    """
    rule = approval_required(db, project, server_ids, for_rollback=for_rollback)
    if not rule:
        return None
    image = _resolve_display_image(db, project, tag)
    envs = resolve_target_envs(db, server_ids)
    params_json = json.dumps(params, ensure_ascii=False)
    approval_id = create_approval(
        db,
        project=project,
        tag=tag,
        image=image,
        deploy_type=deploy_type,
        envs=envs,
        params_json=params_json,
        requester=requester,
    )
    _notify_request(db, rule, project, tag, requester, envs, lang)
    return {"pending": True, "approval_id": approval_id}


def _resolve_display_image(db, project: str, tag: str) -> str:
    try:
        repo = CiService(db).resolve_harbor_repo(project)
        if repo:
            return f"{settings.harbor_registry}/{repo}:{tag}"
    except Exception:
        pass
    return ""


# ── 持久化队列：领取 + 执行 + 轮询 + 恢复 ──


def _claim_and_execute(db, db_factory, approval_id) -> bool:
    """领取（approved → deploying）并开线程执行。返回是否领取成功。"""
    if not _transition(db, approval_id, [APPROVED], DEPLOYING):
        return False
    threading.Thread(
        target=_run_in_thread, args=(db_factory, approval_id), daemon=True, name=f"approval-{approval_id}"
    ).start()
    return True


def _run_in_thread(db_factory, approval_id):
    db = db_factory()
    try:
        _run_approval(db, approval_id)
    except Exception:
        logger.exception("approval execution failed id=%s", approval_id)


def _run_approval(db, approval_id):
    row = _get(db, approval_id)
    if not row or row["status"] != DEPLOYING:
        return
    params = json.loads(row["params_json"] or "{}")
    user = load_user_context(db, row["requester"] or "")
    # 回滚审批：还原原生回滚标志（rollback_service 在 params 里埋了 _rollback）
    rollback = bool(params.pop("_rollback", False))
    result = execute_from_params(db, params, user, rollback=rollback)
    status = result.get("status")
    deploy_id = result.get("deploy_id", 0)
    if status == "ok":
        _transition(db, approval_id, [DEPLOYING], DEPLOYED, deploy_id=deploy_id)
    elif status == "busy":
        _transition(db, approval_id, [DEPLOYING], APPROVED)  # 重投，轮询器稍后重试
    elif status == "cancelled":
        _transition(db, approval_id, [DEPLOYING], CANCELLED)
    else:
        _transition(db, approval_id, [DEPLOYING], FAILED, deploy_id=deploy_id)


def _drain_once(db, db_factory):
    """领取一个 approved 审批单执行（每轮一个）。"""
    with db.conn() as conn:
        row = conn.execute(
            "SELECT id FROM cd_approvals WHERE status=? ORDER BY id LIMIT 1", (APPROVED,)
        ).fetchone()
    if row:
        _claim_and_execute(db, db_factory, row["id"])


_poller_started = False


def start_queue_poller(db_factory):
    """启动审批队列轮询器（后台线程，幂等）。"""
    global _poller_started
    if _poller_started:
        return
    _poller_started = True

    def _loop():
        while True:
            try:
                db = db_factory()
                _drain_once(db, db_factory)
            except Exception:
                logger.exception("approval queue poller error")
            time.sleep(_QUEUE_POLL_INTERVAL)

    threading.Thread(target=_loop, daemon=True, name="approval-queue-poller").start()


def recover_on_startup(db):
    """进程重启恢复：清僵尸 running 部署锁 + 重投 deploying 审批单。"""
    from backend.deploy_run import recover_stale_running

    recovered_logs = recover_stale_running(db)
    with db.conn() as conn:
        cur = conn.execute(
            "UPDATE cd_approvals SET status=?, updated_at=? WHERE status=?", (APPROVED, _now(), DEPLOYING)
        )
        recovered_approvals = getattr(cur, "rowcount", 0) or 0
    if recovered_logs or recovered_approvals:
        logger.info("startup recovery: interrupted_deploys=%s, re_queued_approvals=%s", recovered_logs, recovered_approvals)


# ── 通知 ──


def _notify_request(db, rule, project, tag, requester, envs, lang):
    bot_id = rule.get("notify_bot_id") or 0
    if not bot_id:
        return
    env_label = ", ".join(sorted(envs)) or ("全部" if lang == "zh" else "all")
    if lang == "zh":
        msg = f"[审批请求] {project} 部署 {tag} 待审批\n申请人: {requester}\n环境: {env_label}"
    else:
        msg = f"[Approval Request] {project} deploy {tag} pending\nRequester: {requester}\nEnvs: {env_label}"
    notify_approval(db, bot_id, msg)


def _notify_result(db, rule, approval, result, approver="", note=""):
    bot_id = rule.get("notify_bot_id") or 0
    if not bot_id:
        return
    project = approval.get("project", "")
    tag = approval.get("tag", "")
    if result == "approved":
        msg = f"[审批通过] {project} {tag} 已批准，排队执行中\n审批人: {approver}"
    elif result == "rejected":
        msg = f"[审批驳回] {project} {tag} 已驳回\n审批人: {approver}\n备注: {note or '无'}"
    else:
        msg = f"[审批撤销] {project} {tag} 已撤销\n操作人: {approver}"
    notify_approval(db, bot_id, msg)
