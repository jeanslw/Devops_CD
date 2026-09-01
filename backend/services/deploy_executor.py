"""部署执行器 — 把持久化的部署参数反序列化并重新执行。

审批批准后的执行、回滚重放都走这里，统一复用 DeployService.execute 和
K8s 专用流程 _deploy_k8s_core，避免维护两套执行逻辑。

参数快照约定：params dict 必须含 "deploy_type" 路由判别；
以 "k8s/" 开头走 K8s 流程，否则走 SSH/Compose 流程。
"""

import json
import logging

from backend.services.deploy_service import DeployService
from backend.services.k8s_deploy_service import (
    K8sDeployRequest,
    _deploy_k8s_core,
    _notify_k8s,
    _resolve_cluster,
    _resolve_image,
)

logger = logging.getLogger(__name__)

# DeployService.execute 接受的参数（与 DeployRequest 字段一一对应）
_SSH_FIELDS = (
    "project",
    "tag",
    "deploy_type",
    "server_ids",
    "target_path",
    "deploy_mode",
    "commands",
    "yaml_content",
    "k8s_ns",
    "k8s_deploy",
    "k8s_container",
    "env_file",
    "deploy_note",
    "bot_id",
    "lang",
)
# K8sDeployRequest 字段
_K8S_FIELDS = (
    "project",
    "tag",
    "cd_type",
    "cluster_id",
    "path",
    "api_url",
    "k8s_ns",
    "deploy_note",
    "bot_id",
    "lang",
)


def build_params(deploy_type: str, fields: dict) -> str:
    """序列化部署参数快照（含 deploy_type 路由判别），供审批单/回滚存 params_json。"""
    return json.dumps({"deploy_type": deploy_type, **fields}, ensure_ascii=False)


def ssh_params(req) -> dict:
    """从 SSH/Compose 部署请求构建参数快照 dict（与 DeployService.execute 内部快照同构）。"""
    return {
        "deploy_type": req.deploy_type,
        "project": req.project,
        "tag": req.tag,
        "server_ids": req.server_ids,
        "target_path": req.target_path,
        "deploy_mode": req.deploy_mode,
        "commands": req.commands,
        "yaml_content": req.yaml_content,
        "k8s_ns": req.k8s_ns,
        "k8s_deploy": req.k8s_deploy,
        "k8s_container": req.k8s_container,
        "env_file": req.env_file,
        "deploy_note": req.deploy_note,
        "bot_id": req.bot_id,
        "lang": req.lang,
    }


def k8s_params(req) -> dict:
    """从 K8s 部署请求构建参数快照 dict（deploy_type 为 k8s/{cd_type} 路由判别）。"""
    return {
        "deploy_type": f"k8s/{req.cd_type}",
        "project": req.project,
        "tag": req.tag,
        "cd_type": req.cd_type,
        "cluster_id": req.cluster_id,
        "path": req.path,
        "api_url": req.api_url,
        "k8s_ns": req.k8s_ns,
        "deploy_note": req.deploy_note,
        "bot_id": req.bot_id,
        "lang": req.lang,
    }


def _is_busy(exc: BaseException) -> bool:
    """判断异常是否为并发锁冲突（同项目已有进行中部署），命中则交由轮询器重投。"""
    err_key = getattr(exc, "error_key", "") or ""
    return err_key == "errors.deploy_busy" or "已有部署进行中" in str(exc)


def execute_from_params(db, params: dict, user: dict, callback=None, rollback: bool = False) -> dict:
    """按持久化参数执行部署，返回 {"status": "ok"|"failed"|"busy"|"cancelled", "deploy_id": int}。

    params 必须含 deploy_type 路由判别。user 为执行身份（含 username/role/permissions）。
    rollback=True 时 K8S 走原生回滚（kubectl rollout undo / helm rollback）。
    """
    deploy_type = params.get("deploy_type", "") or ""
    try:
        if deploy_type.startswith("k8s/"):
            return _execute_k8s(db, params, user, callback, rollback=rollback)
        return _execute_ssh(db, params, user, callback)
    except Exception as e:
        if _is_busy(e):
            return {"status": "busy", "deploy_id": 0, "output": getattr(e, "message", "") or str(e)}
        logger.error("execute_from_params failed", exc_info=e)
        return {"status": "failed", "deploy_id": 0, "output": getattr(e, "message", "") or str(e)}


def _execute_ssh(db, params: dict, user: dict, callback=None) -> dict:
    svc = DeployService(db)
    kwargs = {k: params[k] for k in _SSH_FIELDS if k in params}
    kwargs.setdefault("server_ids", "")
    kwargs.setdefault("target_path", "")
    kwargs.setdefault("deploy_mode", "")
    kwargs.setdefault("commands", "")
    kwargs.setdefault("yaml_content", "")
    kwargs.setdefault("k8s_ns", "")
    kwargs.setdefault("k8s_deploy", "")
    kwargs.setdefault("k8s_container", "")
    kwargs.setdefault("env_file", "")
    kwargs.setdefault("deploy_note", "")
    kwargs.setdefault("lang", "en")
    kwargs["bot_id"] = int(kwargs.get("bot_id") or 0)

    result = svc.execute(**kwargs, callback=callback, user=user)
    output = "\n".join((r.get("output") or "") for r in result.get("results", []))
    if result.get("cancelled"):
        return {"status": "cancelled", "deploy_id": result.get("deploy_id", 0), "output": output}
    return {
        "status": "ok" if result.get("success") else "failed",
        "deploy_id": result.get("deploy_id", 0),
        "output": output,
    }


def _execute_k8s(db, params: dict, user: dict, callback=None, rollback: bool = False) -> dict:
    req = K8sDeployRequest(**{k: params[k] for k in _K8S_FIELDS if k in params})
    image, project_key, project_short = _resolve_image(db, req)
    host, port, user_srv, pwd, ssh_key = _resolve_cluster(db, req)
    result = _deploy_k8s_core(
        db,
        req,
        user,
        image,
        project_key,
        project_short,
        host,
        port,
        user_srv,
        pwd,
        ssh_key,
        callback=callback,
        rollback=rollback,
    )

    if not result.get("cancelled"):
        _notify_k8s(
            db, req.bot_id, req.tag, project_key, host, req.cd_type, image, bool(result.get("success")), req.lang
        )

    output = result.get("output", "") or ""
    if result.get("cancelled"):
        return {"status": "cancelled", "deploy_id": result.get("deploy_id", 0), "output": output}
    return {
        "status": "ok" if result.get("success") else "failed",
        "deploy_id": result.get("deploy_id", 0),
        "output": output,
    }
