"""审批路由 — 审批单列表/审批动作 + 审批规则管理 + 回滚"""

import logging

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
    approver_role: str = "cd_admin"
    approvers: str = ""  # 逗号分隔显式审批人用户名（优先于 approver_role）
    notify_bot_id: int = 0
    require_rollback_approval: bool = True


def _is_manager(user: dict) -> bool:
    """是否具备查看所有审批单的权限（super_admin 或审批人 cd.deploy.approve）。"""
    if (user.get("role") or "") == settings.super_admin_role:
        return True
    return "cd.deploy.approve" in (user.get("permissions") or [])


def _enrich(db, item: dict, user: dict) -> dict:
    """为审批单补充当前用户的操作权限标志（供前端渲染按钮）。"""
    rule = svc.get_rule(db, item.get("project") or "") or {}
    item["can_approve"] = svc.can_approve(item, rule, user)
    item["can_cancel"] = svc.can_cancel(item, user)
    return item


# ── 审批单 ──


@router.get("/approvals")
def list_approvals(
    status: str = "",
    project: str = "",
    page: int = 1,
    page_size: int = 20,
    db: Database = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """审批单列表。管理者可见全部；普通用户仅见自己发起的申请。"""
    requester = "" if _is_manager(user) else user.get("username", "")
    result = svc.list_approvals(db, status=status, project=project, requester=requester, page=page, page_size=page_size)
    result["items"] = [_enrich(db, item, user) for item in result["items"]]
    return result


@router.get("/approvals/{approval_id}")
def get_approval(
    approval_id: int,
    db: Database = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    approval = svc._get(db, approval_id)
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


# ── 审批规则 ──


@router.get("/approval-rules")
def list_rules(db: Database = Depends(get_db), _user: dict = Depends(require_perm("cd.deploy.approve"))):
    with db.conn() as conn:
        rows = conn.execute("SELECT * FROM cd_approval_rules ORDER BY project").fetchall()
    return {"items": [dict(r) for r in rows]}


@router.put("/approval-rules/{project:path}")
def upsert_rule(
    project: str,
    req: ApprovalRuleRequest,
    db: Database = Depends(get_db),
    _user: dict = Depends(require_perm("cd.deploy.approve")),
):
    """按项目 upsert 审批规则。project 为 '*' 表示全局默认规则。"""
    with db.conn() as conn:
        exists = conn.execute("SELECT id FROM cd_approval_rules WHERE project=?", (project,)).fetchone()
        if exists:
            conn.execute(
                "UPDATE cd_approval_rules SET enabled=?, require_envs=?, approver_role=?, approvers=?, "
                "notify_bot_id=?, require_rollback_approval=? WHERE project=?",
                (
                    int(req.enabled),
                    req.require_envs,
                    req.approver_role,
                    req.approvers,
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
                    req.require_envs,
                    req.approver_role,
                    req.approvers,
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
