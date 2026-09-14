"""审批路由 — 审批单列表/审批动作 + 审批规则管理 + 回滚"""

import logging
from contextlib import suppress

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from backend.auth import get_current_user, get_db, require_perm
from backend.config import settings
from backend.database import Database
from backend.exceptions import AppException, NotFoundError, ServiceUnavailableError, ValidationError
from backend.models import RollbackRequest
from backend.services import approval_service as svc
from backend.services.ci_client import CiClientError, get_ci_client
from backend.services.deploy_executor import execute_from_params
from backend.services.rollback_service import prepare_rollback, rollback

router = APIRouter(prefix="/api", tags=["approvals"])
logger = logging.getLogger(__name__)


class RejectRequest(BaseModel):
    note: str = ""


class ApprovalRuleRequest(BaseModel):
    enabled: bool = False
    require_envs: str = ""  # 逗号分隔环境标签，空 = 所有环境需审批
    approver_role: str = ""  # 审批角色（须为共享 roles 表中的角色）；空 = 仅按 approvers 审批
    approvers: str = ""  # 逗号分隔显式审批人用户名（优先于 approver_role）
    notify_bot_id: int = 0
    require_rollback_approval: bool = True


def _is_manager(user: dict) -> bool:
    """是否具备查看所有审批单的权限（super_admin 或审批人 cd.deploy.approve）。"""
    if (user.get("role") or "") == settings.super_admin_role:
        return True
    return "cd.deploy.approve" in (user.get("permissions") or [])


def _enrich(db, item: dict, user: dict, rules: list[dict] | None = None) -> dict:
    """为审批单补充当前用户的操作权限标志（供前端渲染按钮）。

    rules 可传入 svc.list_all_rules() 的结果复用（列表页批量匹配，避免逐行查库的 N+1）。
    """
    if rules is None:
        rules = svc.list_all_rules(db)
    rule = svc.match_rule(rules, item.get("project") or "") or {}
    item["can_approve"] = svc.can_approve(item, rule, user)
    item["can_cancel"] = svc.can_cancel(item, user)
    item["can_execute"] = svc.can_execute(item, user)
    return item


# ── 审批单 ──


@router.get("/approvals")
def list_approvals(
    status: str = "",
    project: str = "",
    deploy_kind: str = "",
    active: int = 0,
    mine: int = 0,
    page: int = 1,
    page_size: int = 20,
    db: Database = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """审批单列表。管理者可见全部；普通用户仅见自己发起的申请。

    active=1 时只返回活跃单据（pending/approved/deploying），供部署页恢复进度。
    deploy_kind=ssh|compose|k8s 时按部署形态过滤（部署页只显示本形态的活跃单）。
    mine=1 时强制只返回当前用户本人发起的单据（部署页场景，审批人身份也不例外）。
    """
    requester = user.get("username", "") if mine else ("" if _is_manager(user) else user.get("username", ""))
    result = svc.list_approvals(
        db,
        status=status,
        project=project,
        requester=requester,
        deploy_kind=deploy_kind,
        page=page,
        page_size=page_size,
        active=bool(active),
    )
    rules = svc.list_all_rules(db)
    result["items"] = [_enrich(db, item, user, rules) for item in result["items"]]
    return result


@router.get("/approvals/badge")
def approvals_badge(
    db: Database = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """侧边栏审批菜单红点计数：待我审批 + 我的待执行（轻量轮询端点）。

    注意：必须声明在 /approvals/{approval_id} 之前，否则 "badge" 会被当作 id 解析。
    """
    return svc.count_todo(db, user)


@router.get("/approvals/{approval_id}")
def get_approval(
    approval_id: int,
    db: Database = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    approval = svc.get_by_id(db, approval_id)
    if not approval:
        raise NotFoundError("审批单不存在", error_key="errors.approval_not_found")
    # 普通用户只能看自己的
    if not _is_manager(user) and (approval.get("requester") or "") != user.get("username", ""):
        raise NotFoundError("审批单不存在", error_key="errors.approval_not_found")
    return _enrich(db, approval, user)


@router.post("/approvals/{approval_id}/approve")
def approve(
    approval_id: int,
    db: Database = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    # 定时发布时间由申请人在提交部署时指定；批准只放行（无 body，兼容旧调用方/API token）
    return svc.approve(db, approval_id, user)


@router.post("/approvals/{approval_id}/reject")
def reject(
    approval_id: int,
    req: RejectRequest,
    db: Database = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    return svc.reject(db, approval_id, user, req.note)


@router.post("/approvals/{approval_id}/cancel")
def cancel(
    approval_id: int,
    db: Database = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    return svc.cancel(db, approval_id, user)


@router.post("/approvals/{approval_id}/execute")
def execute_approval(
    approval_id: int,
    db: Database = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """申请人凭已批准单手动执行（同步 JSON，主要供 API token 集成）。

    严格限单据 requester 本人；一次性（原子领取 approved→deploying）。
    注意：success=True 仅表示"执行流程已走完"，实际结果看 data.status
    （ok/failed/busy/cancelled）；异常时单据被置 failed，抛出 500。
    """
    _approval, exec_user = svc.claim_for_execution(db, approval_id, user)
    try:
        result = svc.run_approval(db, approval_id, exec_user)
    except Exception:
        # 双保险：与 execute-stream 的兜底一致。领取成功后单据已是 deploying，
        # 且关联部署记录 running 持有项目并发锁——异常时必须置 failed 并退回
        # 部署记录 pending 释放锁，否则单据卡死、该项目被锁到进程重启。
        logger.error("Approval execute failed id=%s", approval_id, exc_info=True)
        with suppress(Exception):
            svc._transition(db, approval_id, [svc.DEPLOYING], svc.FAILED)
        with suppress(Exception):
            svc.reset_pending_deploy_record(db, approval_id)
        raise
    return {"success": True, "data": {"pending": False, **result}}


@router.post("/approvals/{approval_id}/execute-stream")
async def execute_approval_stream(
    approval_id: int,
    db: Database = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """申请人凭已批准单手动执行（SSE 实时日志，部署页主用）。

    领取失败（非本人/状态不对/单据不存在）以 ERROR 事件结束流。
    """
    try:
        _approval, exec_user = svc.claim_for_execution(db, approval_id, user)
    except AppException as e:
        msg = e.message

        async def _err():
            yield f"retry: 3000\ndata: ERROR:{msg}\n\n"

        return StreamingResponse(_err(), media_type="text/event-stream")

    import asyncio
    import queue
    import threading
    from contextlib import suppress

    log_queue = queue.Queue()
    exec_result = {}

    def do_exec():
        nonlocal exec_result
        try:

            def log_callback(message):
                log_queue.put(message)

            result = svc.run_approval(db, approval_id, exec_user, callback=log_callback)
            # busy 等未进入执行器的场景 callback 不会被调用，补发 output 让日志区有提示
            if result.get("status") == "busy" and result.get("output"):
                log_queue.put(result["output"])
            exec_result = {"success": True, "data": result}
        except Exception as e:
            logger.error("Approval execute stream failed id=%s", approval_id, exc_info=e)
            exec_result = {"success": False, "error": str(e)}
            # 双保险：异常时把仍 deploying 的单据置 failed，避免卡住
            with suppress(Exception):
                svc._transition(db, approval_id, [svc.DEPLOYING], svc.FAILED)
        finally:
            with suppress(Exception):
                log_queue.put(None)

    threading.Thread(target=do_exec, daemon=True).start()

    async def event_stream():
        msg_id = 0
        while True:
            try:
                msg = await asyncio.to_thread(log_queue.get, timeout=30)
                if msg is None:
                    break
                msg_id += 1
                # 多行消息需要每行都加 data: 前缀，否则 SSE 只解析第一行
                safe_msg = msg.replace("\n", "\ndata: ")
                yield f"id: {msg_id}\nretry: 3000\ndata: {safe_msg}\n\n"
            except queue.Empty:
                yield "retry: 3000\ndata: .\n\n"
                await asyncio.sleep(1)

        if exec_result.get("success"):
            result = exec_result["data"]
            # execute_from_params 返回 {"status": "ok"|"failed"|"busy"|"cancelled", ...}，END 只携带成功标志
            yield f"retry: 3000\ndata: END:{str(result.get('status') == 'ok').lower()}\n\n"
        else:
            yield f"retry: 3000\ndata: ERROR:{exec_result.get('error', 'Execute failed')}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ── 审批规则 ──


@router.get("/approval-rules")
def list_rules(db: Database = Depends(get_db), _user: dict = Depends(require_perm("cd.deploy.approve"))):
    with db.conn() as conn:
        rows = conn.execute("SELECT * FROM cd_approval_rules ORDER BY project").fetchall()
    return {"items": [dict(r) for r in rows]}


def _validate_rule(db, project: str, req: ApprovalRuleRequest) -> None:
    """审批规则写入前校验，防止悬空引用导致审批判定静默失效。

    - project 非空且不超过列宽（VARCHAR(255)，'*' 表示全局默认）；
    - approver_role 必须存在于共享 roles 表（悬空角色会让角色匹配永不命中且无告警）；
    - approvers 用户名必须存在于共享 admin_users 表（表缺失时跳过该项校验）。
    """
    project = (project or "").strip()
    if not project:
        raise ValidationError("project 不能为空", error_key="errors.deploy_validation")
    if len(project) > 255:
        raise ValidationError("project 过长（最大 255）", error_key="errors.deploy_validation")
    envs = [e.strip() for e in (req.require_envs or "").split(",") if e.strip()]
    if len(",".join(envs)) > 255:
        raise ValidationError("require_envs 过长（最大 255）", error_key="errors.deploy_validation")
    approvers = [a.strip() for a in (req.approvers or "").split(",") if a.strip()]
    if len(",".join(approvers)) > 1024:
        raise ValidationError("approvers 过长（最大 1024）", error_key="errors.deploy_validation")
    approver_role = (req.approver_role or "").strip()
    if approver_role:
        with db.conn() as conn:
            try:
                row = conn.execute("SELECT 1 FROM roles WHERE name=?", (approver_role,)).fetchone()
            except Exception as e:
                # 共享 roles 表不可达（schema 由 Glue 管理）时跳过校验，不阻断规则写入
                logger.warning("roles table check skipped: %s", e)
                row = True
        if not row:
            raise ValidationError(
                f"审批角色 {approver_role} 不存在，请先在 CI 中创建该角色",
                error_key="errors.rule_role_not_found",
            )
    if approvers:
        with db.conn() as conn:
            ph = ",".join("?" * len(approvers))
            try:
                rows = conn.execute(
                    f"SELECT username FROM admin_users WHERE LOWER(username) IN ({ph})",
                    [a.lower() for a in approvers],
                ).fetchall()
            except Exception as e:
                # 共享 admin_users 表不可达（schema 由 Glue 管理）时跳过校验，不阻断规则写入
                logger.warning("admin_users check skipped: %s", e)
                rows = None
        if rows is not None:
            found = {r["username"].lower() for r in rows}
            missing = [a for a in approvers if a.lower() not in found]
            if missing:
                raise ValidationError(f"审批人不存在: {', '.join(missing)}", error_key="errors.rule_approver_not_found")


@router.put("/approval-rules/{project:path}")
def upsert_rule(
    project: str,
    req: ApprovalRuleRequest,
    db: Database = Depends(get_db),
    _user: dict = Depends(require_perm("cd.deploy.approve")),
):
    """按项目 upsert 审批规则。project 为 '*' 表示全局默认规则。"""
    _validate_rule(db, project, req)
    project = (project or "").strip()
    approver_role = (req.approver_role or "").strip()
    approvers = ",".join(a.strip() for a in (req.approvers or "").split(",") if a.strip())
    require_envs = ",".join(e.strip() for e in (req.require_envs or "").split(",") if e.strip())
    with db.conn() as conn:
        exists = conn.execute("SELECT id FROM cd_approval_rules WHERE project=?", (project,)).fetchone()
        if exists:
            conn.execute(
                "UPDATE cd_approval_rules SET enabled=?, require_envs=?, approver_role=?, approvers=?, "
                "notify_bot_id=?, require_rollback_approval=? WHERE project=?",
                (
                    int(req.enabled),
                    require_envs,
                    approver_role,
                    approvers,
                    req.notify_bot_id,
                    int(req.require_rollback_approval),
                    project,
                ),
            )
        else:
            conn.execute(
                "INSERT INTO cd_approval_rules "
                "(project, enabled, require_envs, approver_role, approvers, notify_bot_id, require_rollback_approval) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    project,
                    int(req.enabled),
                    require_envs,
                    approver_role,
                    approvers,
                    req.notify_bot_id,
                    int(req.require_rollback_approval),
                ),
            )
    return {"success": True}


@router.delete("/approval-rules/{project:path}")
def delete_rule(
    project: str,
    db: Database = Depends(get_db),
    _user: dict = Depends(require_perm("cd.deploy.approve")),
):
    with db.conn() as conn:
        conn.execute("DELETE FROM cd_approval_rules WHERE project=?", (project,))
    return {"success": True}


@router.get("/roles")
def list_roles(_user: dict = Depends(require_perm("cd.deploy.approve"))):
    """列出所有角色，供审批规则选择审批角色。读走 CI 接口。"""
    try:
        roles = get_ci_client().list_roles()
    except CiClientError as e:
        raise ServiceUnavailableError("CI 服务不可用，请联系管理员", error_key="errors.ci_service_unavailable") from e
    return {"items": roles}


# ── 回滚 ──


@router.post("/deploy/rollback")
def rollback_deploy(
    req: RollbackRequest,
    db: Database = Depends(get_db),
    user: dict = Depends(require_perm("cd.deploy-manage")),
):
    """回滚项目到上一版成功部署（复用其参数快照重放，过审批闸门）。"""
    if not req.project:
        raise ValidationError("请提供 project", error_key="errors.deploy_validation")
    return rollback(
        db,
        req.project,
        user,
        before_deploy_id=req.deploy_id,
        deploy_type=req.deploy_type,
        tag=req.tag,
        note=req.deploy_note,
        bot_id=req.bot_id,
        lang=req.lang,
    )


@router.post("/deploy/rollback-stream")
async def rollback_deploy_stream(
    req: RollbackRequest,
    db: Database = Depends(get_db),
    user: dict = Depends(require_perm("cd.deploy-manage")),
):
    """回滚实时流式（SSE）推送，覆盖 K8S 原生/重放 与 SSH/Compose 重放。"""
    if not req.project:

        async def _err_empty():
            yield "retry: 3000\ndata: ERROR:请提供 project\n\n"

        return StreamingResponse(_err_empty(), media_type="text/event-stream")

    # 前置校验（查找 source / 构建 params / 审批闸门）
    try:
        prep = prepare_rollback(
            db,
            req.project,
            user,
            before_deploy_id=req.deploy_id,
            deploy_type=req.deploy_type,
            tag=req.tag,
            note=req.deploy_note,
            bot_id=req.bot_id,
            lang=req.lang,
        )
    except AppException as e:
        msg = e.message

        async def _err():
            yield f"retry: 3000\ndata: ERROR:{msg}\n\n"

        return StreamingResponse(_err(), media_type="text/event-stream")

    if prep["pending"]:

        async def _pending():
            yield f"retry: 3000\ndata: PENDING:{prep['approval_id']}\n\n"

        return StreamingResponse(_pending(), media_type="text/event-stream")

    import asyncio
    import queue
    import threading
    from contextlib import suppress

    log_queue = queue.Queue()
    exec_result = {}

    def do_exec():
        nonlocal exec_result
        try:

            def log_callback(message):
                log_queue.put(message)

            result = execute_from_params(
                db, prep["params"], user, callback=log_callback, rollback=prep["rollback_flag"]
            )
            # busy 等未进入执行器的场景 callback 不会被调用，补发 output 让日志区有提示
            if result.get("status") == "busy" and result.get("output"):
                log_queue.put(result["output"])
            exec_result = {"success": True, "data": result}
        except Exception as e:
            logger.error("Rollback stream failed", exc_info=e)
            exec_result = {"success": False, "error": str(e)}
        finally:
            with suppress(Exception):
                log_queue.put(None)

    threading.Thread(target=do_exec, daemon=True).start()

    async def event_stream():
        msg_id = 0
        while True:
            try:
                msg = await asyncio.to_thread(log_queue.get, timeout=30)
                if msg is None:
                    break
                msg_id += 1
                # 多行消息需要每行都加 data: 前缀，否则 SSE 只解析第一行
                safe_msg = msg.replace("\n", "\ndata: ")
                yield f"id: {msg_id}\nretry: 3000\ndata: {safe_msg}\n\n"
            except queue.Empty:
                yield "retry: 3000\ndata: .\n\n"
                await asyncio.sleep(1)

        if exec_result.get("success"):
            result = exec_result["data"]
            # execute_from_params 返回 {"status": "ok"|"failed"|"busy"|"cancelled", ...}，END 只携带成功标志
            yield f"retry: 3000\ndata: END:{str(result.get('status') == 'ok').lower()}\n\n"
        else:
            yield f"retry: 3000\ndata: ERROR:{exec_result.get('error', 'Rollback failed')}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
