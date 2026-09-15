"""审批服务 — 规则评估 + 审批单状态机（申请 → 批准 → 申请人手动执行）。

v1.5.3 流程（批准不再自动部署）：
1. 规则评估：cd_approval_rules 按项目（'*' 为全局默认）配置是否需要审批、
   哪些环境（require_envs，命中目标服务器 tags 才审批）、审批人
   （approvers 显式用户名优先，approver_role 兜底）。
2. 审批单状态机：
     pending → approved → deploying → deployed / failed
     pending → rejected / cancelled
     approved → cancelled（申请人撤销）
   批准只把 pending 原子改成 approved（落库即不丢）；部署何时执行由申请人
   凭已批准单手动触发（execute / execute-stream 接口），严格限本人、一次性。
3. 崩溃恢复：进程重启后 recover_on_startup 清僵尸 running 部署锁，并把
   deploying 审批单重置回 approved，由申请人重新执行（参数快照不丢）。
"""

import json
import logging
import threading
from contextlib import suppress
from datetime import datetime

from fastapi import HTTPException

from backend.auth import enforce_deploy_perm, load_user_context
from backend.database import Database
from backend.config import settings
from backend.deploy_run import (
    create_pending_deploy_record,
    mark_approval_log_terminal,
    reset_pending_deploy_record,
)
from backend.exceptions import AppException, ConflictError, NotFoundError, ValidationError
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

# 未终态（可恢复/可展示的活跃单据）
ACTIVE_STATUSES = (PENDING, APPROVED, DEPLOYING)

_APPROVE_PERM = "cd.deploy.approve"


def _parse_csv(s) -> list[str]:
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _parse_scheduled_at(s: str) -> str:
    """解析并规范化定时执行时间。空 → ''（立即，不定时）。

    接受 'YYYY-MM-DD HH:MM[:SS]'（前端 datetime-local 的 'T' 分隔自动归一为空格），
    与库内 created_at/approved_at 同为本地朴素时间。非法格式抛 ValidationError。
    """
    s = (s or "").strip().replace("T", " ")
    if not s:
        return ""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    raise ValidationError(
        "定时时间格式无效，应为 YYYY-MM-DD HH:MM[:SS]",
        error_key="errors.approval_invalid_schedule",
    )


# ── 规则评估 ──


def list_all_rules(db) -> list[dict]:
    """取全部审批规则（ORDER BY id）。列表页批量匹配用，避免逐行查库的 N+1。"""
    with db.conn() as conn:
        rows = conn.execute("SELECT * FROM cd_approval_rules ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def match_rule(rules: list[dict], project: str) -> dict | None:
    """在规则列表中匹配项目规则。规则 project 支持逗号分隔多项目（如 'php/devops-glue,static'）或 '*' 全局默认。

    匹配优先级：精确项目命中 > '*' 全局默认；同优先级多条命中时取列表序最前（即 id 最小/最早创建）。
    """
    exact = None
    fallback = None
    for rule in rules:
        targets = _parse_csv(rule.get("project") or "")
        if not targets:
            continue
        if project in targets:
            if exact is None:
                exact = rule
        elif "*" in targets and fallback is None:
            fallback = rule
    return exact or fallback


def get_rule(db, project: str) -> dict | None:
    """查项目审批规则（便捷入口：全量取规则后匹配；列表页请用 list_all_rules + match_rule 批量）。"""
    return match_rule(list_all_rules(db), project)


def resolve_target_envs(db, server_ids: str = "") -> set[str]:
    """解析目标服务器的环境标签（cd_servers.tags，逗号分隔）。server_ids 空 → 全部服务器。

    防御（fail-closed）：server_ids 非空但解析不出任何合法 id 时抛 ValidationError，
    绝不静默返回空集合——空交集会让 require_envs 规则失效、审批被静默跳过。
    """
    server_ids = (server_ids or "").strip()
    if not server_ids:
        with db.conn() as conn:
            rows = conn.execute("SELECT tags FROM cd_servers").fetchall()
    else:
        ids = [int(s) for s in server_ids.split(",") if s.strip().isdigit()]
        if not ids:
            raise ValidationError(f"server_ids 无效: {server_ids}", error_key="errors.deploy_validation")
        ph = ",".join("?" * len(ids))
        with db.conn() as conn:
            rows = conn.execute(f"SELECT tags FROM cd_servers WHERE id IN ({ph})", ids).fetchall()
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


def create_approval(
    db, *, project, tag, image, deploy_type, envs, params_json, requester, scheduled_at="", status=PENDING, approver=""
) -> int:
    """创建审批单，返回审批单 id。

    scheduled_at 由申请人在提交部署时指定（空=立即）。默认创建 pending 待审批单；
    无审批规则但指定了定时时，调用方传入 status=APPROVED 直接落一张"已批准"定时单
    （无人工审批人，approver 留空），由后台调度器到点自动执行。
    """
    with db.conn() as conn:
        cur = conn.execute(
            "INSERT INTO cd_approvals "
            "(project, tag, image, deploy_type, envs, params_json, status, requester, approver, scheduled_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                project,
                tag,
                image,
                deploy_type,
                ",".join(sorted(envs)),
                params_json,
                status,
                requester,
                approver,
                scheduled_at,
            ),
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


def get_by_id(db, approval_id) -> dict | None:
    """按 id 查审批单（对外公共入口，路由层用这个而不是 _get）。"""
    return _get(db, approval_id)


def list_approvals(
    db,
    status: str = "",
    project: str = "",
    requester: str = "",
    deploy_kind: str = "",
    page: int = 1,
    page_size: int = 20,
    active: bool = False,
) -> dict:
    page = max(page, 1)
    page_size = max(min(page_size, 100), 1)
    offset = (page - 1) * page_size
    where = []
    args = []
    if active:
        # 活跃单据：pending/approved/deploying（部署页恢复"我的申请进度"用）
        ph = ",".join("?" * len(ACTIVE_STATUSES))
        where.append(f"status IN ({ph})")
        args.extend(ACTIVE_STATUSES)
    elif status:
        where.append("status=?")
        args.append(status)
    if project:
        where.append("project=?")
        args.append(project)
    if requester:
        where.append("requester=?")
        args.append(requester)
    # 部署形态过滤：避免 Docker 页的活跃单串显到 K8s 页（反之亦然）。
    # ssh/compose 精确匹配；k8s 匹配所有 k8s/<cd_type> 子形态。
    kind = (deploy_kind or "").strip().lower()
    if kind in ("ssh", "compose"):
        where.append("deploy_type=?")
        args.append(kind)
    elif kind == "k8s":
        # 注意：MySQL(pymysql) 会对 SQL 文本中的裸 % 做参数格式化，
        # 模式串必须走绑定参数，不能写字面量 'k8s/%'。
        where.append("(deploy_type='k8s' OR deploy_type LIKE ?)")
        args.append("k8s/%")
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    # active 场景（部署页恢复进度）只取最需要用户行动的一条：
    # deploying（执行中）> approved（待执行）> pending（待审批），同优先级再按新到旧。
    # 否则较新的 pending 单会挡住较早的 approved 单，导致申请人无法执行已批准的部署。
    if active:
        order_sql = (
            "ORDER BY CASE status "
            f"WHEN '{DEPLOYING}' THEN 0 WHEN '{APPROVED}' THEN 1 WHEN '{PENDING}' THEN 2 ELSE 3 END, id DESC"
        )
    else:
        order_sql = "ORDER BY id DESC"
    with db.conn() as conn:
        total = conn.execute(f"SELECT COUNT(*) AS cnt FROM cd_approvals{where_sql}", args).fetchone()["cnt"]
        rows = conn.execute(
            f"SELECT * FROM cd_approvals{where_sql} {order_sql} LIMIT ? OFFSET ?",
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
    """撤销权限：super_admin 或申请人本人；仅 pending/approved（执行前）可撤销。"""
    role = user.get("role") or ""
    if role != settings.super_admin_role and (user.get("username") or "") != (approval.get("requester") or ""):
        return False
    return approval.get("status") in (PENDING, APPROVED)


def can_execute(approval: dict, user: dict) -> bool:
    """执行权限：仅申请人本人（super_admin 也不例外），且单据已批准待执行。"""
    if (user.get("username") or "") != (approval.get("requester") or ""):
        return False
    return approval.get("status") == APPROVED


# ── 审批动作 ──


def _ensure_not_requester(approval: dict, user: dict) -> None:
    """职责分离（四眼原则）：任何人（含 super_admin）都不能审批/驳回自己发起的部署单。"""
    requester = (approval.get("requester") or "").strip()
    current = (user.get("username") or "").strip()
    if requester and current and requester == current:
        raise AppException(
            "申请人不能审批或驳回自己发起的部署单，请由其他审批人处理",
            status_code=403,
            error_key="errors.self_approval_forbidden",
        )


def approve(db, approval_id, user: dict) -> dict:
    approval = _get(db, approval_id)
    if not approval:
        raise NotFoundError("审批单不存在", error_key="errors.approval_not_found")
    _ensure_not_requester(approval, user)
    rule = get_rule(db, approval["project"]) or {}
    if not can_approve(approval, rule, user):
        raise AppException(
            "无审批权限（需要 cd.deploy.approve 权限、规则内审批人身份或对应审批角色）",
            status_code=403,
            error_key="errors.approve_perm_denied",
        )
    # 定时发布时间已由申请人在提交部署时决定；批准只放行，不改动 scheduled_at。
    ok = _transition(db, approval_id, [PENDING], APPROVED, approver=user.get("username", ""), approved_at=_now())
    if not ok:
        return {"success": False, "message": "审批单已处理"}
    sched = (approval.get("scheduled_at") or "").strip()
    _notify_result(db, rule, approval, "approved", approver=user.get("username", ""))
    logger.info(
        "approval %s approved by %s, awaiting requester %s to execute%s",
        approval_id,
        user.get("username", ""),
        approval.get("requester", ""),
        f" (scheduled at {sched})" if sched else "",
    )
    return {
        "success": True,
        "message": "已批准，待申请人执行部署" if not sched else f"已批准，定时 {sched} 自动执行",
    }


def reject(db, approval_id, user: dict, note: str = "") -> dict:
    approval = _get(db, approval_id)
    if not approval:
        raise NotFoundError("审批单不存在", error_key="errors.approval_not_found")
    _ensure_not_requester(approval, user)
    rule = get_rule(db, approval["project"]) or {}
    if not can_approve(approval, rule, user):
        raise AppException(
            "无审批权限（需要 cd.deploy.approve 权限、规则内审批人身份或对应审批角色）",
            status_code=403,
            error_key="errors.approve_perm_denied",
        )
    ok = _transition(
        db, approval_id, [PENDING], REJECTED, approver=user.get("username", ""), approve_note=note, approved_at=_now()
    )
    if not ok:
        return {"success": False, "message": "审批单已处理"}
    mark_approval_log_terminal(db, approval_id, "rejected")
    _notify_result(db, rule, approval, "rejected", approver=user.get("username", ""), note=note)
    return {"success": True, "message": "已驳回"}


def cancel(db, approval_id, user: dict) -> dict:
    approval = get_by_id(db, approval_id)
    if not approval:
        raise NotFoundError("审批单不存在", error_key="errors.approval_not_found")
    is_owner = (user.get("username") or "") == (approval.get("requester") or "")
    is_super = (user.get("role") or "") == settings.super_admin_role
    if not is_owner and not is_super:
        raise AppException(
            "仅申请人本人或超级管理员可撤销",
            status_code=403,
            error_key="errors.approval_cancel_perm",
        )
    # pending 与已批准未执行（approved）均可撤销；一旦 deploying 即不可撤销。
    # 不写 approver 字段：保留原审批人身份，撤销人经通知消息留痕（避免覆盖丢失审批人信息）。
    ok = _transition(db, approval_id, [PENDING, APPROVED], CANCELLED)
    if not ok:
        return {"success": False, "message": "仅待审批或已批准未执行的单据可撤销"}
    mark_approval_log_terminal(db, approval_id, "cancelled")
    rule = get_rule(db, approval.get("project") or "") or {}
    _notify_result(db, rule, approval, "cancelled", approver=user.get("username", ""))
    return {"success": True, "message": "已撤销"}


# ── 部署审批闸门（供 deploy / k8s_deploy 路由调用）──


def gate_deploy(
    db, *, project, tag, deploy_type, server_ids, params, requester, lang="en", for_rollback=False, scheduled_at=""
) -> dict | None:
    """部署审批闸门。返回 None 表示直接执行；否则返回 {"pending": True, "approval_id", "scheduled", "message"}。

    for_rollback=True 时按 require_rollback_approval 规则判断（回滚是否需要审批）。
    scheduled_at 由申请人在提交部署时指定（可选定时发布）；非法格式抛 ValidationError。
    - 需要审批 → 创建 pending 审批单（定时字段随单落库，批准后到点自动执行）。
    - 无需审批但指定了定时 → 直接创建"已批准"定时单（无人工审批人），由后台调度器到点执行，
      保证定时发布在无审批规则的项目同样生效，而非被静默忽略成立即发布。
    """
    sched = _parse_scheduled_at(scheduled_at)
    rule = approval_required(db, project, server_ids, for_rollback=for_rollback)
    if not rule and not sched:
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
        scheduled_at=sched,
        status=PENDING if rule else APPROVED,
    )
    # 同步生成 pending 部署记录：提交申请即在部署日志可见（不占项目锁），
    # 批准/到点执行时复用同一行转 running→ok/failed；驳回/撤销则标记对应终态。
    create_pending_deploy_record(
        db,
        deploy_type=deploy_type,
        project=project,
        tag=tag,
        image=image,
        triggered_by=requester,
        deploy_note=str((params or {}).get("deploy_note") or ""),
        params_json=params_json,
        rollback_type=str((params or {}).get("rollback_type") or "manual"),
        approval_id=approval_id,
    )
    if rule:
        _notify_request(db, rule, project, tag, requester, envs, lang)
        message = "部署已提交审批" + (f"，定时 {sched} 自动执行" if sched else "")
        return {"pending": True, "approval_id": approval_id, "scheduled": bool(sched), "message": message}
    return {
        "pending": True,
        "approval_id": approval_id,
        "scheduled": True,
        "message": f"已创建定时发布，将于 {sched} 自动执行",
    }


def _resolve_display_image(db, project: str, tag: str) -> str:
    try:
        repo = CiService(db).resolve_harbor_repo(project)
        if repo:
            return f"{settings.harbor_registry}/{repo}:{tag}"
    except Exception:
        pass
    return ""


# ── 手动执行：领取 + 执行 + 崩溃恢复 ──


def _enforce_execute_perm(params: dict, user: dict) -> None:
    """按审批单快照复核执行人当前权限（防御深度：账号权限可能已被收回）。

    回滚快照要求 cd.deploy-manage；普通部署按 deploy_type 映射子权限
    （enforce_deploy_perm 同时允许 cd.deploy-manage blanket 与 super_admin）。
    """
    if bool(params.get("_rollback")):
        role = user.get("role") or ""
        if role == settings.super_admin_role or "cd.deploy-manage" in (user.get("permissions") or []):
            return
        raise AppException(
            "无回滚执行权限（需要 cd.deploy-manage）",
            status_code=403,
            error_key="errors.approval_execute_perm",
        )
    # 统一转 AppException：同步接口经异常处理器返回 error_key，SSE 接口据其发 ERROR 事件
    # （裸 HTTPException 无法被 execute-stream 的 except AppException 捕获，会变成 500）
    try:
        enforce_deploy_perm(user, params.get("deploy_type", ""))
    except HTTPException as e:
        raise AppException(
            "当前账号已无该类型部署的执行权限",
            status_code=e.status_code,
            error_key="errors.approval_execute_perm",
        ) from e


def claim_for_execution(db, approval_id, user: dict) -> tuple[dict, dict]:
    """校验并原子领取审批单（approved → deploying）。

    仅单据 requester 本人可领取；领取前实时重建申请人身份并按快照复核权限。
    返回 (审批单, 执行身份)。失败抛 NotFoundError/AppException/ConflictError。
    """
    approval = _get(db, approval_id)
    if not approval:
        raise NotFoundError("审批单不存在", error_key="errors.approval_not_found")
    if (approval.get("requester") or "") != (user.get("username") or ""):
        raise AppException(
            "仅申请人本人可执行该审批单",
            status_code=403,
            error_key="errors.approval_execute_self_only",
        )
    if approval.get("status") != APPROVED:
        raise ConflictError(
            "审批单当前状态不可执行（仅已批准待执行的单据可执行）",
            error_key="errors.approval_not_executable",
        )
    # 实时重建身份（与旧后台执行一致）并按快照复核当前权限
    exec_user = load_user_context(db, approval["requester"] or "")
    params = json.loads(approval.get("params_json") or "{}")
    _enforce_execute_perm(params, exec_user)
    # 原子领取：并发/重复请求仅一个成功
    if not _transition(db, approval_id, [APPROVED], DEPLOYING):
        raise ConflictError(
            "审批单正在被执行或状态已变化",
            error_key="errors.approval_not_executable",
        )
    logger.info("approval %s claimed for execution by %s", approval_id, user.get("username", ""))
    return _get(db, approval_id) or approval, exec_user


def run_approval(db, approval_id: int, user: dict, callback=None) -> dict:
    """执行已领取（deploying）审批单的部署/回滚，并按结果推进单据终态。

    必须在 claim_for_execution 成功后调用。返回 execute_from_params 风格
    {"status": ok|failed|busy|cancelled, "deploy_id", "output"}：
    busy（项目锁冲突）退回 approved，单据不消耗，可稍后重试。
    """
    row = _get(db, approval_id)
    if not row or row.get("status") != DEPLOYING:
        return {"status": "failed", "deploy_id": 0, "output": "approval status changed before execution"}
    params = json.loads(row.get("params_json") or "{}")
    # 回滚审批：还原原生回滚标志（rollback_service 在 params 里埋了 _rollback）
    rollback = bool(params.pop("_rollback", False))
    approval_id_in_params = int(params.pop("_approval_id", 0) or 0)
    if approval_id_in_params and approval_id_in_params != approval_id:
        logger.warning("approval %s snapshot carries mismatched _approval_id=%s", approval_id, approval_id_in_params)
    result = execute_from_params(db, params, user, callback=callback, rollback=rollback, approval_id=approval_id)
    status = result.get("status")
    deploy_id = result.get("deploy_id", 0)
    if status == "ok":
        _transition(db, approval_id, [DEPLOYING], DEPLOYED, deploy_id=deploy_id)
    elif status == "busy":
        # 项目锁冲突：退回 approved，不消耗单据，申请人稍后可重试
        _transition(db, approval_id, [DEPLOYING], APPROVED)
        # 防御：若 pending 行已被领取，退回 pending 并释放锁（常规路径 busy 发生在领取前）
        try:
            reset_pending_deploy_record(db, approval_id)
        except Exception:
            # reset 失败不阻断单据回退，但要留痕：running 记录持锁不释放会锁死该项目
            logger.warning("approval %s: reset pending deploy record failed on busy", approval_id, exc_info=True)
    elif status == "cancelled":
        _transition(db, approval_id, [DEPLOYING], CANCELLED)
    else:
        _transition(db, approval_id, [DEPLOYING], FAILED, deploy_id=deploy_id)
    return result


def recover_on_startup(db):
    """进程重启恢复：清僵尸 running 部署锁 + deploying 审批单按关联部署记录的实际状态收敛。

    - 关联部署记录已终态（ok/failed）→ 审批单收敛到对应终态（deployed/failed，含 deploy_id 回填）：
      堵住"部署记录已写终态但审批单还没更新"的崩溃窗口，避免申请人重复执行造成重复部署；
    - 其余（部署记录已被标记 interrupted）→ 审批单退回 approved 等待申请人重跑，
      interrupted 行退回 pending 以便申请人重新执行时能再次领取。
    """
    from backend.deploy_run import recover_stale_running

    recovered_logs = recover_stale_running(db)
    with db.conn() as conn:
        # 崩溃窗口兜底：部署记录已终态的 DEPLOYING 审批单，按记录状态收敛（不退回 approved）
        cur = conn.execute(
            "UPDATE cd_approvals SET status=?, updated_at=?, deploy_id=("
            "SELECT id FROM cd_deploy_logs WHERE approval_id=cd_approvals.id AND status='ok' ORDER BY id DESC LIMIT 1) "
            "WHERE status=? AND id IN (SELECT approval_id FROM cd_deploy_logs WHERE approval_id>0 AND status='ok')",
            (DEPLOYED, _now(), DEPLOYING),
        )
        converged_deployed = getattr(cur, "rowcount", 0) or 0
        cur = conn.execute(
            "UPDATE cd_approvals SET status=?, updated_at=?, deploy_id=("
            "SELECT id FROM cd_deploy_logs WHERE approval_id=cd_approvals.id AND status='failed' ORDER BY id DESC LIMIT 1) "
            "WHERE status=? AND id IN (SELECT approval_id FROM cd_deploy_logs WHERE approval_id>0 AND status='failed')",
            (FAILED, _now(), DEPLOYING),
        )
        converged_failed = getattr(cur, "rowcount", 0) or 0
        # 剩余 DEPLOYING（部署记录为 interrupted）→ approved 等待申请人重跑
        cur = conn.execute(
            "UPDATE cd_approvals SET status=?, updated_at=? WHERE status=?", (APPROVED, _now(), DEPLOYING)
        )
        recovered_approvals = getattr(cur, "rowcount", 0) or 0
        # 这些审批单关联的部署记录已被上面标记为 interrupted：退回 pending，
        # 申请人重新执行时 claim_pending_deploy_record 才能再次领取
        conn.execute(
            "UPDATE cd_deploy_logs SET status='pending', lock_key=NULL WHERE approval_id>0 AND status='interrupted'"
        )
    if recovered_logs or recovered_approvals or converged_deployed or converged_failed:
        logger.info(
            "startup recovery: interrupted_deploys=%s, approvals_reset_to_approved=%s, "
            "approvals_converged_deployed=%s, approvals_converged_failed=%s",
            recovered_logs,
            recovered_approvals,
            converged_deployed,
            converged_failed,
        )


# ── 菜单红点计数（与 bot 通知互补的站内提醒）──


def count_todo(db, user: dict) -> dict:
    """侧边栏审批菜单红点计数。

    - to_approve：pending 且当前用户可审批（can_approve 同口径），
      排除本人发起的单（四眼原则，自己的单不能自己审批）；
    - to_execute：本人发起、已批准待执行（申请人手动执行模型下的行动项）。

    轻量：pending 一次查询（LIMIT 200 兜底）+ 待执行一次 count + 一次规则列表，内存判定。
    """
    username = (user or {}).get("username") or ""
    with db.conn() as conn:
        row = conn.execute(
            "SELECT count(*) AS c FROM cd_approvals WHERE status=? AND requester=?",
            (APPROVED, username),
        ).fetchone()
        to_execute = row["c"] if row else 0
        pending = conn.execute(
            "SELECT project, requester FROM cd_approvals WHERE status=? ORDER BY id DESC LIMIT 200",
            (PENDING,),
        ).fetchall()
    rules = list_all_rules(db)
    to_approve = 0
    for p in pending:
        if (p.get("requester") or "") == username:
            continue
        rule = match_rule(rules, p.get("project") or "") or {}
        if can_approve(dict(p), rule, user):
            to_approve += 1
    return {"to_approve": to_approve, "to_execute": to_execute}


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
        msg = f"[审批通过] {project} {tag} 已批准，待申请人执行部署\n审批人: {approver}"
    elif result == "rejected":
        msg = f"[审批驳回] {project} {tag} 已驳回\n审批人: {approver}\n备注: {note or '无'}"
    else:
        msg = f"[审批撤销] {project} {tag} 已撤销\n操作人: {approver}"
    notify_approval(db, bot_id, msg)


# ── 定时发布调度器（v1.5.3）──
# 到点以申请人身份自动执行已批准的定时单据：claim_for_execution 内部实时重建
# 申请人身份并按快照复核权限，审计链（谁申请/谁批/谁执行）与手动执行完全一致。

_SCHED_POLL_INTERVAL = 30  # 秒
_sched_runner = None
_sched_stop = threading.Event()


def run_due_scheduled(db) -> int:
    """执行所有到点的定时审批单（approved 且 scheduled_at <= now）。返回成功执行数（status=ok）。

    每单独立隔离：
    - ConflictError（项目锁占用/并发领取）→ 保留定时，下一轮重试；
    - 权限缺失/账号被删等永久性失败 → 清空 scheduled_at（单据保持 approved，
      申请人可手动执行），避免每轮重试刷日志；
    - 意外异常（领取成功但执行抛异常）→ 单据退回 approved 并释放项目锁，避免卡死。
    """
    now = _now()
    with db.conn() as conn:
        rows = conn.execute(
            "SELECT id, requester FROM cd_approvals "
            "WHERE status=? AND scheduled_at != '' AND scheduled_at <= ? ORDER BY scheduled_at",
            (APPROVED, now),
        ).fetchall()

    executed = 0
    for row in rows:
        aid = row["id"]
        requester = row["requester"] or ""
        try:
            _, exec_user = claim_for_execution(db, aid, {"username": requester})
            result = run_approval(db, aid, exec_user)
            if result.get("status") == "ok":
                executed += 1
            logger.info(
                "scheduled approval %s executed for requester %s (status=%s)",
                aid,
                requester,
                result.get("status"),
            )
        except ConflictError:
            # 项目锁占用等瞬态冲突：保留 scheduled_at，下一轮重试
            logger.info("scheduled approval %s busy, will retry next tick", aid)
        except (AppException, NotFoundError) as e:
            # 永久性失败（权限被收回/账号被删）：清空定时，退回手动执行
            logger.warning("scheduled approval %s cannot execute (%s), clearing schedule", aid, e)
            with db.conn() as conn:
                conn.execute(
                    "UPDATE cd_approvals SET scheduled_at='', updated_at=? WHERE id=? AND status=?",
                    (_now(), aid, APPROVED),
                )
        except Exception:
            # 领取成功但执行抛意外异常：把仍 deploying 的单据退回 approved 并释放项目锁，
            # 避免单据卡死、项目被锁到进程重启（与手动执行路径的兜底一致）。
            logger.exception("scheduled approval %s execution failed unexpectedly", aid)
            with suppress(Exception):
                _transition(db, aid, [DEPLOYING], APPROVED)
            with suppress(Exception):
                reset_pending_deploy_record(db, aid)
    return executed


def _sched_run_loop():
    from backend.database import Database
    from backend.dlock import acquire, release

    while not _sched_stop.is_set():
        try:
            # 多副本/多 worker 隔离：锁被其他存活实例持有时静默跳过本轮
            # （分布式锁防止多头扫描调度；单据本身的原子领取状态机兜底不重复部署）
            if acquire(Database(), "scheduled_executor", ttl_seconds=120):
                try:
                    db = Database()
                    run_due_scheduled(db)
                    # 周期清扫：恢复其他实例崩溃遗留（心跳已过期）的 running 部署锁，
                    # 与启动恢复同一套逻辑，无需依赖重启才会触发
                    recover_on_startup(db)
                finally:
                    release(Database(), "scheduled_executor")
        except Exception:
            logger.exception("scheduled executor loop error")
        _sched_stop.wait(_SCHED_POLL_INTERVAL)


def start_scheduled_executor():
    """启动定时发布调度器后台线程（幂等）。"""
    global _sched_runner
    if _sched_runner and _sched_runner.is_alive():
        return
    _sched_stop.clear()
    _sched_runner = threading.Thread(target=_sched_run_loop, daemon=True)
    _sched_runner.start()
    logger.info("scheduled deploy executor started (interval=%ss)", _SCHED_POLL_INTERVAL)


def stop_scheduled_executor():
    """停止定时发布调度器后台线程。"""
    _sched_stop.set()
    if _sched_runner:
        _sched_runner.join(timeout=5)
