"""回滚服务 - 重放上一版成功部署 / 原生回滚.

- 原生回滚(kubectl / helm / argocd):以被回滚的成功记录为上下文,调集群原生回退命令.
- 重放回滚(fluxcd / ssh / compose):取上一版不同 tag 的成功记录,复用其 params_json 快照重放.

无 params_json 快照的记录不支持回滚.
"""

import json
import logging

from backend.deployers.base import InvalidTag, validate_tag
from backend.exceptions import NotFoundError, ValidationError
from backend.services.approval_service import gate_deploy
from backend.services.deploy_executor import execute_from_params

logger = logging.getLogger(__name__)


def find_rollback_source(db, project: str, before_deploy_id: int = 0, deploy_type: str = "") -> dict | None:
    """查找可回滚的上一版成功部署记录(含 params_json 快照),无则返回 None.

    回滚目标 = 参考版本之前,tag 不同、deploy_type 相同的最近一条成功记录.
    参考版本:before_deploy_id > 0 取该 id 记录;否则取 deploy_type 限定的最新成功记录;再否则取最新成功记录.
    """
    base = (
        "SELECT * FROM cd_deploy_logs WHERE project=? AND status='ok' AND params_json IS NOT NULL AND params_json != ''"
    )
    with db.conn() as conn:
        if before_deploy_id:
            ref = conn.execute(
                "SELECT id, tag, deploy_type FROM cd_deploy_logs WHERE id=? AND project=?",
                (before_deploy_id, project),
            ).fetchone()
        elif deploy_type:
            ref = conn.execute(base + " AND deploy_type=? ORDER BY id DESC LIMIT 1", (project, deploy_type)).fetchone()
        else:
            ref = conn.execute(base + " ORDER BY id DESC LIMIT 1", (project,)).fetchone()
        if not ref:
            return None
        sql = base + " AND deploy_type=? AND tag != ? AND id < ? ORDER BY id DESC LIMIT 1"
        args = [project, ref["deploy_type"] or "", ref["tag"], ref["id"]]
        row = conn.execute(sql, args).fetchone()
    return dict(row) if row else None


def _find_record(db, project: str, deploy_id: int) -> dict | None:
    """按 id 取部署记录,无则 None."""
    with db.conn() as conn:
        row = conn.execute("SELECT * FROM cd_deploy_logs WHERE id=? AND project=?", (deploy_id, project)).fetchone()
    return dict(row) if row else None


def _find_latest_success(db, project: str, deploy_type: str = "") -> dict | None:
    """取项目(可选限定模式)最近一条成功记录(含 params_json 快照),无则 None."""
    base = (
        "SELECT * FROM cd_deploy_logs WHERE project=? AND status='ok' AND params_json IS NOT NULL AND params_json != ''"
    )
    with db.conn() as conn:
        if deploy_type:
            row = conn.execute(base + " AND deploy_type=? ORDER BY id DESC LIMIT 1", (project, deploy_type)).fetchone()
        else:
            row = conn.execute(base + " ORDER BY id DESC LIMIT 1", (project,)).fetchone()
    return dict(row) if row else None


def prepare_rollback(
    db,
    project: str,
    user: dict,
    *,
    before_deploy_id: int = 0,
    deploy_type: str = "",
    tag: str = "",
    note: str = "",
    bot_id: int = 0,
    lang: str = "en",
) -> dict:
    """准备回滚:查找 source,构建 params 快照,过审批闸门.返回:
    - 需审批: {'pending': True, 'approval_id': int, 'source_deploy_id': int}
    - 可执行: {'pending': False, 'params': dict, 'rollback_flag': bool, 'source_deploy_id': int}

    回滚三类(tag 优先级最高):
    - 指定 tag(tag 非空):重放该 tag -- 以该模式最新成功记录为上下文,把 tag 换成所选值再部署.
    - 原生回滚(kubectl/helm/argocd,tag 空):以该模式最新成功记录为上下文,
      直接调集群原生回退命令(rollout undo / helm rollback / argocd rollback),不看 tag 记录.
    - 重放回滚(fluxcd/ssh/compose,tag 空):找上一版不同 tag 的成功记录,复用其参数快照重放.
    """
    mode = deploy_type
    if before_deploy_id:
        ref = _find_record(db, project, before_deploy_id)
        if not ref:
            raise NotFoundError("部署记录不存在", error_key="errors.rollback_not_found")
        mode = ref["deploy_type"] or ""

    native = mode in ("k8s/kubectl", "k8s/helm", "k8s/argocd")

    if tag:
        try:
            validate_tag(tag)
        except InvalidTag as e:
            raise ValidationError(str(e), error_key="errors.deploy_validation") from e

    if tag:
        source = (
            _find_record(db, project, before_deploy_id) if before_deploy_id else _find_latest_success(db, project, mode)
        )
    elif native:
        source = (
            _find_record(db, project, before_deploy_id) if before_deploy_id else _find_latest_success(db, project, mode)
        )
    else:
        source = find_rollback_source(db, project, before_deploy_id, mode)

    if not source:
        raise NotFoundError("无可用回滚版本（需存在成功部署记录）", error_key="errors.rollback_not_found")

    try:
        params = json.loads(source["params_json"] or "{}")
    except (TypeError, ValueError) as e:
        raise ValidationError("该部署记录的参数快照已损坏", error_key="errors.rollback_unsupported") from e
    if not params.get("deploy_type"):
        raise ValidationError("该部署记录缺少回滚所需参数快照", error_key="errors.rollback_unsupported")

    rollback_type = "native" if (native and not tag) else "replay"
    params["rollback_type"] = rollback_type

    if tag:
        params["tag"] = tag

    marker = f"[回滚到 {tag}]" if tag else f"[回滚 #{source['id']}]"
    user_note = (note or "").strip()
    max_note_len = 512 - len(marker) - 1
    if max_note_len > 0 and len(user_note) > max_note_len:
        user_note = user_note[:max_note_len]
    params["deploy_note"] = f"{marker} {user_note}".strip()
    params["bot_id"] = int(bot_id or params.get("bot_id") or 0)
    params["lang"] = lang or params.get("lang") or "en"

    tag_val = params.get("tag") or source.get("tag") or ""
    deploy_type_val = params["deploy_type"]
    server_ids = params.get("server_ids") or ""

    rollback_flag = native and not tag
    params["_rollback"] = rollback_flag
    gate = gate_deploy(
        db,
        project=project,
        tag=tag_val,
        deploy_type=deploy_type_val,
        server_ids=server_ids,
        params=params,
        requester=(user or {}).get("username", ""),
        lang=lang,
        for_rollback=True,
    )
    if gate:
        return {"pending": True, "approval_id": gate["approval_id"], "source_deploy_id": source["id"]}
    return {"pending": False, "params": params, "rollback_flag": rollback_flag, "source_deploy_id": source["id"]}


def rollback(
    db,
    project: str,
    user: dict,
    *,
    before_deploy_id: int = 0,
    deploy_type: str = "",
    tag: str = "",
    note: str = "",
    bot_id: int = 0,
    lang: str = "en",
) -> dict:
    """同步执行回滚(非流式).返回:
    - 需审批: {'pending': True, 'approval_id': int, 'source_deploy_id': int}
    - 直接执行: {'pending': False, 'status': 'ok'|'failed'|'busy'|'cancelled', 'deploy_id': int, 'source_deploy_id': int, 'output': str}
    """
    prep = prepare_rollback(
        db,
        project,
        user,
        before_deploy_id=before_deploy_id,
        deploy_type=deploy_type,
        tag=tag,
        note=note,
        bot_id=bot_id,
        lang=lang,
    )
    if prep["pending"]:
        return {"pending": True, "approval_id": prep["approval_id"], "source_deploy_id": prep["source_deploy_id"]}
    result = execute_from_params(db, prep["params"], user, rollback=prep["rollback_flag"])
    return {"pending": False, **result, "source_deploy_id": prep["source_deploy_id"]}
